"""
Train-only configuration search (Milestone 3 / Brief 9).

The chronological train/test boundary is fixed *before* any trial is
scored. Every configuration is evaluated on the training window only.
The winner is the best train Sharpe. The untouched test window is used
once, for that winner alone.

All trials are written to disk — not just the winner — because the
number of attempts is part of how convincing a Sharpe is (Bailey et al.).
CSCV / PBO is not computed here; the trial log is the input that a later
PBO estimate would need.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import analytics, features, store, strategies
from .backtest import BENCHMARK_EW, BookResult, metrics_row, simulate
from .config import Config
from .validate import assert_split_is_clean, chronological_split


@dataclass(frozen=True)
class TrialSpec:
    trial_id: int
    family: str
    label: str
    params: dict[str, object]
    n_long: int


@dataclass
class SearchOutput:
    n_trials: int
    boundary: pd.Timestamp
    trials: pd.DataFrame
    winner: TrialSpec
    train_winner: dict[str, object]
    test_winner: dict[str, object]
    train_ew: dict[str, object]
    test_ew: dict[str, object]
    report_path: Path


def literature_grid() -> list[tuple[str, str, dict[str, object]]]:
    """Named configurations from common short-horizon literature shapes.

    These are not fit to the test window. They are a fixed menu evaluated
    on train only. Labels make the trial log readable.
    """
    out: list[tuple[str, str, dict[str, object]]] = []
    # Momentum: skipped-return cross-section (Jegadeesh/Titman family, daily).
    for label, window, skip in (
        ("mom_10d", 10, 0),
        ("mom_10d_skip1", 10, 1),
        ("mom_20d_skip1", 20, 1),          # Milestone 2 default
        ("mom_60d_skip1", 60, 1),
        ("mom_126d_skip21", 126, 21),      # ~6-1 month
        ("mom_252d_skip21", 252, 21),      # ~12-1 month
    ):
        out.append(("momentum", label, {"window": window, "skip": skip}))
    # Vol-scaled short-horizon reversal.
    for label, rw, vw in (
        ("rev_3d_vol10", 3, 10),
        ("rev_5d_vol20", 5, 20),           # Milestone 2 default
        ("rev_10d_vol20", 10, 20),
        ("rev_20d_vol60", 20, 60),
    ):
        out.append(("reversal", label, {"return_window": rw, "vol_window": vw}))
    # Breakout / Donchian-style distance to high.
    for label, window in (
        ("brk_10d", 10),
        ("brk_20d", 20),                   # Milestone 2 default
        ("brk_60d", 60),
        ("brk_120d", 120),
    ):
        out.append(("breakout", label, {"window": window}))
    return out


def build_trial_list(n_longs: tuple[int, ...] = (5, 10, 15)) -> list[TrialSpec]:
    trials: list[TrialSpec] = []
    tid = 0
    for family, label, params in literature_grid():
        for n_long in n_longs:
            trials.append(TrialSpec(
                trial_id=tid,
                family=family,
                label=f"{label}_top{n_long}",
                params=params,
                n_long=n_long,
            ))
            tid += 1
    return trials


def _score_family(prices: pd.DataFrame, family: str, params: dict[str, object]) -> pd.DataFrame:
    if family == "momentum":
        return strategies.momentum_scores(prices, int(params["window"]), int(params["skip"]))
    if family == "reversal":
        return strategies.reversal_scores(
            prices, int(params["return_window"]), int(params["vol_window"]))
    if family == "breakout":
        return strategies.breakout_scores(prices, int(params["window"]))
    raise ValueError(f"unknown family {family!r}")


def _panel_matrices(cfg: Config, panel: pd.DataFrame) -> dict[str, object]:
    tradable = list(cfg.universe.tickers)
    bench = cfg.universe.benchmark
    present = set(panel["symbol"].unique())
    for sym in (*tradable, bench):
        if sym not in present:
            raise ValueError(f"{sym} missing from curated panel")
    raw_open = store.wide(panel, "open")
    close = store.wide(panel, "close")
    adj_close = store.wide(panel, "adj_close")
    volume = store.wide(panel, "volume")
    adj_open = features.adjusted_open(raw_open, close, adj_close)
    dollar_volume = close.reindex(columns=tradable) * volume.reindex(columns=tradable)
    prices = adj_close.reindex(columns=tradable)
    return {
        "tradable": tradable,
        "bench": bench,
        "raw_open": raw_open,
        "adj_open": adj_open,
        "dollar_volume": dollar_volume,
        "prices": prices,
    }


def _capped_weights(scores: pd.DataFrame, dollar_volume: pd.DataFrame,
                    cfg: Config, n_long: int) -> pd.DataFrame:
    port = cfg.strategies.portfolio  # type: ignore[union-attr]
    weights = strategies.target_weights(scores, n_long)
    return strategies.apply_portfolio_caps(
        weights,
        dollar_volume,
        max_weight=port.max_weight,
        max_participation=port.max_participation,
        adv_window=port.adv_window,
        assumed_nav_usd=port.assumed_nav_usd,
    )


def _book_metrics(book: BookResult, bench_book: BookResult, cfg: Config,
                  *, split: str, name: str) -> dict[str, object]:
    an = cfg.analytics
    row = metrics_row(name, "strategy", book, bench_book.returns,
                      an.risk_free_daily, an.trading_days_per_year, split=split)
    row["excess_cagr_vs_bench"] = (
        float(row["cagr"]) - analytics.annualised_return(bench_book.returns, an.trading_days_per_year)
        if np.isfinite(row["cagr"]) else float("nan")
    )
    return row


def run_search(cfg: Config, panel: pd.DataFrame,
               trials: list[TrialSpec] | None = None) -> SearchOutput:
    """Search on train only; evaluate the winner once on the sealed test."""
    if cfg.strategies is None or cfg.validation is None:
        raise ValueError("search requires strategies: and validation: in the config")

    mats = _panel_matrices(cfg, panel)
    prices: pd.DataFrame = mats["prices"]  # type: ignore[assignment]
    raw_open: pd.DataFrame = mats["raw_open"]  # type: ignore[assignment]
    adj_open: pd.DataFrame = mats["adj_open"]  # type: ignore[assignment]
    dollar_volume: pd.DataFrame = mats["dollar_volume"]  # type: ignore[assignment]
    tradable: list[str] = mats["tradable"]  # type: ignore[assignment]
    bench: str = mats["bench"]  # type: ignore[assignment]
    port = cfg.strategies.portfolio
    cost = port.cost_bps
    periods = cfg.analytics.trading_days_per_year

    # Fix the split from the Milestone-2 default weights so the boundary
    # does not depend on which trial is being scored.
    baseline_scores = strategies.score_all(
        prices,
        momentum_window=cfg.strategies.momentum.window,
        momentum_skip=cfg.strategies.momentum.skip,
        reversal_return_window=cfg.strategies.reversal.return_window,
        reversal_vol_window=cfg.strategies.reversal.vol_window,
        breakout_window=cfg.strategies.breakout.window,
    )
    baseline_w = {
        name: _capped_weights(frame, dollar_volume, cfg, port.n_long)
        for name, frame in baseline_scores.items()
    }
    common = max(strategies.first_invested(w) for w in baseline_w.values())
    decision_index = baseline_w["momentum"].loc[common:].index
    split = chronological_split(decision_index, cfg.validation.train_fraction)
    assert_split_is_clean(split)

    if trials is None:
        trials = build_trial_list()

    print("[search] train-only configuration search")
    print(f"      boundary  {split.boundary.date()} (fixed before any trial)")
    print(f"      train     {split.train_index[0].date()} -> {split.train_index[-1].date()} "
          f"({len(split.train_index)} decisions)")
    print(f"      test      sealed until winner selected "
          f"({len(split.test_index)} decisions)")
    print(f"      trials    {len(trials)} "
          f"(literature-shaped grid × n_long; costs/caps fixed)")

    # Benchmarks on train (and later test) once.
    ew_train_w = pd.DataFrame(1.0 / len(tradable), index=split.train_index, columns=tradable)
    ew_train_w = strategies.apply_portfolio_caps(
        ew_train_w, dollar_volume,
        max_weight=port.max_weight, max_participation=port.max_participation,
        adv_window=port.adv_window, assumed_nav_usd=port.assumed_nav_usd,
    )
    spy_train_w = pd.DataFrame({bench: 1.0}, index=split.train_index)
    ew_train = simulate(ew_train_w, raw_open, adj_open, cost, name="equal_weight_train")
    spy_train = simulate(spy_train_w, raw_open, adj_open, cost, name=f"{bench}_train")
    train_ew_row = metrics_row(BENCHMARK_EW, "benchmark", ew_train, spy_train.returns,
                               cfg.analytics.risk_free_daily, periods, split="train")

    rows: list[dict[str, object]] = []
    train_books: dict[int, BookResult] = {}

    for spec in trials:
        scores = _score_family(prices, spec.family, spec.params)
        weights = _capped_weights(scores, dollar_volume, cfg, spec.n_long)
        # Drop warmup for this trial; intersect with the fixed train window.
        try:
            first = strategies.first_invested(weights)
        except ValueError:
            rows.append(_failed_trial(spec, "never invested"))
            continue
        w_train = weights.loc[first:].reindex(split.train_index).fillna(0.0)
        if w_train.sum(axis=1).sum() <= 0:
            rows.append(_failed_trial(spec, "no overlap with train window"))
            continue
        book = simulate(w_train, raw_open, adj_open, cost, name=spec.label)
        train_books[spec.trial_id] = book
        m = _book_metrics(book, spy_train, cfg, split="train", name=spec.label)
        ew_cagr = float(train_ew_row["cagr"])
        rows.append({
            "trial_id": spec.trial_id,
            "family": spec.family,
            "label": spec.label,
            "n_long": spec.n_long,
            **{f"param_{k}": v for k, v in spec.params.items()},
            "train_cagr": m["cagr"],
            "train_sharpe": m["sharpe"],
            "train_max_drawdown": m["max_drawdown"],
            "train_turnover": m["annual_turnover"],
            "train_excess_cagr_vs_spy": m["excess_cagr_vs_bench"],
            "train_excess_cagr_vs_ew": (
                float(m["cagr"]) - ew_cagr if np.isfinite(m["cagr"]) else float("nan")),
            "train_beats_ew": bool(np.isfinite(m["cagr"]) and float(m["cagr"]) > ew_cagr),
            "status": "ok",
        })
        if (spec.trial_id + 1) % 10 == 0 or spec.trial_id == 0:
            sharpe = m["sharpe"]
            sharpe_s = f"{sharpe:.2f}" if np.isfinite(sharpe) else "n/a"
            print(f"      trial {spec.trial_id + 1}/{len(trials)}  {spec.label}  "
                  f"train Sharpe={sharpe_s}")

    trials_df = pd.DataFrame(rows)
    ok = trials_df.loc[trials_df["status"] == "ok"].copy()
    if ok.empty:
        raise ValueError("every trial failed to invest on the train window")

    # Selection uses train Sharpe only. Ties -> higher train CAGR, then lower id.
    ok = ok.sort_values(["train_sharpe", "train_cagr", "trial_id"],
                        ascending=[False, False, True])
    best_id = int(ok.iloc[0]["trial_id"])
    winner = next(t for t in trials if t.trial_id == best_id)
    winner_train = ok.iloc[0].to_dict()

    print(f"      winner    {winner.label}  "
          f"train Sharpe={winner_train['train_sharpe']:.2f}  "
          f"train CAGR={winner_train['train_cagr']:.1%}  "
          f"(selected on train only)")

    # ---- sealed test: winner + benchmarks, once ----
    scores = _score_family(prices, winner.family, winner.params)
    weights = _capped_weights(scores, dollar_volume, cfg, winner.n_long)
    first = strategies.first_invested(weights)
    w_test = weights.loc[first:].reindex(split.test_index).fillna(0.0)
    ew_test_w = pd.DataFrame(1.0 / len(tradable), index=split.test_index, columns=tradable)
    ew_test_w = strategies.apply_portfolio_caps(
        ew_test_w, dollar_volume,
        max_weight=port.max_weight, max_participation=port.max_participation,
        adv_window=port.adv_window, assumed_nav_usd=port.assumed_nav_usd,
    )
    spy_test_w = pd.DataFrame({bench: 1.0}, index=split.test_index)
    test_book = simulate(w_test, raw_open, adj_open, cost, name=f"{winner.label}_test")
    ew_test = simulate(ew_test_w, raw_open, adj_open, cost, name="equal_weight_test")
    spy_test = simulate(spy_test_w, raw_open, adj_open, cost, name=f"{bench}_test")
    test_winner = _book_metrics(test_book, spy_test, cfg, split="test", name=winner.label)
    test_ew = metrics_row(BENCHMARK_EW, "benchmark", ew_test, spy_test.returns,
                          cfg.analytics.risk_free_daily, periods, split="test")
    test_ew["excess_cagr_vs_bench"] = (
        float(test_ew["cagr"]) - analytics.annualised_return(spy_test.returns, periods))

    beats_ew_test = bool(np.isfinite(test_winner["cagr"])
                         and float(test_winner["cagr"]) > float(test_ew["cagr"]))
    print(f"      sealed test CAGR={test_winner['cagr']:.1%}  "
          f"Sharpe={test_winner['sharpe']:.2f}  "
          f"EW test CAGR={test_ew['cagr']:.1%}  "
          f"{'BEATS' if beats_ew_test else 'loses to'} equal-weight")

    # Mark winner on the trial log; no OOS metrics for losers (sealed).
    trials_df["selected"] = trials_df["trial_id"] == best_id
    store.write_table(cfg.output_dir, "search_trials", trials_df, index=False)
    summary = pd.DataFrame([{
        "n_trials": len(trials),
        "n_ok": int((trials_df["status"] == "ok").sum()),
        "boundary": split.boundary,
        "winner_trial_id": best_id,
        "winner_label": winner.label,
        "winner_family": winner.family,
        "selection_rule": "max train Sharpe; tie-break train CAGR",
        "train_sharpe": winner_train["train_sharpe"],
        "train_cagr": winner_train["train_cagr"],
        "train_beats_ew": winner_train["train_beats_ew"],
        "test_sharpe": test_winner["sharpe"],
        "test_cagr": test_winner["cagr"],
        "test_max_drawdown": test_winner["max_drawdown"],
        "test_ew_cagr": test_ew["cagr"],
        "test_ew_sharpe": test_ew["sharpe"],
        "test_beats_ew": beats_ew_test,
        "config_sha256": cfg.source_sha256,
    }])
    store.write_table(cfg.output_dir, "search_summary", summary, index=False)

    from . import search_report
    report_path = search_report.write_report(
        cfg,
        trials=trials_df,
        winner=winner,
        winner_train=winner_train,
        winner_test=test_winner,
        train_ew=dict(train_ew_row),
        test_ew=dict(test_ew),
        boundary=split.boundary,
        n_train=len(split.train_index),
        n_test=len(split.test_index),
    )
    print(f"      report  -> {report_path}")
    return SearchOutput(
        n_trials=len(trials),
        boundary=split.boundary,
        trials=trials_df,
        winner=winner,
        train_winner=winner_train,
        test_winner=test_winner,
        train_ew=dict(train_ew_row),
        test_ew=dict(test_ew),
        report_path=report_path,
    )


def _failed_trial(spec: TrialSpec, reason: str) -> dict[str, object]:
    return {
        "trial_id": spec.trial_id,
        "family": spec.family,
        "label": spec.label,
        "n_long": spec.n_long,
        **{f"param_{k}": v for k, v in spec.params.items()},
        "train_cagr": float("nan"),
        "train_sharpe": float("nan"),
        "train_max_drawdown": float("nan"),
        "train_turnover": float("nan"),
        "train_excess_cagr_vs_spy": float("nan"),
        "train_excess_cagr_vs_ew": float("nan"),
        "train_beats_ew": False,
        "status": reason,
    }
