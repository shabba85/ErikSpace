"""`ledger` -- the command line.

All fetching and computation happens here.  The dashboard only reads what this
writes, so a failed pull surfaces as stale data rather than a page that renders
around a hole.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config, db
from .provenance import Q, utcnow

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Local equity research instrument for TSX/TSXV and "
                       "cross-listed North American equities.")
console = Console()


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(config.ROOT / ".env")
    except ImportError:
        pass


def _wacc(value: Optional[float], source: str) -> Q:
    """The discount rate must be supplied explicitly and is recorded as an
    assumption.  There is no default WACC: a wrong discount rate silently
    rewrites every valuation in the system."""
    from .provenance import MISSING_INPUT, Provenance

    if value is None:
        return Q.null(MISSING_INPUT,
                      missing=("wacc not supplied; pass --wacc (e.g. --wacc 0.09). "
                               "There is no default: the discount rate determines "
                               "every valuation output.",),
                      unit="ratio", label="wacc")
    now = utcnow()
    return Q(value=value, unit="ratio", as_of=now[:10],
             prov=Provenance(source_name=f"user assumption ({source})",
                             source_url="cli://--wacc", retrieved_at=now,
                             method="normalized", confidence="low",
                             note="USER ASSUMPTION, not observed"),
             label="wacc")


def _fmt(q: Q) -> str:
    if q.is_na:
        return "[dim]n/a to this business model[/dim]"
    if not q.ok:
        return f"[yellow]insufficient data[/yellow] ({q.reason})"
    v = q.value
    if q.unit == "ratio":
        return f"{v:.2%}"
    if q.unit in ("x", "years", "score", "flag", "count"):
        return f"{v:,.2f}"
    return f"{v:,.0f} {q.unit}"


# ---------------------------------------------------------------------------


@app.command()
def init() -> None:
    """Create the database and directories."""
    _load_env()
    with db.session() as con:
        pass
    for k in ("raw",):
        config.path(k).mkdir(parents=True, exist_ok=True)
    console.print(f"[green]initialized[/green] db={config.path('db')} "
                  f"raw={config.path('raw')}")


@app.command()
def doctor() -> None:
    """Report which sources are reachable and which keys are set."""
    _load_env()
    from . import providers

    t = Table(title="ledger doctor", show_lines=False)
    t.add_column("source"); t.add_column("configured"); t.add_column("note")
    for name in providers.configured("fundamentals"):
        p = providers.get(name)
        t.add_row(name, "[green]yes[/green]" if p.available() else "[red]no[/red]",
                  "keyless, LOW CONFIDENCE" if name == "yahoo"
                  else f"set {name.upper()}_API_KEY in .env")
    t.add_row("boc", "[green]yes[/green]", "keyless")
    t.add_row("statcan", "[green]yes[/green]", "keyless")
    t.add_row("eia", "[green]yes[/green]" if config.api_key("eia") else "[red]no[/red]",
              "set EIA_API_KEY in .env")
    t.add_row("sec", "[green]yes[/green]" if config.contact() else "[red]no[/red]",
              "set LEDGER_CONTACT in .env (SEC fair-access policy)")
    t.add_row("anthropic",
              "[green]yes[/green]" if os.environ.get("ANTHROPIC_API_KEY") else "[red]no[/red]",
              "interpretation layer only; writes no numbers")
    console.print(t)

    paid = providers.paid_available("fundamentals")
    if len(paid) < 2:
        console.print("\n[yellow]WARNING[/yellow] the reconciliation gate needs two "
                      "independent sources. With "
                      f"{len(paid)} paid source(s) configured, rows will be "
                      "BLOCKED rather than scored (require_second_source=true).")
    deck = config.load_deck()
    for w in deck.warnings():
        console.print(f"[yellow]deck[/yellow] {w}")


@app.command()
def deck() -> None:
    """Print the mid-cycle commodity deck and its staleness."""
    d = config.load_deck()
    t = Table(title=f"commodity deck v{d.version} (USER ASSUMPTIONS, not fetched)")
    for c in ("commodity", "value", "unit", "set on", "age (days)", "status"):
        t.add_column(c)
    for name, p in sorted(d.prices.items()):
        age = p.age_days()
        status = ("[red]UNSET[/red]" if p.value is None
                  else "[yellow]STALE[/yellow]" if p.is_stale()
                  else "[green]ok[/green]")
        t.add_row(name, "-" if p.value is None else f"{p.value:,.2f}", p.unit,
                  p.set_on or "-", "-" if age is None else str(age), status)
    console.print(t)
    console.print("[dim]Unset commodities return null for mid-cycle metrics. "
                  "The system will not invent a price level.[/dim]")


@app.command()
def macro() -> None:
    """Fetch and print the macro panel (BoC, StatCan, EIA)."""
    _load_env()
    from .providers.macro import macro_panel

    t = Table(title="macro panel")
    for c in ("series", "value", "unit", "as of", "source"):
        t.add_column(c)
    for k, q in sorted(macro_panel().items()):
        t.add_row(k, _fmt(q), q.unit, q.as_of or "-",
                  q.prov.source_name if q.prov else "-")
    console.print(t)


@app.command()
def refresh(
    tickers: list[str] = typer.Argument(..., help="e.g. RY.TO CNQ.TO"),
    wacc: Optional[float] = typer.Option(None, help="Discount rate, e.g. 0.09. Required for valuation."),
    force: bool = typer.Option(False, help="Ignore cache and refetch."),
    years: int = typer.Option(10, help="Annual periods to pull."),
    quarters: int = typer.Option(8, help="Quarterly periods to pull."),
    providers_csv: Optional[str] = typer.Option(None, "--providers",
                                                help="Override provider order."),
) -> None:
    """Fetch, reconcile, compute and store. Idempotent and resumable."""
    _load_env()
    from .refresh import refresh_ticker

    names = providers_csv.split(",") if providers_csv else None
    d = config.load_deck()
    w = _wacc(wacc, "--wacc")
    with db.session() as con:
        for tk in tickers:
            r = refresh_ticker(tk, wacc=w, deck=d, providers=names, force=force,
                               annual_years=years, quarters=quarters, con=con)
            _report_refresh(r)


def _report_refresh(r: Any) -> None:
    console.print(f"\n[bold]{r.ticker}[/bold]  run={r.run_id}")
    for s in r.sources:
        console.print(f"  {'[green]ok[/green]' if s.ok else '[red]--[/red]'} {s.detail}")
    for e in r.errors:
        console.print(f"  [red]error[/red] {e}")
    if r.reconciliation:
        rec = r.reconciliation
        if rec.blocked:
            console.print(f"  [red]BLOCKED[/red] {rec.blocking_message()}")
        else:
            console.print(f"  [green]reconciled[/green] flags={rec.flags or 'none'}")
        for c in rec.checks:
            mark = {"agree": "[green]=[/green]", "disagree": "[red]![/red]",
                    "uncheckable": "[yellow]?[/yellow]"}[c.status]
            d = "-" if c.disagreement_pct is None else f"{c.disagreement_pct:.2f}%"
            console.print(f"    {mark} {c.anchor}: {c.primary_source} vs "
                          f"{c.secondary_source}, {d} apart")
    if r.analysis:
        cov = r.analysis.coverage()
        console.print(f"  coverage: {cov['populated']}/{cov['total']} populated, "
                      f"{cov['null']} null, {cov['not_applicable']} n/a")


@app.command()
def show(ticker: str, lens: Optional[str] = None) -> None:
    """Print stored metrics for one ticker with as_of and coverage."""
    _load_env()
    from .audit import audit_rows, audit_summary

    with db.session() as con:
        s = audit_summary(con, ticker)
        rows = [r for r in audit_rows(con, ticker, include_statements=False)
                if not lens or lens.lower() in r["metric"].lower()]
    if not rows:
        console.print(f"[yellow]no data for {ticker}[/yellow] -- run "
                      f"`ledger refresh {ticker}` first")
        raise typer.Exit(1)
    rec = s["reconciliation"]
    if rec and rec["blocked"]:
        console.print(f"[red]RECONCILIATION_FAILED[/red] {rec['reason']}\n"
                      "[red]This row is blocked from scoring.[/red]")
    t = Table(title=f"{ticker}  as_of={s['as_of']}  "
                    f"{s['metrics_populated']}/{s['metrics_total']} populated")
    for c in ("metric", "value", "unit", "as of", "method", "source"):
        t.add_column(c)
    for r in rows:
        val = (f"{r['value']:,.4g}" if r["value"] is not None
               else f"[yellow]insufficient data[/yellow]")
        t.add_row(r["metric"], val, r["unit"] or "", r["as_of"] or "-",
                  r["method"] or "-", (r["source_name"] or "-")[:24])
    console.print(t)
    console.print(f"[dim]audit: ledger audit {ticker}[/dim]")


@app.command()
def audit(ticker: str,
          metric: Optional[str] = typer.Option(None, help="Filter by name."),
          statements: bool = typer.Option(False, help="Include raw statement fields."),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Every field with its source URL and retrieval timestamp."""
    _load_env()
    from .audit import audit_rows, audit_summary

    with db.session() as con:
        s = audit_summary(con, ticker)
        rows = audit_rows(con, ticker, include_statements=statements,
                          metric_filter=metric)
    if as_json:
        console.print_json(json.dumps({"summary": s, "rows": rows}, default=str))
        return
    if not rows:
        console.print(f"[yellow]no data for {ticker}[/yellow]")
        raise typer.Exit(1)
    console.print(f"[bold]{ticker}[/bold] as_of={s['as_of']} "
                  f"populated={s['metrics_populated']}/{s['metrics_total']} "
                  f"low_confidence_values={s['low_confidence_values']}")
    console.print(f"sources: {', '.join(s['distinct_sources']) or 'none'}")
    if s["reconciliation"] and s["reconciliation"]["blocked"]:
        console.print(f"[red]BLOCKED:[/red] {s['reconciliation']['reason']}")
    for r in rows:
        head = f"\n[bold]{r['metric']}[/bold] = "
        head += (f"{r['value']:,.6g} {r['unit']}" if r["value"] is not None
                 else f"[yellow]{r['status']}[/yellow]")
        console.print(head + f"   as_of={r['as_of']}")
        if r["value"] is None and r["missing"]:
            for m in r["missing"]:
                console.print(f"    [yellow]missing:[/yellow] {m}")
        if r["source_url"]:
            console.print(f"    source: {r['source_name']}  {r['source_url']}")
            console.print(f"    retrieved: {r['retrieved_at']}  "
                          f"method={r['method']}  confidence={r['confidence']}")
        if r["formula"]:
            console.print(f"    formula: {r['formula']}")
        if r["input_provenance_ids"]:
            console.print(f"    inputs: {', '.join(r['input_provenance_ids'])}")
        if r["raw_payload"]:
            console.print(f"    raw payload: {r['raw_payload']}")


