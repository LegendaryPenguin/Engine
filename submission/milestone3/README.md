# Milestone 3 — submission evidence

Captured terminal output from a real validation run, for grading.
Everything numbered here is produced by

```bash
python submission/milestone3/capture.py
```

after

```bash
python -m marketengine --config config/validation.yml run
```

has built the curated panel once. The capture script does not download,
and it does not edit the files afterwards.

---

## Headline numbers

**Universe:** 62 tradable names + SPY (tech, financials, health care,
energy, consumer, industrials, and liquid ETFs). Config:
`config/validation.yml`.

**Book:** long-only top 10, equal weight before caps. Cost **5 bp
one-way** = 1 bp commission + 4 bp slippage. Caps: max weight 15%; max
participation 2% of 20-day median dollar volume at an assumed $1M book.

**Split:** chronological **80% train / 20% test** on decision dates after
warmup. Boundary **2024-06-20**. Train 2015-02-03 → 2024-06-18 (2,360
decisions). Test 2024-06-20 → 2026-10-26 (590 decisions). Untouched.

| book | train CAGR | test CAGR | train Sharpe | test Sharpe | overfit flag |
|---|---|---|---|---|---|
| momentum | 17.9% | 12.5% | 0.80 | 0.60 | no |
| reversal | 9.8% | 7.8% | 0.45 | 0.38 | no |
| breakout | 4.7% | 3.4% | 0.24 | 0.16 | no |
| equal-weight buy-and-hold | — | **18.9%** | — | — | — |
| SPY buy-and-hold | — | 16.8% | — | — | — |

**On the untouched test window, none of the three baseline strategies beat
equal-weight buy-and-hold of the same 62 names.** That is the result.
Momentum still beats SPY on the test window; clearing the passive book of
the same names is the harder null, and none clear it. No strategy tripped
the configured 40% Sharpe-drop overfit flag; every strategy’s test CAGR
is still below its train CAGR.

### Train-only search (Brief 9)

**42** literature-shaped configurations (momentum / reversal / breakout ×
`n_long ∈ {5,10,15}`) were scored on the **training window only**. The
boundary was fixed first. Winner by train Sharpe:
`mom_126d_skip21_top5` — train Sharpe **1.13**, train CAGR **26.0%**
(beats equal-weight on train).

**Sealed test (once):** CAGR **9.4%**, Sharpe **0.41**, vs equal-weight
test CAGR **18.9%**. It loses. A strong in-sample Sharpe did not survive.
All 42 trials are in `12_search_trials.csv`; only the winner has test
metrics (`13_search_summary.csv`, `05b_search.md`).

---

## Files

| file | what it is |
|---|---|
| `00_environment.txt` | platform, Python, frozen packages |
| `01_config.txt` | resolved validation config + SHA-256 |
| `02_tests.txt` | `pytest -q` (includes split, cap, and overfit tests) |
| `03_validate.txt` | stdout of `python -m marketengine --config config/validation.yml validate` |
| `04_validation.md` | full report: method, train/test, overfit, costs, **bias analysis** |
| `05_backtest_fullsample.md` | full-sample backtest on the same universe (reusable workflow) |
| `06_comparison.csv` | train vs test metrics, gaps |
| `07_overfit_flags.csv` | per-strategy Sharpe-drop flags |
| `08_cost_grid.csv` | cost sweep on both windows |
| `09_split.csv` | boundary date, counts, config hash |
| `10_train_summary.csv` / `11_test_summary.csv` | full metric tables |
| `figures/validation_*.png` | train equity, test equity, CAGR bars, test cost sweep |

[`RUBRIC.md`](RUBRIC.md) maps each rubric line to the file that shows it.

---

## What this does not claim

- **It does not beat owning the universe on the test window.** Equal-weight
  won. Beating SPY alone is a weaker statement.
- **Survivorship remains.** Sixty-two names that exist today are still
  survivors. Point-in-time membership is later work.
- **Parameters were not searched on the test set.** Retuning after seeing
  these numbers would invalidate the overfit check; the config hash is
  how that would show up.
- **No orders.** The simulator is still the seam a later milestone
  replaces with paper trading.
