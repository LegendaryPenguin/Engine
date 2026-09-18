# MarketEngine

A reproducible daily market-data and analytics pipeline. Point it at a list
of tickers and it downloads the history, cleans and aligns it, computes
returns / rolling volatility / correlations / benchmark-relative
performance, and writes out CSVs, five figures and a Markdown report.

EECS 692 Technical Milestone 1. The layers here are meant to be the data
foundation for the later milestones, so the split between "get the data"
and "do maths on the data" is enforced rather than incidental: only
`ingest.py` touches the network, and everything downstream reads Parquet
off disk.

**Latest run: [`reports/baseline/summary.md`](reports/baseline/summary.md)** —
11 symbols, 2015-01-02 → 2026-09-18, 32,395 bars. Committed so the output
is readable without running anything.

**Grading this?** [`submission/milestone1/`](submission/milestone1/) is
captured terminal output from real runs — tests, a cold run, a warm run,
latency, SQL over the curated panel, the quality event log.
[`RUBRIC.md`](submission/milestone1/RUBRIC.md) in that folder maps each
rubric line to the file that shows it.
[`ROADMAP.md`](ROADMAP.md) is where this goes next.

---

## Run it

```bash
python -m venv .venv
./.venv/bin/pip install -r requirements.txt

./.venv/bin/python -m marketengine run
```

That is the whole thing. yfinance needs no credentials, no API key and no
account. A full cold run — download eleven years for eleven symbols, clean
it, compute everything, draw the figures, write the report — takes about two
seconds and produces:

```
data/raw/yfinance/<SYM>.parquet     one file per symbol, as downloaded
data/curated/baseline/prices.parquet  the cleaned, aligned panel
data/outputs/baseline/*.csv         13 analytics tables (incl. the quality event log)
data/marketengine.duckdb            SQL views over the above
reports/baseline/summary.md         the report
reports/baseline/figures/*.png      5 figures
reports/baseline/run_manifest.json  exactly what produced all of it
```

### Other commands

```bash
python -m marketengine config                # resolved config + every output path
python -m marketengine ingest --dry-run      # what WOULD be downloaded, per symbol
python -m marketengine ingest                # download only what is missing
python -m marketengine clean                 # rebuild the curated panel from raw
python -m marketengine analyze               # recompute metrics + figures only
python -m marketengine run --refresh         # re-download the full window
python -m marketengine bench                 # stage timings, fetch latency, data freshness
python -m marketengine bench --no-network    # compute only, no API calls
python -m marketengine query "SELECT symbol, count(*) FROM prices_baseline GROUP BY 1"
```

`analyze` is the one used most while iterating: it re-reads the curated
Parquet and touches no network, so changing a metric or a chart costs no
API calls and no waiting.

### Tests

```bash
./.venv/bin/pytest -q      # 121 passed, 2 skipped, ~1.3 seconds, no network
```

No test hits the network. The provider adapters are tested against stub
responses; the analytics are tested against hand-computed closed forms
rather than against their own output. The 2 skips are live-API tests that
run only when Alpaca credentials are present in `.env`.

---

## Configuration

Everything lives in [`config/default.yml`](config/default.yml). No ticker,
date, or window is hard-coded anywhere in the package. A different universe
is a different YAML file:

```bash
python -m marketengine --config config/my_universe.yml run
```

`run_id` namespaces the outputs, so two configs coexist without
overwriting each other.

### Switching to Alpaca

```bash
cp .env.example .env    # add ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY
./.venv/bin/python -m marketengine --config config/alpaca.yml run
```

Paper keys work — the pipeline only reads market data and never places an
order. Credentials come from the environment or `.env`, never from the
config file, so configs are safe to commit.

Both providers implement one interface (`providers/base.py`) and return the
same schema, so `provider:` is the only line that changes. Two things about
Alpaca differ under the hood:

- **Alpaca has no adjusted-close column.** Adjustment is a property of the
  *request*, so `fetch` makes two calls — `adjustment=raw` for as-traded
  OHLCV, `adjustment=all` for the total-return close — and merges them on
  (symbol, date). That merge is the thing most likely to be silently wrong,
  so it is tested directly in `tests/test_alpaca.py`.