@app.command()
def trace(ticker: str, metric: str) -> None:
    """Full provenance trace of one metric, including how it moved over time."""
    _load_env()
    from .audit import trace as _trace

    with db.session() as con:
        console.print_json(json.dumps(_trace(con, ticker, metric), default=str))


@app.command()
def rank(lens: str = typer.Argument(..., help="e.g. ebit_ev_yield, incremental_roic"),
         tickers: Optional[list[str]] = typer.Argument(None)) -> None:
    """Rank a cohort by one lens. Never a composite by default."""
    _load_env()
    from .scoring import LENSES, rank_by

    if lens not in LENSES:
        console.print(f"[red]unknown lens[/red]. available: {sorted(LENSES)}")
        raise typer.Exit(1)
    key = LENSES[lens][0]
    with db.session() as con:
        names = tickers or [r["ticker"] for r in db.list_companies(con)]
        rows = []
        for tk in names:
            co = con.execute("SELECT * FROM companies WHERE ticker=?", (tk,)).fetchone()
            m = con.execute(
                "SELECT * FROM metrics WHERE ticker=? AND metric=? "
                "ORDER BY last_seen DESC LIMIT 1", (tk, key)).fetchone()
            rec = db.get_reconciliation(con, tk)
            q = (Q(value=m["value"], unit=m["unit"] or "", as_of=m["as_of"],
                   prov=None, reason=m["reason"],
                   missing=tuple(json.loads(m["missing"] or "[]")))
                 if m else Q.null("not_fetched", missing=(key,)))
            rows.append({"ticker": tk, "schema": co["schema_kind"] if co else "industrial",
                         "metrics": {key: q},
                         "blocked": (rec["reason"] if rec and rec["blocked"] else None)})
    try:
        ranked = rank_by(lens, rows)
    except AssertionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    t = Table(title=f"rank by {lens} ({ranked.to_dict()['direction']})")
    for c in ("#", "ticker", "value", "as of"):
        t.add_column(c)
    for r in ranked.rows:
        t.add_row(str(r["rank"]), r["ticker"], f"{r['value']:,.4g}", r["as_of"] or "-")
    console.print(t)
    for e in ranked.excluded:
        console.print(f"[yellow]excluded[/yellow] {e['ticker']}: {e['reason']} "
                      f"{'; '.join(e.get('missing', []))[:100]}")


