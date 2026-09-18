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

---

## Run it

```bash
python -m venv .venv
./.venv/bin/pip install -r requirements.txt

./.venv/bin/python -m marketengine run
```

That is the whole thing. yfinance needs no credentials, no API key and no
account. Takes about 30 seconds from cold, and writes:

```
data/raw/yfinance/<SYM>.parquet     one file per symbol, as downloaded
data/curated/baseline/prices.parquet  the cleaned, aligned panel
data/outputs/baseline/*.csv         10 analytics tables
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
python -m marketengine query "SELECT symbol, count(*) FROM prices_baseline GROUP BY 1"
```

`analyze` is the one used most while iterating: it re-reads the curated
Parquet and touches no network, so changing a metric or a chart costs no
API calls and no waiting.

### Tests

```bash
./.venv/bin/pytest -q      # 93 passed, 2 skipped, ~1 second, no network
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
 clean.py ........ align to a calendar, gap policy, quality report
      |
      v
 store.py ........ Parquet + DuckDB views
      |
      v
 analytics.py .... pure functions, no I/O
      |
      +--> plots.py .... 5 figures
      +--> report.py ... summary.md + run_manifest.json
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

Timezones are dropped deliberately: a daily bar labels a *session*, not an
instant, and two vendors stamping the same session 00:00Z and 05:00Z would
refuse to join.

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

3. **Gaps are forward-filled for at most `max_ffill_days` sessions** (3 by
   default) and **every filled bar is flagged** in the `filled` column, so
   any downstream analysis can exclude them. Longer gaps are *dropped*
   rather than filled — a long forward fill invents a flat price, which
   reads as zero volatility, and zero volatility is a much worse lie than a
   missing row. A consequence: a late listing starts at its own first real
   session instead of at a fabricated flat stretch.

4. **Volume is never forward-filled.** A carried bar has unknown volume and
   NaN is what unknown means. A carried volume would invent trading
   activity.

5. **Nothing is dropped for being surprising.** Extreme moves, zero-volume
   sessions, stale price runs and non-positive prices are *counted* in
   `data_quality.csv` and kept in the data. A 60% single-day move is
   usually real — earnings, a biotech readout — and deleting it is the
   actual error. Rows are only ever dropped for having no usable price at
   all, or for sitting in a gap too long to fill.

6. **Annualisation.** Returns annualise **geometrically** (CAGR); volatility
   scales by `sqrt(252)`; alpha annualises **arithmetically** (daily alpha ×
   252) because alpha is an average per-period abnormal return, not a
   compounded path. The risk-free rate is de-annualised geometrically,
   `(1+rf)^(1/252) - 1`, not divided by 252.

7. **The risk-free rate is a flat 2% assumption.** It affects Sharpe,
   Sortino and CAPM alpha. Swap in a real T-bill series before quoting a
   Sharpe ratio anywhere it matters.

8. **Pairwise windows.** A symbol with less history than the benchmark is
   compared against it only over the sessions they share, and
   `overlap_with_benchmark` in `metrics_summary.csv` says how many that
   was. The full-period correlation matrix blanks any pair sharing fewer
   than `min_history_days` (250) sessions, because a correlation from a
   30-day overlap is confidently wrong.

9. **Capture ratios use geometric mean per-day returns**, not the textbook
   ratio of compounded totals. Over a sample this long the textbook version
   saturates and stops discriminating: SPY's up days compound to roughly
   +1,400,000% and its down days to roughly -99.99%, which made every
   leveraged symbol report a down-capture of exactly 1.00 and gave NVDA an
   up-capture of 115,008. Geometric means remove the horizon from the
   statistic.

10. **`trading_days_per_year: 252`** is a convention, not a count of the
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
`metrics_summary` (~28 columns per symbol), `data_quality`,
`ingest_report`.

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
