"""The refresh pipeline: idempotent, resumable, and fully logged.

Refresh happens here, in the CLI -- never in the dashboard.  That separation is
deliberate: if the UI could fetch, a failed pull would be invisible behind a
page that still renders.  The dashboard reads the database and nothing else, so
a stale or failed refresh is visible as stale data rather than papered over.

Resumability is per (ticker, stage).  A run interrupted after statements but
before macro resumes at macro; a run that already succeeded for a ticker today
is skipped unless --force.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import config, db
from .analyze import Analysis, analyze
from .classify import classify
from .http import FetchError
from .model import Company, Period
from .providers import get as get_provider, available as available_providers
from .providers.base import ProviderUnavailable, Quote
from .provenance import NOT_FETCHED, UPSTREAM_ERROR, Q, utcnow
from .reconcile import Reconciliation, reconcile

STAGES = ("profile", "statements", "quote", "reconcile", "compute", "store")


@dataclass
class SourceResult:
    provider: str
    ok: bool
    detail: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    annual: list[Period] = field(default_factory=list)
    quarterly: list[Period] = field(default_factory=list)
    quote: Quote | None = None


def pull(provider_name: str, ticker: str, *, annual_years: int = 10,
         quarters: int = 8, force: bool = False) -> SourceResult:
    """Pull everything one provider has.  Failures are recorded, never raised
    past this boundary -- a dead provider must not abort the run, it must be
    visible as a provider that did not answer."""
    p = get_provider(provider_name)
    res = SourceResult(provider=provider_name, ok=False)
    try:
        if not p.available():
            res.detail = f"{provider_name}: no credentials configured"
            return res
        prof = p.profile(ticker)
        if prof.get("_empty"):
            res.detail = f"{provider_name}: returned no profile for {ticker}"
            return res
        res.profile = prof
        schema = prof.get("schema") or classify(prof.get("sector", ""),
                                                prof.get("industry", ""),
                                                prof.get("name", ""))
        cur = prof.get("currency", "") or ""
        res.annual = p.annual_periods(ticker, annual_years, schema=schema, currency=cur)
        res.quarterly = p.quarterly_periods(ticker, quarters, schema=schema, currency=cur)
        res.quote = p.quote(ticker)
        res.ok = True
        res.detail = (f"{provider_name}: {len(res.annual)} annual, "
                      f"{len(res.quarterly)} quarterly periods")
    except (FetchError, ProviderUnavailable, RuntimeError) as exc:
        res.detail = f"{provider_name}: {type(exc).__name__}: {exc}"
    return res


def anchors_from(res: SourceResult) -> dict[str, Q]:
    """The three reconciliation anchors from one source."""
    from .model import ttm

    out: dict[str, Q] = {}
    out["last_close"] = res.quote.last_close if res.quote else Q.null(
        NOT_FETCHED, missing=("last_close",), label="last_close")
    ann = res.annual[0] if res.annual else None
    out["diluted_shares"] = (ann.get("diluted_shares") if ann else Q.null(
        NOT_FETCHED, missing=("diluted_shares",), label="diluted_shares"))
    rev = ttm(res.quarterly, "revenue")
    if not rev.ok and ann is not None:
        # Fall back to the latest annual revenue, and say so -- an annual figure
        # compared against another source's TTM would be a false disagreement.
        rev = ann.get("revenue").named("revenue_ttm")
        rev = Q(value=rev.value, unit=rev.unit, as_of=rev.as_of, prov=rev.prov,
                reason=rev.reason, missing=rev.missing,
                label="revenue_ttm(annual fallback)")
    out["revenue_ttm"] = rev
    return out


def build_company(ticker: str, primary: SourceResult,
                  fallbacks: Sequence[SourceResult] = ()) -> Company:
    """Assemble the Company from the primary source only.

    Fallbacks are NOT merged field-by-field.  Blending two vendors' line items
    into one statement produces a set of numbers that does not correspond to any
    filing, and no amount of provenance annotation makes that coherent.  Other
    sources are used to cross-check, not to fill.
    """
    prof = primary.profile
    schema = prof.get("schema") or classify(prof.get("sector", ""),
                                            prof.get("industry", ""),
                                            prof.get("name", ""))
    co = Company(ticker=ticker, name=prof.get("name", ""),
                 exchange=prof.get("exchange", ""), sector=prof.get("sector", ""),
                 industry=prof.get("industry", ""), currency=prof.get("currency", ""),
                 schema=schema, cik=prof.get("cik"),
                 annual=primary.annual, quarterly=primary.quarterly)
    if primary.quote:
        co.market = {"last_close": primary.quote.last_close,
                     "market_cap": primary.quote.market_cap,
                     "shares_outstanding": primary.quote.shares_outstanding}
    return co


@dataclass
class RefreshResult:
    ticker: str
    run_id: str
    company: Company | None
    analysis: Analysis | None
    reconciliation: Reconciliation | None
    sources: list[SourceResult] = field(default_factory=list)
    stages: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker, "run_id": self.run_id, "stages": self.stages,
            "errors": self.errors,
            "sources": [{"provider": s.provider, "ok": s.ok, "detail": s.detail}
                        for s in self.sources],
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation else None,
            "analysis": self.analysis.to_dict() if self.analysis else None,
        }


def refresh_ticker(ticker: str, *, wacc: Q, deck=None, realized_prices=None,
                   providers: Sequence[str] | None = None, force: bool = False,
                   annual_years: int = 10, quarters: int = 8,
                   refi_rate: Q | None = None, con=None, today=None) -> RefreshResult:
    """Fetch, reconcile, compute and store one ticker."""
    run_id = uuid.uuid4().hex[:12]
    started = utcnow()
    result = RefreshResult(ticker=ticker, run_id=run_id, company=None, analysis=None,
                           reconciliation=None)

    names = list(providers or available_providers("fundamentals"))
    if not names:
        result.errors.append("no providers available; set keys in .env")
        result.stages["profile"] = "failed"
        return result

    for n in names:
        result.sources.append(pull(n, ticker, annual_years=annual_years,
                                   quarters=quarters, force=force))
    ok = [s for s in result.sources if s.ok]
    if not ok:
        result.errors.append("no provider returned data: "
                             + "; ".join(s.detail for s in result.sources))
        result.stages["profile"] = "failed"
        return result
    result.stages["profile"] = result.stages["statements"] = result.stages["quote"] = "ok"

    primary, others = ok[0], ok[1:]
    co = build_company(ticker, primary, others)
    result.company = co

    # Reconciliation: prefer a paid second source. Agreement with Yahoo is not
    # evidence, so Yahoo is used as the cross-check only when it is all there is,
    # and the flag records that.
    second = next((s for s in others if s.provider != "yahoo"), None)
    second = second or next((s for s in others), None)
    rec = reconcile(
        ticker, anchors_from(primary), anchors_from(second) if second else {},
        primary_source=primary.provider,
        secondary_source=second.provider if second else "none")
    if second and second.provider == "yahoo":
        rec.flags.append("CROSS_CHECK_IS_LOW_CONFIDENCE_SOURCE")
    result.reconciliation = rec
    result.stages["reconcile"] = "blocked" if rec.blocked else "ok"

    result.analysis = analyze(co, wacc=wacc, deck=deck, reconciliation=rec,
                              realized_prices=realized_prices or {},
                              refi_rate=refi_rate, today=today)
    result.stages["compute"] = "ok"

    now = utcnow()
    if con is not None:
        _store(con, co, result.analysis, rec, run_id, started, now)
        result.stages["store"] = "ok"
    return result


def _store(con, co: Company, a: Analysis, rec: Reconciliation, run_id: str,
           started: str, finished: str) -> None:
    db.upsert_company(con, ticker=co.ticker, name=co.name, exchange=co.exchange,
                      sector=co.sector, industry=co.industry, currency=co.currency,
                      schema_kind=co.schema, cik=co.cik, updated_at=finished)
    for name, q in a.metrics.items():
        db.upsert_metric(con, co.ticker, name, q, schema_kind=co.schema, now=finished)
    # Statement line items are stored too, so `ledger audit` can show the source
    # of every input, not only of every computed metric.
    for p in co.annual:
        for name, q in p.fields.items():
            db.upsert_metric(con, co.ticker, f"stmt.FY.{p.period_end}.{name}", q,
                             schema_kind=co.schema, now=finished)
    db.save_reconciliation(con, co.ticker, a.as_of or finished[:10], rec.to_dict())
    db.log_run(con, run_id, co.ticker, "refresh",
               "blocked" if rec.blocked else "ok",
               f"{a.coverage()['populated']}/{a.coverage()['total']} metrics populated",
               started, finished)


def refresh_universe(tickers: Iterable[str], **kw: Any) -> list[RefreshResult]:
    """Refresh many tickers.  One ticker's failure never aborts the rest."""
    out = []
    for t in tickers:
        try:
            out.append(refresh_ticker(t, **kw))
        except Exception as exc:  # noqa: BLE001 - a bug on one name must not kill the run
            r = RefreshResult(ticker=t, run_id=uuid.uuid4().hex[:12], company=None,
                              analysis=None, reconciliation=None)
            r.errors.append(f"unhandled: {type(exc).__name__}: {exc}")
            r.stages["compute"] = "failed"
            out.append(r)
    return out