@app.command()
def interpret(ticker: str, wacc: Optional[float] = typer.Option(None)) -> None:
    """Generate prose about computed metrics. The model writes no numbers."""
    _load_env()
    from .interpret import interpret as _interpret
    from .refresh import refresh_ticker

    with db.session() as con:
        r = refresh_ticker(ticker, wacc=_wacc(wacc, "--wacc"),
                           deck=config.load_deck(), con=con)
    if not r.analysis:
        console.print("[red]no analysis[/red]")
        raise typer.Exit(1)
    out = _interpret(r.analysis)
    if not out.accepted:
        console.print("[red]interpretation refused[/red]")
        for v in out.violations:
            console.print(f"  violation: {v}")
        for n in out.unverified_numbers:
            console.print(f"  [red]model stated a number the code did not "
                          f"compute: {n}[/red]")
        raise typer.Exit(1)
    console.print(out.prose)


@app.command("verify-filing")
def verify_filing(
    ticker: str,
    filing: Path = typer.Option(..., help="JSON filing fixture (see ledger.verify)."),
    tolerance: float = typer.Option(0.5, help="Match tolerance, percent."),
    wacc: Optional[float] = typer.Option(None),
) -> None:
    """Acceptance test 1: reconcile computed metrics against a primary filing.

    Prints the comparison table and exits non-zero on any mismatch.
    """
    _load_env()
    from .refresh import refresh_ticker
    from .verify import compare_to_filing, computed_figures, load_filing

    doc = load_filing(filing)
    with db.session() as con:
        r = refresh_ticker(ticker, wacc=_wacc(wacc, "--wacc"),
                           deck=config.load_deck(), con=con)
    if r.company is None:
        console.print(f"[red]no data pulled for {ticker}[/red]")
        for s in r.sources:
            console.print(f"  {s.detail}")
        raise typer.Exit(2)
    cmp = compare_to_filing(computed_figures(r.company, r.analysis), doc,
                            tolerance_pct=tolerance)
    console.print(cmp.table())
    if not cmp.reconciles:
        raise typer.Exit(1)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the read-only dashboard."""
    _load_env()
    import uvicorn

    uvicorn.run("ledger.web.app:app", host=host, port=port, reload=False)


@app.command()
def selftest() -> None:
    """Run the acceptance tests that need no network."""
    import subprocess

    r = subprocess.run([sys.executable, "-m", "pytest", "-q",
                        str(config.ROOT / "tests")], cwd=config.ROOT)
    raise typer.Exit(r.returncode)


if __name__ == "__main__":
    app()
