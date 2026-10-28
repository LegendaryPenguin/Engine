# Train-only search — validation

**42 configurations** evaluated on the training window only. The chronological boundary (**2024-06-20**) was fixed before any trial. The winner is the best **train Sharpe**. The test window was used **once**, for that winner alone.

**Winner:** `mom_126d_skip21_top5` (momentum, top 5, params {'window': 126, 'skip': 21}).

- Train: CAGR 26.0%, Sharpe 1.13, beats equal-weight (17.2%).
- Sealed test: CAGR 9.4%, Sharpe 0.41, max DD -22.4%. Equal-weight test CAGR 18.9% / Sharpe 1.10. **Does not beat equal-weight on the untouched test window.**

Searching more configurations against this same test window would invalidate the result (Bailey et al., probability of backtest overfitting). The trial log is saved so a later CSCV/PBO estimate can use it without re-peeking.

## Method

- Split: chronological, same rule as `validate` (80% train). Train 2360 decisions, test 590.
- Grid: literature-shaped momentum / reversal / breakout settings × `n_long ∈ {5,10,15}`. Costs and caps stay at the config values (5 bp one-way).
- Selection: maximum train Sharpe; ties broken by train CAGR.
- Test metrics exist only for the selected trial. Other trials have train metrics only.
- Config SHA-256: `7c2a3479545401637ca2e40770d5915326dcb508e607a43f88a5ad06968eb9e8`.

## Top train Sharpes

| label                 | family   | train Sharpe   | train CAGR   | vs EW (train)   | selected   |
|:----------------------|:---------|:---------------|:-------------|:----------------|:-----------|
| mom_126d_skip21_top5  | momentum | 1.13           | 26.0%        | yes             | ←          |
| mom_60d_skip1_top10   | momentum | 1.00           | 22.9%        | yes             |            |
| mom_252d_skip21_top5  | momentum | 0.98           | 22.0%        | yes             |            |
| mom_60d_skip1_top5    | momentum | 0.94           | 19.6%        | yes             |            |
| mom_126d_skip21_top10 | momentum | 0.94           | 22.9%        | yes             |            |
| mom_60d_skip1_top15   | momentum | 0.93           | 19.6%        | yes             |            |
| mom_252d_skip21_top10 | momentum | 0.91           | 21.7%        | yes             |            |
| mom_126d_skip21_top15 | momentum | 0.89           | 19.8%        | yes             |            |
| mom_252d_skip21_top15 | momentum | 0.82           | 17.7%        | yes             |            |
| mom_20d_skip1_top10   | momentum | 0.80           | 17.9%        | yes             |            |

## How many beat equal-weight on train?

10 of 42 successful trials beat equal-weight CAGR on the **training** window. That is an in-sample count; it is not evidence about the test window.

## Reference

D. H. Bailey, J. Borwein, M. López de Prado, and Q. J. Zhu, "The probability of backtest overfitting," *Journal of Computational Finance*, 2017. Ordinary holdout after repeated selection contaminates the test set; this run selects only on train and evaluates test once.