- **The free tier serves the IEX feed.** Closes track the consolidated tape
  closely, but daily **volume is IEX-only** — roughly 2-3% of the real
  number — and open/high/low are IEX prints. Fine for return analytics;
  wrong for anything volume-based. The caveat is recorded in the run
  manifest so a later milestone cannot forget it.

---

## How it fits together

```
config/default.yml
      |
      v
 config.py ....... parse + validate eagerly, hash the file
      |
      v
 providers/ ...... yfinance | alpaca  ->  one canonical schema   [network]
      |
      v
 ingest.py ....... incremental download  ->  data/raw/           [network]
      |
      v
 clean.py ........ quarantine, align to a calendar, gap policy
      |              flags.py ..... the named quality flags
      |
      v
 store.py ........ Parquet + DuckDB views
      |
      v
 analytics.py .... pure functions, no I/O
      |
      +--> plots.py .... 5 figures
      +--> report.py ... summary.md + run_manifest.json

 bench.py ........ stage timings, fetch latency, data freshness  [network]
```

`analytics.py` takes and returns pandas objects and reads nothing from
disk, which is why every metric in it has a closed-form test.

### The canonical schema

One row per (date, symbol), long rather than wide:

| column | type | notes |
|---|---|---|
| `date` | `datetime64[ns]` | tz-naive, midnight-normalised |
| `symbol` | `string` | uppercase |
| `open` `high` `low` `close` | `float64` | as-traded |
| `adj_close` | `float64` | splits **and** dividends → total return |
| `volume` | `float64` | float, not int, so a missing bar is NaN and not 0 |
| `filled` | `bool` | *(curated only)* this bar was forward-filled |
| `flags` | `string` | *(curated only)* comma-joined flag names, `""` when clean |

Timezones are dropped deliberately: a daily bar labels a *session*, not an
instant, and two vendors stamping the same session 00:00Z and 05:00Z would
refuse to join.

---

## Speed and freshness

```bash
python -m marketengine bench
```

Two different questions, measured separately because they have nothing to
do with each other. Real output, from
[`submission/milestone1/06_latency_warm.txt`](submission/milestone1/06_latency_warm.txt):

```
stage timings
  stage              seconds        rows      rows/sec
  ingest               0.051           -             -
  read_raw             0.013      32,395     2,583,076
  clean                0.062      32,395       520,598
  write_curated        0.011      32,395     2,902,193
  analytics            0.026      32,384     1,225,583
  TOTAL                0.163
```

**Throughput** is not the interesting number. 32,395 rows through clean +
analytics in 0.16 seconds means iteration is free; it does not make the
output any more correct. (`ingest` is 0.051s there because everything was
already on disk. `bench --refresh` re-downloads the whole window and reports
0.731s for 32,395 rows, about 44,000 rows a second.)

**Freshness** is the interesting number, and `bench` reports it in
*sessions behind the benchmark* rather than in seconds. A pipeline that
runs in 180ms on yesterday's close is worse than useless for a
one-day-to-one-month holding period, because the position it sizes today
is sized off a price that has already moved. Each symbol is compared to
SPY's own last session (if SPY has a bar for a date, the market was open),
and SPY is compared to an independent weekday-and-cutoff calendar, which
catches the case where the vendor is stale for *every* symbol at once —
something comparing symbols to each other cannot detect.

`bench` also fires a warning that matters more than any latency figure:

```
  WARNING: the newest bar is for a session that has not closed yet.
           Its close is the last trade so far and its volume is partial.
```

Both vendors serve a partial bar for the session in progress. It is not
wrong, it is not final, and a signal computed on it will change by 16:00.

With a network, `bench` also times five single-symbol requests and reports
`cold_ms` separately from `p50_ms`/`p95_ms`/`max_ms` — the first request
carries TLS, DNS and (for yfinance) a cookie/crumb negotiation, and
averaging it in makes steady state look three times worse than it is.
Percentiles, not a mean: the mean hides the one request in twenty that
takes four seconds, and that is the request that decides whether the daily
job finishes before the open.

**What this does not claim.** Daily bars are not a live feed. The floor on
"delay from live pricing" for a daily close is the vendor's own end-of-day
publication lag, which is minutes to hours after 16:00 New York. Measuring
it honestly is the point: a number that says the close is 18 hours old is
what tells you a same-day decision needs a different data path (see
`ROADMAP.md`, phase 4), not a tuning problem.

---

## Reproducibility

