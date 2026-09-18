# Rubric evidence index — Milestone 1 (50 pts)

One row per rubric line, pointing at the file that shows it. `README.md` in
this folder explains *why* each choice was made; this is just the map.
Paths are relative to the repository root.

## Data acquisition and reproducibility — 12

| The requirement | Where to look |
|---|---|
| Daily price/volume from an approved API | `submission/milestone1/04_run_cold.txt` — 32,395 bars, 11 symbols, yfinance |
| Configurable universe | `01_config.txt` (resolved config) ← `config/default.yml`; `marketengine/config.py` |
| Second provider, same pipeline | `config/alpaca.yml`, `marketengine/providers/alpaca_provider.py`, `tests/test_alpaca.py` |
| Reproducible: config hash + versions + coverage | `10_run_manifest.json` |
| Incremental, idempotent | `03_ingest_dry_run.txt`, `04_run_cold.txt` (32,395 rows) vs `05_run_warm.txt` (0 rows) |
| Restatement handling | `marketengine/ingest.py` (5-day overlap) + `marketengine/store.py` (`keep="last"`) |
| Deterministic from a clean checkout | `capture.sh` regenerates every file here |

## Cleaning, alignment and storage — 10

| The requirement | Where to look |
|---|---|
| Raw kept immutable | `09_outputs_listing.txt` → `data/raw/yfinance/*.parquet` |
| Cleaned analytical table | `data/curated/baseline/prices.parquet`; `marketengine/clean.py` |
| Quality-event log (per-bar, with values) | `08_data_quality.txt` — 16 events; `data/outputs/baseline/quality_events.csv` |
| Per-symbol quality report | `08_data_quality.txt` — coverage, flag counts, short-history/unadjusted checks |
| Alignment to one calendar | `07_query.txt` — 11 symbols × 2,945 bars, identical date range |
| No fabricated prices | `07_query.txt` — `forward_filled = 0` for every symbol; `max_ffill_days: 0` |
| Flag rather than delete | `08_data_quality.txt` — 16 flagged, 0 quarantined; `flags` column in the panel |
| Quarantine rule for impossible bars | `marketengine/clean.py` `_ohlc_inconsistent`; `tests/test_clean.py` quarantine group |
| Reproducible storage format | Parquet + DuckDB views; `07_query.txt` is SQL over the real files |

## Analytics and benchmark calculations — 12

| The requirement | Where to look |
|---|---|
| Daily returns | `11_summary.md` §1; `data/outputs/baseline/daily_returns.csv` |
| Cumulative returns | `11_summary.md` §1; `cumulative_returns.csv`; `figures/growth_of_one.png` |
| Rolling volatility (21/63/252) | `rolling_volatility_21d.csv`, `_63d`, `_252d`; `figures/rolling_volatility.png` |
| Correlations | `11_summary.md` §3; `correlation_matrix.csv`; `figures/correlation_heatmap.png`; `rolling_corr_63d_vs_SPY.csv` |
| Benchmark-relative performance | `11_summary.md` §2 — beta, alpha, R², tracking error, information ratio, active return, up/down capture; `figures/relative_to_benchmark.png` |
| Risk metrics | `11_summary.md` §1 — Sharpe, Sortino, max drawdown, empirical 5% VaR |
| Total-return basis stated | returns from `adj_close`; README assumption 1 |
| Correctness, not self-snapshots | `02_tests.txt` — closed-form tests per metric, incl. regressions for the Sharpe-on-constant-series and capture-ratio bugs |

## Code organisation and functionality — 10

| The requirement | Where to look |
|---|---|
| Layered modules, one responsibility each | root `README.md` layer diagram; `marketengine/` (`config`, `providers/`, `ingest`, `store`, `clean`, `flags`, `analytics`, `plots`, `report`, `bench`, `pipeline`, `cli`) |
| Only the ingest layer touches the network | `marketengine/analytics.py` does no I/O; `02_tests.txt` runs with zero network calls |
| Every stage independently runnable | `01_config.txt`; `ingest`/`clean`/`analyze`/`report`/`bench`/`query`/`config` subcommands |
| Eager config validation | `marketengine/config.py`; `tests/test_config.py` |
| Errors are legible, not tracebacks | `marketengine/cli.py` error handling |
| Tests | `02_tests.txt` — 121 passed, 2 skipped, 1.25s |
| Secrets not in config | `.gitignore`, `.env.example`, `marketengine/providers/alpaca_provider.py` |
| Speed | `06_latency_warm.txt` / `06_latency_cold.txt` — stage timings, rows/sec, fetch-latency percentiles |

## README and explanation — 6

| The requirement | Where to look |
|---|---|
| How to run it | root `README.md` — Quickstart, every subcommand |
| Assumptions that change the numbers | root `README.md` — 11 numbered assumptions, each with its alternative; regenerated into `11_summary.md` §6 |
| Data-quality policy explained | root `README.md` assumptions 3–6; `marketengine/clean.py` docstring |
| Known limitations stated | root `README.md` — daily bars only, survivorship bias, flat 2% risk-free rate, overlap requirement |
| Where it goes next | `ROADMAP.md` — phases 2–9 with exit criteria |
