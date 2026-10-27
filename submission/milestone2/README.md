# Milestone 2 — submission evidence

Captured terminal output from a real run, for grading. Everything numbered
in this folder is produced by

```bash
python submission/milestone2/capture.py
```

from the repository root, after `python -m marketengine run` has built the
curated panel once. The script runs pytest and `backtest` and copies their
output. It does not download, and it does not edit the files afterwards.

The panel is the Milestone 1 baseline: 11 symbols (10 tickers + SPY),
yfinance, total-return prices. The backtest drops the warmup, so the
numbers below start once every strategy can actually fire
(**decisions from 2015-02-03**, marks from 2015-02-05 through the latest
completed open).

---

## Headline numbers

Stated cost: **5 bp one-way**, charged on the notional that changes hands.
Book: **long-only top 3 of 10**, equal weight, unfilled slots left in cash.
Fill: signal at the close of t, trade at the **raw open of t+1**, P&L on
the adjusted open so a split is not a loss.

| book | CAGR | vol | Sharpe | max DD | hold | turnover |
|---|---|---|---|---|---|---|
| momentum | 18.6% | 20.2% | 0.85 | -34.1% | 6.3 sessions | 39.73x / year |
| reversal | 14.4% | 20.5% | 0.66 | -38.2% | 3.3 sessions | 77.35x / year |
| breakout | 8.5% | 15.6% | 0.48 | -21.9% | 3.4 sessions | 73.67x / year |
| equal-weight buy-and-hold | **21.3%** | 15.6% | 1.19 | -26.8% | the whole window | ~0x |
| SPY buy-and-hold | 13.8% | 16.8% | 0.73 | -32.0% | the whole window | ~0x |

**None of the three strategies beat equal-weight buy-and-hold of the same
names.** That is the result. Momentum and reversal do beat SPY. Breakout
does not. Momentum held NVDA on 54% of decision dates, so its lead over
the index is not separable from NVDA on a three-name book.

Breakeven versus SPY, one-way:

| strategy | breakeven | at the stated 5 bp |
|---|---|---|
| momentum | 10.2 bp | still ahead |
| reversal | 5.4 bp | still ahead, barely (the lead is +0.6 pp of CAGR) |
| breakout | 1.8 bp | already behind |

At 0 bp, momentum and reversal both compound at about 23.5%, which is the
gross of the signal. At 25 bp both of the fast books are negative and
momentum is down to 1.2%. The turnover is why. A top-3 book that holds a
name for 3 to 6 sessions replaces itself dozens of times a year, and a
flat 5 bp on that much trading is most of the edge. [`04_backtest.md`](04_backtest.md)
section 4 and `figures/backtest_costs.png` are that sweep.

Score correlation, strongest pair: **reversal vs breakout, -0.61**. They
disagree, which a reversal rule and a trend rule should. Nothing is near
0.9, which is the line at which the report would call two of them one
strategy.

---

## Files

| file | what it is |
|---|---|
| `00_environment.txt` | platform, Python, frozen packages |
| `01_config.txt` | resolved config, including the strategy block and the config SHA-256 |
| `02_tests.txt` | `pytest -q`. The new tests are closed-form: rankings, no lookahead, next-open fill, split, cost |
| `03_backtest.txt` | stdout of `python -m marketengine backtest` |
| `04_backtest.md` | the report: rules, results, costs, score correlation, ten assumptions |
| `05_summary.csv` | one row per book, full precision |
| `06_cost_grid.csv` | CAGR at 0, 5, 10, 25 bp and the excess over SPY |
| `07_score_corr.csv` | correlation of the three score panels |
| `figures/backtest_equity.png` | growth of $1, log scale, both benchmarks |
| `figures/backtest_drawdown.png` | drawdown from peak |
| `figures/backtest_costs.png` | CAGR against one-way cost |

[`RUBRIC.md`](RUBRIC.md) is the same evidence as one row per rubric line.

The position and trade logs are written to `data/outputs/baseline/`
(`weights_*.csv`, `positions_*.csv`, `trades_*.csv`, `backtest_nav.csv`).
`data/` is gitignored because it is reproducible from the config. The
report and the three figures are the committed record.

---

## What this does not claim

- **It does not beat owning the universe.** Equal-weight buy-and-hold is
  the null, and it won. Beating SPY is a weaker statement on a list of
  names that were chosen because they exist today.
- **The breakout is on the wrong price basis.** It should use
  split-adjusted prices that have not been dividend-adjusted. That series
  is not stored yet. The report uses `adj_close` and says so.
- **No volume cap.** A one-third weight is not checked against the day's
  dollar volume. A later milestone has to, before any of this is a trade.
- **No orders.** Nothing here talks to a broker. The simulator is the
  seam a later milestone will replace with paper orders, and the score
  functions do not need to change for that to happen.
- **Parameters were not searched.** 20, 5, 20, top 3, and 5 bp are the
  rules in `ROADMAP.md`, written into `config/default.yml` before this
  run. A window that would have looked better is not the result.
