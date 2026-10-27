# Rubric evidence index — Milestone 2 (50 pts)

One row per rubric line, pointing at the file that shows it. `README.md`
in this folder is the same evidence as prose. Paths are relative to the
repository root.

## Strategy definitions — 12

| The requirement | Where to look |
|---|---|
| At least two quantitative strategies, explicit rules or scores | Three, in `config/default.yml` under `strategies:` and in `marketengine/strategies.py` (`RULES`). Momentum (20-day return, skip 1), reversal (5-day return scaled by 20-day volatility), breakout (distance to the 20-day high). |
| Parameters stated, not discovered from the result | `config/default.yml` comments. `04_backtest.md` section 1 repeats the rule with the parameters that actually ran. |
| A score is not yet a position | `marketengine/strategies.py` returns a percentile per (date, symbol). `target_weights` is a separate function. The simulator is `marketengine/backtest.py`. |

## Signal implementation — 12

| The requirement | Where to look |
|---|---|
| Signals computed from historical prices | `marketengine/features.py`: trailing return, trailing volatility, distance to high. All shifts look backward. |
| Signals match the stated rules | `tests/test_backtest.py`: hand-built cross-sections where the intended ranking is the test, not a snapshot of the function's own output. |
| No lookahead | `tests/test_backtest.py` `test_scores_do_not_change_when_the_future_is_rewritten` and `test_truncating_the_future_does_not_change_past_features`. `02_tests.txt` is the run. |
| Ineligible names stay blank | A short window, a flat volatility, or a missing price is NaN and is not ranked. Test: `test_flat_volatility_is_not_a_reversal_signal`. |

## Basic historical simulation — 12

| The requirement | Where to look |
|---|---|
| A coherent simulation through time | `03_backtest.txt` and `04_backtest.md`. Decisions from 2015-02-03, marks through the latest completed open. |
| Actions and positions | `data/outputs/baseline/weights_*.csv`, `positions_*.csv`, `trades_*.csv`. The trade row records the raw open. |
| Portfolio value over time | `data/outputs/baseline/backtest_nav.csv`, `figures/backtest_equity.png`, `figures/backtest_drawdown.png`. |
| Fill is the next open, not the signal close | `tests/test_backtest.py` `test_the_fill_is_the_next_open_and_not_the_signal_close`. A close spike of 999 is not a fill and not a return. |
| A split is not a loss | `test_a_split_is_not_booked_as_a_loss_and_the_fill_is_the_raw_open`. P&L uses the adjusted open; the logged fill is the raw open. |
| Costs | 5 bp one-way on the notional that changed hands. `test_cost_is_charged_once_on_the_way_in_and_not_again_for_holding`. |

## Comparison and interpretation — 10

| The requirement | Where to look |
|---|---|
| Compared with a simple benchmark | Two. SPY buy-and-hold, and equal-weight buy-and-hold of the same ten names. `04_backtest.md` section 3, `05_summary.csv`, `figures/backtest_equity.png`. |
| The comparison is interpreted | The opening of `04_backtest.md` states the result: none of the three beat equal-weight. Momentum and reversal beat SPY; breakout does not. Section 3 names the NVDA concentration from the position log. |
| Costs change the conclusion | Section 4 and `06_cost_grid.csv` and `figures/backtest_costs.png`. Breakeven vs SPY is a headline number: momentum 10.2 bp, reversal 5.4 bp, breakout 1.8 bp, against a stated cost of 5 bp. |
| Limitations stated | `04_backtest.md` section 7. Survivorship, the missing split-only price series, no volume cap, flat costs, no intraday stops, cash earns zero, provisional last bar not refused. |
| The three rules are not one rule | Section 5, `07_score_corr.csv`. Strongest pair is reversal vs breakout at -0.61. The report says they would be called one strategy at 0.9. They are not. |

## Code and documentation quality — 4

| The requirement | Where to look |
|---|---|
| Readable, reusable, assumptions written down | `features.py`, `strategies.py`, and `backtest.py` are pure of the network. A different universe is a different YAML file. Assumptions are section 7 of the report and the comments in `config/default.yml`. |
| Tests | `02_tests.txt`. Closed-form cases for the ranks, the fill timing, the split, the cost, and the config. |
| Regenerable | `capture.py` rewrites this directory from a real `pytest` and a real `backtest`. |