Every run writes `reports/<run_id>/run_manifest.json` containing the config
path and the **SHA-256 of the config file's bytes**, the provider's identity
and adjustment policy, library versions (pandas, numpy, yfinance,
alpaca-py, …), the effective window, per-symbol coverage, and the list of
files written. Given a manifest you can tell whether two sets of numbers
came from the same inputs, which a list of tickers alone cannot tell you.

Ingestion is **incremental and idempotent**. Re-running on the same day
makes zero network calls; re-running tomorrow fetches one session. When
new sessions are needed the request reaches five calendar days *back* into
data already on disk, because vendors restate recent bars (late prints, a
split applied a day late) and `write_raw` merges with `keep="last"` — so a
restated bar propagates instead of staying wrong forever.

Whether the front of a window is already covered is decided from a record
of what was **requested** (`data/raw/<provider>/_requests.json`), not from
the earliest stored bar. Those differ constantly: `window.start:
2015-01-01` is a holiday, so the earliest possible bar is 2015-01-02, and a
symbol that listed mid-window has no earlier bars by definition. Comparing
against the earliest bar re-downloaded eleven years of history on every
single run until this was fixed.

For a byte-frozen run, pin `window.end` to a date. With `end: null` the
window ends at the latest available close, so the output legitimately
changes tomorrow.

---

## Assumptions that change the numbers

These are the choices where a different-but-defensible decision produces
different results. They are repeated at the bottom of every generated
report, because a number without them is not interpretable.

1. **Total return, not price return.** Every return is computed from
   `adj_close`. Using `close` would read each dividend as a price drop and
   understate every dividend payer in the universe. TLT and JNJ would look
   materially worse than they are.

2. **The benchmark defines the trading calendar** (`calendar.align:
   benchmark`). A date SPY did not trade has no benchmark return, so no
   relative statistic exists for it. `intersect` (only dates every ticker
   traded) and `union` (every date any ticker traded) are also available.

3. **Prices are not forward-filled.** `calendar.max_ffill_days: 0`. A
   carried price is a fabricated 0% return on the fill day followed by a
   fabricated real return on the next one, and at a one-day-to-one-month
   holding period that is a directly tradeable-looking artefact rather than
   a rounding error. Filling past a security's last real quote is worse
   still: it manufactures a flat, zero-volatility series for something that
   no longer trades, and every risk statistic then rewards it. Sessions
   with no vendor bar are flagged `MISSING_SESSION` and dropped, so a
   return spanning the hole is honestly a two-day return instead of a
   made-up pair of one-day returns. The mechanism is kept — set
   `max_ffill_days: 2` if you need a gap-free matrix — and filled rows
   carry a `FORWARD_FILLED` flag and a `filled` boolean either way. The
   fill also never runs past a symbol's own last observed bar, because each
   series is reindexed onto the calendar *within its own span*.

4. **Volume is never forward-filled.** A carried bar has unknown volume and
   NaN is what unknown means. A carried volume would invent trading
   activity.

5. **Flag, do not delete.** Extreme moves, zero-volume sessions and stale
   price runs are named on the row itself in the `flags` column, counted
   per symbol in `data_quality.csv`, and logged with their triggering
   values in `quality_events.csv`. All of them stay in the data. A 60%
   single-day move is usually real — earnings, a biotech readout — and
   deleting it is the actual error, and the one that makes a backtest look
   good.

   The only rows that leave the analytical table are **structural
   impossibilities**: a non-positive price, or a bar whose high is below
   its own low, open or close. There is no defensible guess at what the
   real number was, so those rows are quarantined into the event log with
   their values intact and then treated as missing sessions. The OHLC check
   carries a one-part-in-a-million relative tolerance, because vendors
   round to four decimals and a check that fires on rounding is a check
   nobody reads.

6. **An extreme return is `|r| > 50%` OR `|r| > 8` trailing standard
   deviations**, over a 20-session window that *ends before* the bar being
   tested — so an outlier cannot inflate the yardstick it is measured
   against, and the test is one a live strategy could actually run. Both
   arms are needed: the absolute threshold is the only thing that catches a
   decimal-point error on a quiet bond ETF, whose "8 sigma" is a 3% move it
   makes anyway, and the relative one is the only thing that catches a bad
   print on a name that routinely moves 15%. On the baseline run this
   flags 16 rows out of 32,395 — every one of them a real event (NVDA
   +29.8% on 2016-11-11, SPY -3.2% on 2018-10-10), all kept.

