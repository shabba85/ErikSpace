# ledger

A local, single-user equity research instrument for Canadian (TSX/TSXV) and
cross-listed North American equities.

Runs on your machine, holds your keys, writes to your disk.

## The rule that shapes everything

**No number exists in this system unless it can name where it came from.**

Every numeric field carries `{value, unit, as_of, source_name, source_url,
retrieved_at, method}` where `method ∈ {reported, derived, normalized}`. Derived
values record their formula and the provenance ids of every input.

If an input is missing, the output is `None`. `None` propagates. The UI renders
"insufficient data" and names the missing field. Sector defaults, peer-median
fills and remembered figures do not exist anywhere in the codebase — there is no
code path that produces one.

This is enforced structurally, not by convention:

- `Q` (the quantity type) has no constructor for a populated value that does not
  take a `Provenance`. `Q.reported` is only reachable from an adapter that
  actually fetched something; everything downstream goes through `derive`.
- Bank and industrial metrics live in *separate schemas*. A bank has no
  `gross_profit` field to populate, so no code can print a bank's gross margin.
- `scripts/check_no_hardcoded_constants.py` fails the build if a numeric literal
  encoding a financial judgement appears in the method layer.
- The LLM layer scans its own output and refuses any generation containing a
  number the code did not compute.

## Quick start

```bash
uv venv && uv pip install -e ".[dev,interpret]"
cp .env.example .env          # add your keys
ledger init
ledger doctor                 # what's reachable, what's missing
ledger refresh RY.TO --wacc 0.09
ledger show RY.TO
ledger audit RY.TO            # every field, with its source URL
ledger serve                  # read-only dashboard on :8000
```

`ledger doctor` will warn you if fewer than two paid providers are configured:
the reconciliation gate needs an independent second opinion, and with only one
source every row is **blocked rather than scored**.

## Commands

| command | what it does |
|---|---|
| `ledger init` | create the database and directories |
| `ledger doctor` | which sources are reachable, which keys are set, deck staleness |
| `ledger refresh TICKER...` | fetch → reconcile → compute → store (idempotent, resumable) |
| `ledger show TICKER` | stored metrics with as_of and coverage |
| `ledger audit TICKER` | every field with source URL and retrieval timestamp |
| `ledger trace TICKER METRIC` | full provenance trace + how the figure moved between refreshes |
| `ledger rank LENS [TICKERS]` | rank a cohort by one lens |
| `ledger deck` | the mid-cycle commodity deck and its age |
| `ledger macro` | BoC / StatCan / EIA panel |
| `ledger verify-filing TICKER --filing f.json` | reconcile computed metrics to a primary filing |
| `ledger interpret TICKER` | prose about computed metrics (the model writes no numbers) |
| `ledger serve` | read-only dashboard |
| `ledger selftest` | run the acceptance suite |

## Architecture

```
providers/     adapters: fmp, eodhd, tiingo, yahoo (fallback, low-confidence),
               macro (BoC Valet, StatCan WDS, EIA), filings (SEDAR+, SEC EDGAR)
http.py        rate limiting, retry/backoff, on-disk cache, immutable raw archive
provenance.py  Q, Provenance, derive/combine -- the core invariant
model.py       canonical schemas: industrial | bank | insurer (disjoint by design)
reconcile.py   the three-anchor gate
methods/       pure functions: cashflow, returns, valuation, cyclical, banks,
               insurers, forensics, capital, portfolio
analyze.py     routes a company to its admissible metric set
refresh.py     the pipeline (CLI only -- the UI never fetches)
db.py          SQLite, metrics keyed (ticker, metric, as_of) so history accumulates
interpret.py   the only LLM call, with a numeric-fabrication guard
web/           read-only dashboard
```

Raw payloads land in `data/raw/{source}/{ticker}/{retrieved_at}.json`, written
read-only and never overwritten, so every computed figure is reproducible from
the original bytes. Every HTTP call — including failures — is appended to
`data/http.log.jsonl`. API keys are redacted before anything is recorded.

## What it computes

