"""
Chronological out-of-sample validation.

The same prepare/simulate path as `backtest`, run twice: once on the
training window, once on the untouched test window. The split is by
calendar order of decision dates, never a random shuffle. Shuffling
daily data puts tomorrow in the training set and yesterday in the test
set, which inflates everything and is undetectable from the output.

Parameters are not chosen here. They come from the config that
Milestone 2 already fixed. The test window is for one look, and the
gap between train and test is the headline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import plots, store, strategies
from .backtest import (
    BENCHMARK_EW,
    BookResult,
    PreparedBooks,
    cost_sweep,
    metrics_row,
    prepare_books,
    simulate_prepared,
)
from .config import Config


@dataclass
class Split:
    """Chronological train / test decision dates and the first test date."""

    train_index: pd.DatetimeIndex
    test_index: pd.DatetimeIndex
    boundary: pd.Timestamp
    train_fraction: float


@dataclass
class ValidationOutput:
    split: Split
    train_summary: pd.DataFrame
    test_summary: pd.DataFrame
    comparison: pd.DataFrame
    train_cost_grid: pd.DataFrame
    test_cost_grid: pd.DataFrame
    train_breakeven: dict[str, float]
    test_breakeven: dict[str, float]
    train_books: dict[str, BookResult]
    test_books: dict[str, BookResult]
    overfitting_flags: pd.DataFrame
    report_path: Path
    figures: dict[str, Path]
    prepared: PreparedBooks


def chronological_split(decision_index: pd.DatetimeIndex, train_fraction: float) -> Split:
    """Cut the decision calendar into train then test. No shuffle.

    The boundary is the first date of the test window. Every train date
    is strictly earlier. A test that finds a train date >= boundary fails.
    """
    if not (0.5 <= train_fraction < 1.0):
        raise ValueError(f"train_fraction must be in [0.5, 1), got {train_fraction}")
    index = pd.DatetimeIndex(decision_index).sort_values().unique()
    if len(index) < 40:
        raise ValueError(
            f"need at least 40 decision dates to split, got {len(index)}. "
            "Widen the window or shrink the feature warmup."
        )
    cut = int(len(index) * train_fraction)
    # Keep at least 20 sessions on each side so a metric is not one week.
    cut = min(max(cut, 20), len(index) - 20)
    train = index[:cut]
    test = index[cut:]
    return Split(
        train_index=train,
        test_index=test,
        boundary=pd.Timestamp(test[0]),
        train_fraction=train_fraction,
    )


def assert_split_is_clean(split: Split) -> None:
    """Fail loudly if any training decision sits on or after the boundary."""
    if len(split.train_index) == 0 or len(split.test_index) == 0:
        raise ValueError("train and test windows must both be non-empty")
    if split.train_index.max() >= split.boundary:
        raise ValueError(
            f"train window reaches {split.train_index.max().date()}, "
            f"which is not before the test boundary {split.boundary.date()}"
        )
    if split.test_index.min() != split.boundary:
        raise ValueError(
            f"test window must start at the boundary {split.boundary.date()}, "
            f"got {split.test_index.min().date()}"
        )
    overlap = set(split.train_index).intersection(split.test_index)
    if overlap:
        raise ValueError(f"train and test share {len(overlap)} decision date(s)")


def overfitting_table(train_summary: pd.DataFrame, test_summary: pd.DataFrame,
                      drop: float) -> pd.DataFrame:
    """Flag strategies whose test Sharpe falls more than `drop` below train.

    A drop of 0.40 means test Sharpe below 60% of train Sharpe is reported
    as overfitting. Negative train Sharpe is not a success to defend: the
    flag then fires when test is worse still.
    """
    rows = []
    for name in strategies.NAMES:
        tr = train_summary.loc[train_summary["name"] == name].iloc[0]
        te = test_summary.loc[test_summary["name"] == name].iloc[0]
        train_s = float(tr["sharpe"])
        test_s = float(te["sharpe"])
        if np.isfinite(train_s) and train_s > 0:
            threshold = train_s * (1.0 - drop)
            flagged = bool(np.isfinite(test_s) and test_s < threshold)
            reason = (f"test Sharpe {test_s:.2f} < {threshold:.2f} "
                      f"({drop:.0%} below train {train_s:.2f})") if flagged else ""
        elif np.isfinite(train_s) and train_s <= 0:
            flagged = bool(np.isfinite(test_s) and test_s < train_s)
            reason = (f"train Sharpe already non-positive ({train_s:.2f}); "
                      f"test {test_s:.2f} is worse") if flagged else ""
        else:
            flagged = False
            reason = "train Sharpe undefined"
        rows.append({
            "name": name,
            "train_sharpe": train_s,
            "test_sharpe": test_s,
            "sharpe_gap": test_s - train_s if np.isfinite(train_s) and np.isfinite(test_s) else float("nan"),
            "train_cagr": float(tr["cagr"]),
            "test_cagr": float(te["cagr"]),
            "cagr_gap": float(te["cagr"]) - float(tr["cagr"]),
            "overfit_flag": flagged,
            "reason": reason,
        })
    return pd.DataFrame(rows)


def _summary_for(books: dict[str, BookResult], cfg: Config, *, split: str) -> pd.DataFrame:
    bench = cfg.universe.benchmark
    an = cfg.analytics
    bench_rets = books[bench].returns
    rows = []
    for name in (*strategies.NAMES, BENCHMARK_EW, bench):
        kind = "benchmark" if name in (BENCHMARK_EW, bench) else "strategy"
        rows.append(metrics_row(name, kind, books[name], bench_rets,
                                an.risk_free_daily, an.trading_days_per_year,
                                split=split))
    return pd.DataFrame(rows)


def _comparison(train: pd.DataFrame, test: pd.DataFrame) -> pd.DataFrame:
    """Train and test metrics side by side, gap as the headline columns."""
    keys = ["name", "kind"]
    metrics = ["cagr", "ann_volatility", "sharpe", "sortino", "max_drawdown",
               "var_95", "hit_rate", "avg_holding_sessions", "annual_turnover",
               "alpha_annual", "n_obs", "n_trades", "mean_gross_exposure"]
    tr = train.loc[:, keys + metrics].rename(columns={m: f"train_{m}" for m in metrics})
    te = test.loc[:, keys + metrics].rename(columns={m: f"test_{m}" for m in metrics})
    out = tr.merge(te, on=keys, how="inner")
    out["cagr_gap"] = out["test_cagr"] - out["train_cagr"]
    out["sharpe_gap"] = out["test_sharpe"] - out["train_sharpe"]
    return out


def run_validation(cfg: Config, panel: pd.DataFrame) -> ValidationOutput:
    """Prepare once, simulate train and test, write the validation report."""
    if cfg.strategies is None:
        raise ValueError("config has no `strategies:` section")
    if cfg.validation is None:
        raise ValueError(
            "config has no `validation:` section. "
            "Use config/validation.yml or add the block to the config."
        )

    prepared = prepare_books(cfg, panel)
    split = chronological_split(
        prepared.weights["momentum"].index, cfg.validation.train_fraction)
    assert_split_is_clean(split)

    print("[validate] chronological train / test")
    print(f"      universe  {len(prepared.tradable)} names + {cfg.universe.benchmark}")
    print(f"      common    {prepared.common_start.date()} onward "
          f"({len(prepared.weights['momentum'])} decision dates)")
    print(f"      train     {split.train_index[0].date()} -> {split.train_index[-1].date()} "
          f"({len(split.train_index)} decisions, {cfg.validation.train_fraction:.0%})")
    print(f"      test      {split.test_index[0].date()} -> {split.test_index[-1].date()} "
          f"({len(split.test_index)} decisions, untouched)")
    print(f"      boundary  {split.boundary.date()} (first test decision)")

    train_books = simulate_prepared(prepared, cfg, decision_index=split.train_index,
                                    name_suffix="_train")
    test_books = simulate_prepared(prepared, cfg, decision_index=split.test_index,
                                   name_suffix="_test")
    train_summary = _summary_for(train_books, cfg, split="train")
    test_summary = _summary_for(test_books, cfg, split="test")
    comparison = _comparison(train_summary, test_summary)
    flags = overfitting_table(train_summary, test_summary,
                              cfg.validation.overfitting_sharpe_drop)

    train_grid, train_be = cost_sweep(prepared, cfg, decision_index=split.train_index)
    test_grid, test_be = cost_sweep(prepared, cfg, decision_index=split.test_index)
    train_grid = train_grid.assign(split="train")
    test_grid = test_grid.assign(split="test")

    _write_validation_tables(cfg, train_summary, test_summary, comparison, flags,
                             train_grid, test_grid, train_books, test_books, split)
    figures = _write_validation_figures(cfg, train_books, test_books, train_grid, test_grid)
    from . import validation_report

    report_path = validation_report.write_report(
        cfg,
        split=split,
        prepared=prepared,
        train_summary=train_summary,
        test_summary=test_summary,
        comparison=comparison,
        flags=flags,
        train_cost_grid=train_grid,
        test_cost_grid=test_grid,
        train_breakeven=train_be,
        test_breakeven=test_be,
        train_books=train_books,
        test_books=test_books,
        figures=figures,
    )
    _print_validation_headline(comparison, flags, cfg.universe.benchmark)
    print(f"      report  -> {report_path}")

    return ValidationOutput(
        split=split,
        train_summary=train_summary,
        test_summary=test_summary,
        comparison=comparison,
        train_cost_grid=train_grid,
        test_cost_grid=test_grid,
        train_breakeven=train_be,
        test_breakeven=test_be,
        train_books=train_books,
        test_books=test_books,
        overfitting_flags=flags,
        report_path=report_path,
        figures=figures,
        prepared=prepared,
    )


def _write_validation_tables(cfg: Config, train_summary: pd.DataFrame,
                             test_summary: pd.DataFrame, comparison: pd.DataFrame,
                             flags: pd.DataFrame, train_grid: pd.DataFrame,
                             test_grid: pd.DataFrame,
                             train_books: dict[str, BookResult],
                             test_books: dict[str, BookResult],
                             split: Split) -> None:
    store.write_table(cfg.output_dir, "validation_train_summary", train_summary, index=False)
    store.write_table(cfg.output_dir, "validation_test_summary", test_summary, index=False)
    store.write_table(cfg.output_dir, "validation_comparison", comparison, index=False)
    store.write_table(cfg.output_dir, "validation_overfit_flags", flags, index=False)
    store.write_table(cfg.output_dir, "validation_cost_grid",
                      pd.concat([train_grid, test_grid], ignore_index=True), index=False)
    store.write_table(cfg.output_dir, "validation_split", pd.DataFrame([{
        "train_fraction": split.train_fraction,
        "boundary": split.boundary,
        "train_start": split.train_index[0],
        "train_end": split.train_index[-1],
        "test_start": split.test_index[0],
        "test_end": split.test_index[-1],
        "n_train": len(split.train_index),
        "n_test": len(split.test_index),
        "config_sha256": cfg.source_sha256,
    }]), index=False)

    train_nav = pd.DataFrame({n: b.ledger["nav"] for n, b in train_books.items() if len(b.ledger)})
    test_nav = pd.DataFrame({n: b.ledger["nav"] for n, b in test_books.items() if len(b.ledger)})
    store.write_table(cfg.output_dir, "validation_train_nav", train_nav)
    store.write_table(cfg.output_dir, "validation_test_nav", test_nav)
    for label, books in (("train", train_books), ("test", test_books)):
        for name in strategies.NAMES:
            store.write_table(cfg.output_dir, f"validation_{label}_positions_{name}",
                              books[name].positions, index=False)
            store.write_table(cfg.output_dir, f"validation_{label}_trades_{name}",
                              books[name].trades, index=False)


def _write_validation_figures(cfg: Config, train_books: dict[str, BookResult],
                              test_books: dict[str, BookResult],
                              train_grid: pd.DataFrame,
                              test_grid: pd.DataFrame) -> dict[str, Path]:
    if not cfg.outputs.figures:
        return {}
    bench = cfg.universe.benchmark
    train_rets = pd.DataFrame({n: train_books[n].returns
                               for n in (*strategies.NAMES, BENCHMARK_EW, bench)})
    test_rets = pd.DataFrame({n: test_books[n].returns
                              for n in (*strategies.NAMES, BENCHMARK_EW, bench)})
    cost = cfg.strategies.portfolio.cost_bps if cfg.strategies else 0
    figures = {
        "Train window — growth of $1":
            plots.plot_strategy_equity(
                train_rets, cfg.figure_dir / "validation_train_equity.png",
                cfg.outputs.dpi, benchmark=bench, cost_bps=cost),
        "Test window — growth of $1 (untouched)":
            plots.plot_strategy_equity(
                test_rets, cfg.figure_dir / "validation_test_equity.png",
                cfg.outputs.dpi, benchmark=bench, cost_bps=cost),
        "Train vs test CAGR at the stated cost":
            plots.plot_train_test_bars(
                train_books, test_books, cfg.figure_dir / "validation_train_test_cagr.png",
                cfg.outputs.dpi, periods=cfg.analytics.trading_days_per_year),
        "Test-window cost sweep":
            plots.plot_cost_sweep(
                test_grid, cfg.figure_dir / "validation_test_costs.png", cfg.outputs.dpi),
    }
    del train_grid
    return figures


def _print_validation_headline(comparison: pd.DataFrame, flags: pd.DataFrame,
                               benchmark: str) -> None:
    show = comparison.loc[comparison["kind"] == "strategy",
                          ["name", "train_cagr", "test_cagr", "cagr_gap",
                           "train_sharpe", "test_sharpe", "sharpe_gap"]].copy()
    for col in ("train_cagr", "test_cagr", "cagr_gap"):
        show[col] = show[col].map(lambda v: "n/a" if not np.isfinite(v) else f"{v:.1%}")
    for col in ("train_sharpe", "test_sharpe", "sharpe_gap"):
        show[col] = show[col].map(lambda v: "n/a" if not np.isfinite(v) else f"{v:.2f}")
    print(show.to_string(index=False))
    flagged = flags.loc[flags["overfit_flag"], "name"].tolist()
    if flagged:
        print(f"      overfitting flag: {', '.join(flagged)}")
    else:
        print("      overfitting flag: none under the configured Sharpe-drop rule")
    ew = comparison.loc[comparison["name"] == BENCHMARK_EW]
    if not ew.empty:
        row = ew.iloc[0]
        print(f"      equal-weight test CAGR {row['test_cagr']:.1%} vs "
              f"{benchmark} test CAGR "
              f"{comparison.loc[comparison['name'] == benchmark, 'test_cagr'].iloc[0]:.1%}")