7. **Annualisation.** Returns annualise **geometrically** (CAGR); volatility
   scales by `sqrt(252)`; alpha annualises **arithmetically** (daily alpha ×
   252) because alpha is an average per-period abnormal return, not a
   compounded path. The risk-free rate is de-annualised geometrically,
   `(1+rf)^(1/252) - 1`, not divided by 252.

8. **The risk-free rate is a flat 2% assumption.** It affects Sharpe,
   Sortino and CAPM alpha. Swap in a real T-bill series before quoting a
   Sharpe ratio anywhere it matters.

9. **Pairwise windows.** A symbol with less history than the benchmark is
   compared against it only over the sessions they share, and
   `overlap_with_benchmark` in `metrics_summary.csv` says how many that
   was. The full-period correlation matrix blanks any pair sharing fewer
   than `min_history_days` (250) sessions, because a correlation from a
   30-day overlap is confidently wrong.

10. **Capture ratios use geometric mean per-day returns**, not the textbook
   ratio of compounded totals. Over a sample this long the textbook version
   saturates and stops discriminating: SPY's up days compound to roughly
   +1,400,000% and its down days to roughly -99.99%, which made every
   leveraged symbol report a down-capture of exactly 1.00 and gave NVDA an
   up-capture of 115,008. Geometric means remove the horizon from the
   statistic.

11. **`trading_days_per_year: 252`** is a convention, not a count of the
    actual sessions in any given year.

---

## Data source

Default is **yfinance** (Yahoo Finance) — free, no key, deep history.
`auto_adjust=False` is set explicitly so Yahoo does not overwrite `Close`
and drop `Adj Close`, which is its default behaviour and which would leave
the pipeline unable to distinguish as-traded from total-return prices.
Yahoo's API is unofficial and occasionally restates or rate-limits; the
ingest report names any symbol that failed rather than aborting the run.

**Alpaca** is wired and tested as the second source (see above). It is the
provider the later milestones' paper trading will use, so it exists now
rather than being bolted on afterwards.

---

## What is in the output

`reports/baseline/summary.md` has six sections: absolute performance and
risk, performance relative to the benchmark, the correlation matrix, data
quality, the figures, and the assumptions above.

**Tables** (`data/outputs/<run_id>/`):
`prices_adj_close`, `daily_returns`, `cumulative_returns`,
`rolling_volatility_{21,63,252}d`, `correlation_matrix`,
`rolling_corr_63d_vs_SPY`, `relative_cumulative_vs_SPY`,
`metrics_summary` (~28 columns per symbol), `data_quality` (per-symbol flag
counts), `quality_events` (one row per flagged bar, with the value that
triggered it), `ingest_report`.

**Figures** (`reports/<run_id>/figures/`):

| figure | what it answers |
|---|---|
| `growth_of_one.png` | Growth of $1, total return, **log** y-axis — on a linear axis NVDA's 455x flattens every other line into the floor |
| `rolling_volatility.png` | 21-day annualised volatility through time |
| `correlation_heatmap.png` | daily-return correlations, colour limits fixed at ±1 so two runs are comparable |
| `relative_to_benchmark.png` | cumulative performance minus SPY's |
| `risk_return.png` | annualised risk vs return, with SPY's return-per-risk ray |

**Metrics** in `metrics_summary.csv`: total and cumulative return, CAGR,
annualised volatility, Sharpe, Sortino, max drawdown with peak/trough/
recovery dates, 95% historical VaR, hit rate, beta, annualised alpha,
R², correlation to benchmark, tracking error, information ratio, active
return, and up/down capture.

---

## Known limitations

- **Daily bars only.** No intraday, no corporate-action detail beyond the
  adjusted close, no fundamentals.
- **US equities and ETFs.** No FX conversion, so a non-USD listing would be
  wrong in a way nothing here would catch.
- **Survivorship bias.** The universe is a hand-picked list of names that
  exist today. Any backtest built on it inherits that bias, and the fix is
  a point-in-time constituent list, which is not free.
- **Relative metrics need overlap.** A symbol with a short history gets
  NaNs, not an approximation.
- **The 2% risk-free rate is flat** across eleven years in which the actual
  rate went from near zero to over 5%.