**Cash-flow truth.** Owner earnings (Buffett's definition) reported as a *band*,
because maintenance capex is not disclosed: it is estimated both by the D&A proxy
and by Greenwald's PP&E-to-sales method, and both ends are shown. FCF conversion
mean and dispersion over five years.

**Returns on capital.** NOPAT on a *cash* tax rate, not statutory and not the
book effective rate. ROIC, reinvestment rate, and incremental ROIC over a
trailing window — which refuses to compute on a shrinking capital base, where the
ratio is a trap rather than a signal. Reinvestment × incremental ROIC gives the
intrinsic compounding rate, held directly against the market-implied growth.

**Valuation, expectations first.** The headline is a reverse DCF: *"the market is
pricing 7.4% FCF growth for 10 years, then 2% in perpetuity, discounted at 9%."*
Your job is to judge that claim, not to argue with a fair-value estimate. Also
Greenwald EPV, reproduction value and franchise value (always all three), the
Acquirer's Multiple and EBIT/EV. Forward DCF exists only as a scenario tool,
tagged `normalized` with its assumptions in the formula string.

**Cyclicals.** Energy and Materials are never scored on TTM earnings. Earnings
are restated at a user-set mid-cycle deck using the company's **own disclosed
MD&A sensitivities** — not a sector beta. TTM and mid-cycle multiples always
appear side by side with the difference labelled as cyclical distortion. Plus
reserve life index, corporate breakeven WTI (the price at which cash flow covers
capex and the dividend), and net debt / mid-cycle EBITDA.

**Financials get a different model.** Banks and insurers cannot touch Altman Z,
ROIC, EV, gross margin or current ratio — those fields do not exist in their
schema. Banks get ROTCE, P/TBV, CET1, PCL as a share of average loans *split
between performing and impaired* (a bank provisioning on performing loans is
making a forecast; one provisioning on impaired loans is reporting an outcome),
NIM, efficiency ratio, deposit growth and mix. The valuation anchor is an OLS of
P/TBV on ROTCE across the peer set: **the residual is the signal**, and it is
what a generic P/E screen cannot see. Insurers get book value + CSM, LICAT and
core ROE.

**Forensics.** Piotroski F, Beneish M, Sloan accruals, Montier C, Altman Z with
the correct variant per sector. A composite refuses to report a partial total: a
6-of-9 Piotroski printed as "6" is indistinguishable from a real 6.

**Capital allocation.** Buyback yield *and the multiple paid* — retiring stock
above intrinsic value is destruction reported as a return. Dividend coverage by
FCF, never by EPS. Debt maturity ladder against the current refinancing rate.

**Portfolio.** Look-through factor exposure: what share of portfolio *earnings*
depends on Canadian household credit, on oil, on the level of rates. A four-name
TSX portfolio is often one bet wearing four tickers, and this quantifies it.
Fractional Kelly on probabilities you supply. Base-rate context for any growth
assumption above 10% — from a table you supply, never invented.

## Scoring

Composite scoring is **off by default** and is never the headline. Ranking is by
one lens you select. Any composite shows its full input vector and refuses to
compute above 20% null inputs. Ranking across the financial / non-financial
boundary raises rather than silently sorting: the two score vectors are disjoint
and have no common scale.

## The interpretation layer

The only place an LLM appears. It receives computed metrics with provenance and
returns prose in five sections: what the market is pricing in (as a falsifiable
claim), the variables that decide whether that is right, the strongest bear case,
what would change the assessment, and what could not be computed.

It may not produce ratings, price targets, or confident language over null-heavy
inputs. Every generation is scanned for numeric tokens; any figure the code did
not compute causes a rejection and a retry with the violation fed back, and a
second failure returns nothing at all.

## Acceptance tests

```bash
ledger selftest        # or: pytest
```

| # | requirement | status |
|---|---|---|
| 1 | computed metrics reconcile to the latest annual filing | machinery tested; **run `ledger verify-filing` against a real filing on your machine** |
| 2 | deleting an input nulls dependents, names the field, substitutes no default | passing |
| 3 | a bank has no Altman Z, ROIC, EV or gross margin — absent, not zero | passing |
| 4 | an oil producer shows TTM and mid-cycle, with deck assumptions printed | passing |
| 5 | a >2% provider disagreement blocks and flags the row | passing |
| 6 | two runs on unchanged data are byte-identical | passing |
| 7 | no hardcoded financial constants outside config | passing |

For test 1, copy `tests/fixtures/filings/EXAMPLE.json.template`, fill it from the
issuer's own statements on SEDAR+, and run:

```bash
ledger verify-filing XYZ.TO --filing tests/fixtures/filings/XYZ.json
```

## Configuration

| file | contents |
|---|---|
| `.env` | API keys. Never committed. |
| `config/settings.yaml` | provider order, reconciliation tolerance, rate limits, cache TTLs |
| `config/deck.yaml` | mid-cycle commodity deck — **user assumptions**, each stamped with the date it was set and warned about once stale |
| `config/models.yaml` | published model coefficients (Altman, Beneish, Piotroski, Montier) with citations |
| `config/factors.yaml` | segment → macro factor map for look-through exposure |

Nothing in `config/deck.yaml` is presented as observed data. Deck-normalized
outputs carry `method='normalized'`, the deck version, and a staleness warning.

## Scheduled refresh

See `deploy/crontab.example` (Linux) and `deploy/com.ledger.refresh.plist`
(macOS): macro and prices daily, statements weekly.

## Known limits

- **Adapter shapes are unverified against live endpoints.** The four vendor
  adapters were written to documented response shapes and are tested against
  recorded payloads, not live calls. Run `ledger doctor` and a first
  `ledger refresh` before trusting any of them; a shape drift shows up as
  null fields naming the vendor key that went missing.
- **SEDAR+ is not scraped.** It has no documented public API and its search is
  session-based. The system produces the clickable SEDAR+ record URL for
  manual spot-checking rather than a scraper that silently returns wrong figures
  from a filing. EDGAR *is* queried, for cross-listed 40-F/6-K filers only.
- **The WCS differential has no free authoritative source.** It returns null
  until you configure `macro.wcs_quote_url`. For an oil-sands name this is one of
  the two numbers that determine the valuation, so it is not assumed.
- **No base-rate data ships with the system.** Supply `data/base_rates.csv`;
  every row carries its own citation into the audit log.
- **Deck commodities beyond WTI, WCS and gold are unset.** The fields, units and
  sensitivity plumbing exist for natural gas, copper, uranium (spot and term),
  potash, met coal and lithium. The price levels are yours to set — the system
  will not invent a mid-cycle copper price.
