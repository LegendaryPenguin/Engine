# Rubric evidence index — Milestone 3 (100 pts)

One row per rubric line. `README.md` in this folder is the same evidence
as prose. Paths are relative to the repository root.

## Reusable backtesting workflow and metrics — 30

| The requirement | Where to look |
|---|---|
| Backtester is reusable | `marketengine/backtest.py`: `prepare_books` then `simulate_prepared`. Full-sample `backtest` and chronological `validate` share the same path. |
| Required performance / risk metrics | CAGR, vol, Sharpe, Sortino, max drawdown, hit rate, empirical 95% VaR, holding period, turnover, beta, alpha, trades, gross exposure. `04_validation.md` §2; `06_comparison.csv`; `10_train_summary.csv`; `11_test_summary.csv`. |
| VaR is empirical, not Gaussian | `marketengine/backtest.py` `metrics_row` → `analytics.historical_var`. Named in the report. |
| Runs on a large liquid universe | `config/validation.yml`: 62 tradable names + SPY. `01_config.txt`, `03_validate.txt`. |

## Transaction costs / portfolio state — 17

| The requirement | Where to look |
|---|---|
| Configurable cost / slippage | `commission_bps: 1`, `slippage_bps: 4`, `cost_bps: 5` in `config/validation.yml`. Must sum. Grid `[0, 5, 10, 25]`. |
| Cost sensitivity | Train and test sweeps in `04_validation.md` §4; `08_cost_grid.csv`; `figures/validation_test_costs.png`. |
| Portfolio / trades tracked coherently | Positions and trades per strategy per split under `data/outputs/validation/validation_{train,test}_{positions,trades}_*.csv`. NAV in `validation_{train,test}_nav.csv`. |
| Position / participation caps | `max_weight: 0.15`, `max_participation: 0.02` of 20-day median dollar volume at `$1,000,000` assumed NAV. `strategies.apply_portfolio_caps`; tests in `tests/test_validate.py`. Clipped mass stays cash. |

## Out-of-sample validation — 23

| The requirement | Where to look |
|---|---|
| Clear time-based train / untouched test | Chronological 80/20 on decision dates after warmup. Boundary **2024-06-20**. `09_split.csv`, `03_validate.txt`, `04_validation.md` §1. |
| No shuffle | `marketengine/validate.py` `chronological_split`. Test fails if any train date ≥ boundary (`tests/test_validate.py`). |
| Train and test side by side | `04_validation.md` §2; `06_comparison.csv`; `figures/validation_train_test_cagr.png`. |
| Overfitting reported, not omitted | Test Sharpe >40% below train → flag. `07_overfit_flags.csv`; §3 of the report. |
| Parameters not retuned on test | Baseline rules fixed in `config/validation.yml`. Separate train-only search (`marketengine/search.py`): 42 trials logged, winner by train Sharpe, sealed test once — `03b_search.txt`, `05b_search.md`, `12_search_trials.csv`. |
| Search size + IS→OOS drop reported | `13_search_summary.csv`: n_trials, best train Sharpe/CAGR, same config’s test return/Sharpe/max DD vs equal-weight. |

## Bias / limitation analysis — 20

| The requirement | Where to look |
|---|---|
| Look-ahead / leakage | `04_validation.md` §5.1; truncation and next-open tests in `02_tests.txt` / `tests/test_backtest.py`. |
| Overfitting | §5.2 and §3; overfit flags. Train-only search winner (Sharpe 1.13 IS → 0.41 OOS) in `05b_search.md`. |
| Survivorship / universe construction | §5.3 and §5.7. 62 survivors, not a point-in-time membership list. |
| Unstable parameters / regime | §5.4. |
| Cost-model and price-basis risk | §5.5–5.6. |
| Multiple testing | §5.8. |

## Reproducibility and documentation — 10

| The requirement | Where to look |
|---|---|
| Experiments can be rerun | `capture.py`; commands in `04_validation.md` §7 and root `README.md`. |
| Key parameters / data periods documented | Boundary date, train fraction, cost, caps, universe, config SHA-256 in `01_config.txt`, `09_split.csv`, report §1 and §7. |
| Evidence from real runs | Numbered `0x_*.txt` files in this folder; exit codes recorded. |
