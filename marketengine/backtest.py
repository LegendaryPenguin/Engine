"""
Historical simulation.

A score becomes a book here, and nowhere else. The rules are the ones a
daily bar can actually support:

  - The signal is computed from the close of session t, using only data
    that exists at that close.
  - The earliest fill is the open of session t+1. Nothing in this file
    trades on the close that produced the signal.
  - The fill *price* recorded on the trade is the raw open: the number
    that was on the screen. The P&L between that open and the next one
    uses the adjusted open, so a split does not look like a loss and a
    dividend does not look like one either.
  - There is no intraday stop. A daily bar does not say whether the low
    happened before the high, and a backtest that answers the question
    is inventing the answer.
  - Cost is a flat number of basis points per side, charged on the
    notional that changed hands. It is a parameter, not a result.

The output of one book is a ledger of completed holds, the trades that
opened them, and the positions that were actually filled. A target
weight that could not be filled — missing or non-positive open — stays
in cash. It is not given a made-up return.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import analytics, features, plots, store, strategies
from .config import Config


BENCHMARK_EW = "equal_weight"


@dataclass
class BookResult:
    """One simulated book, strategy or benchmark."""

    name: str
    ledger: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame

    @property
    def returns(self) -> pd.Series:
        if self.ledger.empty:
            return pd.Series(dtype=float)
        return self.ledger["return"]

    @property
    def mean_one_way_turnover(self) -> float:
        if self.ledger.empty:
            return float("nan")
        return float(self.ledger["one_way_turnover"].mean())

    @property
    def average_holding_sessions(self) -> float:
        return average_holding_sessions(self.positions)


@dataclass
class BacktestOutput:
    """Everything `backtest.md` and the CSVs are built from."""

    common_start: pd.Timestamp
    books: dict[str, BookResult]
    scores: dict[str, pd.DataFrame]
    weights: dict[str, pd.DataFrame]
    summary: pd.DataFrame
    cost_grid: pd.DataFrame
    breakeven_bps: dict[str, float]
    score_corr: pd.DataFrame
    report_path: object
    figures: dict[str, object] = field(default_factory=dict)


def breakeven_cost_bps(costs: list[float], excess: list[float]) -> float:
    """Cost, in basis points, where excess CAGR versus the benchmark crosses zero.

    `math.inf` means the strategy is still ahead at the most expensive
    cost on the grid. `math.nan` means it is already behind at the
    cheapest cost, so there is no positive breakeven to report — the
    edge is not there before costs either.
    """
    if not costs or len(costs) != len(excess):
        raise ValueError("costs and excess must be the same non-empty length")
    if excess[0] <= 0:
        return float("nan")
    for i in range(1, len(costs)):
        if excess[i] <= 0:
            span = excess[i - 1] - excess[i]
            if span == 0:
                return float(costs[i])
            t = excess[i - 1] / span
            return float(costs[i - 1] + t * (costs[i] - costs[i - 1]))
    return float("inf")


def average_holding_sessions(positions: pd.DataFrame) -> float:
    """Mean length of consecutive holding spells, in decision sessions.

    A spell breaks when a name is absent for a decision. The unit is
    sessions, not calendar days, because the simulator only acts on
    sessions.
    """
    if positions.empty:
        return float("nan")
    lengths: list[int] = []
    for _, group in positions.groupby("symbol", sort=False):
        idxs = sorted(int(i) for i in group["decision_idx"].unique())
        spell = 1
        for prev, cur in zip(idxs, idxs[1:]):
            if cur == prev + 1:
                spell += 1
            else:
                lengths.append(spell)
                spell = 1
        lengths.append(spell)
    if not lengths:
        return float("nan")
    return float(np.mean(lengths))


def _usable_price(frame: pd.DataFrame) -> pd.DataFrame:
    """True where a price is finite and strictly positive."""
    return np.isfinite(frame) & (frame > 0)


def simulate(weights: pd.DataFrame, raw_open: pd.DataFrame, adj_open: pd.DataFrame,
             cost_bps: float, *, name: str) -> BookResult:
    """Turn decision-time weights into a ledger of completed holds.

    `weights` is indexed by the decision date: the close the signal was
    computed on. The position is entered at the next session's open and
    marked at the open after that. A weight dated on the last two
    sessions of the calendar cannot complete that round trip and is not
    booked. That is a missing trade, not a zero return.

    Cost is charged at entry, on the sum of absolute weight changes,
    before the subsequent open-to-open move. One-way turnover is half of
    that sum: replacing the whole book is 1.

    The shifts are by position on the trading calendar, not by a fixed
    number of calendar days, so a weekend is not a session. Doing it on
    the whole frame rather than one session at a time does not change
    the rule; it is the same rule applied to every row.
    """
    if cost_bps < 0:
        raise ValueError(f"cost_bps must be >= 0, got {cost_bps}")

    calendar = pd.DatetimeIndex(adj_open.index.sort_values().unique())
    symbols = list(weights.columns)
    raw = raw_open.reindex(index=calendar, columns=symbols)
    adj = adj_open.reindex(index=calendar, columns=symbols)

    decision_index = pd.DatetimeIndex(pd.DatetimeIndex(weights.index).sort_values().unique())
    locs = calendar.get_indexer(decision_index)
    keep = (locs >= 0) & (locs + 2 < len(calendar))
    decision_index = decision_index[keep]
    locs = locs[keep]

    empty_ledger = pd.DataFrame(columns=["decision_date", "entry_date", "return", "gross_return",
                                         "cost", "one_way_turnover", "unfilled_weight", "nav"])
    if len(decision_index) == 0:
        return BookResult(name=name, ledger=empty_ledger,
                          trades=pd.DataFrame(), positions=pd.DataFrame())

    target = (weights.reindex(index=decision_index, columns=symbols)
              .fillna(0.0)
              .astype(float))
    entry_dates = pd.DatetimeIndex(calendar[locs + 1])
    mark_dates = pd.DatetimeIndex(calendar[locs + 2])
    # Pull the entry and mark sessions back onto the decision index, so
    # every column of the arithmetic below refers to the same decision.
    entry_raw = raw.loc[entry_dates].set_axis(decision_index)
    entry_adj = adj.loc[entry_dates].set_axis(decision_index)
    mark_adj = adj.loc[mark_dates].set_axis(decision_index)

    can_fill = (target > 0) & _usable_price(entry_raw) & _usable_price(entry_adj) & _usable_price(mark_adj)
    filled = target.where(can_fill, 0.0)
    prev = filled.shift(1).fillna(0.0)
    delta = filled - prev
    traded = delta.abs().sum(axis=1)
    one_way = 0.5 * traded
    cost = traded * (cost_bps / 10_000.0)
    # 0 * NaN would be NaN. A name we did not fill contributes 0, not a hole.
    asset_ret = (mark_adj / entry_adj) - 1.0
    gross = (filled * asset_ret).fillna(0.0).sum(axis=1)
    net = (1.0 - cost) * (1.0 + gross) - 1.0
    nav = (1.0 + net).cumprod()
    unfilled = (target.clip(lower=0.0) - filled).sum(axis=1)

    ledger = pd.DataFrame({
        "decision_date": decision_index,
        "entry_date": entry_dates,
        "return": net.to_numpy(),
        "gross_return": gross.to_numpy(),
        "cost": cost.to_numpy(),
        "one_way_turnover": one_way.to_numpy(),
        "unfilled_weight": unfilled.to_numpy(),
        "nav": nav.to_numpy(),
    }, index=pd.DatetimeIndex(mark_dates, name="mark_date"))

    trades = _changes(delta, prev, filled, entry_raw, entry_dates, decision_index)
    positions = _holdings(filled, entry_raw, entry_adj, entry_dates, mark_dates, decision_index)
    return BookResult(name=name, ledger=ledger, trades=trades, positions=positions)


def _changes(delta: pd.DataFrame, prev: pd.DataFrame, filled: pd.DataFrame,
             entry_raw: pd.DataFrame, entry_dates: pd.DatetimeIndex,
             decision_index: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per (decision, symbol) whose weight actually changed."""
    changed = delta.where(delta.abs() > 1e-12)
    changed.index.name = "decision_date"
    changed.columns.name = "symbol"
    long = changed.stack().dropna().rename("delta").reset_index()
    if long.empty:
        return pd.DataFrame(columns=["decision_date", "entry_date", "symbol",
                                     "weight_before", "weight_after", "delta", "raw_open"])
    entry_map = pd.Series(entry_dates, index=decision_index)
    long["entry_date"] = long["decision_date"].map(entry_map)
    long["weight_before"] = _lookup(prev, long["decision_date"], long["symbol"])
    long["weight_after"] = _lookup(filled, long["decision_date"], long["symbol"])
    long["raw_open"] = _lookup(entry_raw, long["decision_date"], long["symbol"])
    return long.loc[:, ["decision_date", "entry_date", "symbol",
                        "weight_before", "weight_after", "delta", "raw_open"]]


def _holdings(filled: pd.DataFrame, entry_raw: pd.DataFrame, entry_adj: pd.DataFrame,
              entry_dates: pd.DatetimeIndex, mark_dates: pd.DatetimeIndex,
              decision_index: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per (decision, symbol) that was actually filled."""
    held = filled.where(filled > 0)
    held.index.name = "decision_date"
    held.columns.name = "symbol"
    long = held.stack().dropna().rename("weight").reset_index()
    if long.empty:
        return pd.DataFrame(columns=["decision_idx", "decision_date", "entry_date", "mark_date",
                                     "symbol", "weight", "raw_open", "adj_open"])
    entry_map = pd.Series(entry_dates, index=decision_index)
    mark_map = pd.Series(mark_dates, index=decision_index)
    idx_map = pd.Series(np.arange(len(decision_index)), index=decision_index)
    long["decision_idx"] = long["decision_date"].map(idx_map).astype(int)
    long["entry_date"] = long["decision_date"].map(entry_map)
    long["mark_date"] = long["decision_date"].map(mark_map)
    long["raw_open"] = _lookup(entry_raw, long["decision_date"], long["symbol"])
    long["adj_open"] = _lookup(entry_adj, long["decision_date"], long["symbol"])
    return long.loc[:, ["decision_idx", "decision_date", "entry_date", "mark_date",
                        "symbol", "weight", "raw_open", "adj_open"]]


def _lookup(frame: pd.DataFrame, dates: pd.Series, symbols: pd.Series) -> np.ndarray:
    """frame.loc[date, symbol] for a pair of aligned series. Missing is NaN."""
    row = frame.index.get_indexer(pd.DatetimeIndex(dates))
    col = frame.columns.get_indexer(symbols)
    values = frame.to_numpy(dtype=float)
    out = np.full(len(dates), np.nan)
    ok = (row >= 0) & (col >= 0)
    out[ok] = values[row[ok], col[ok]]
    return out


def metrics_row(name: str, kind: str, book: BookResult, bench_returns: pd.Series,
                rf_daily: float, periods: int, *, split: str | None = None) -> dict[str, object]:
    """Performance / risk metrics for one book. Pure of I/O."""
    rets = book.returns
    dd = analytics.max_drawdown(rets)
    capm = analytics.beta_alpha(rets, bench_returns, rf_daily, periods)
    total = float((1.0 + rets).prod() - 1.0) if len(rets) else float("nan")
    row = {
        "name": name,
        "kind": kind,
        "cagr": analytics.annualised_return(rets, periods),
        "ann_volatility": analytics.annualised_volatility(rets, periods),
        "sharpe": analytics.sharpe_ratio(rets, rf_daily, periods),
        "sortino": analytics.sortino_ratio(rets, rf_daily, periods),
        "max_drawdown": dd["max_drawdown"],
        "drawdown_peak": dd["peak_date"],
        "drawdown_trough": dd["trough_date"],
        "hit_rate": analytics.hit_rate(rets),
        # Empirical 5% VaR of daily returns. Not a Gaussian approximation.
        "var_95": analytics.historical_var(rets, 0.95),
        "total_return": total,
        "final_nav": float(book.ledger["nav"].iloc[-1]) if len(book.ledger) else float("nan"),
        "avg_holding_sessions": book.average_holding_sessions,
        "one_way_turnover": book.mean_one_way_turnover,
        "annual_turnover": book.mean_one_way_turnover * periods,
        "beta": capm["beta"],
        "alpha_annual": capm["alpha_annual"],
        "n_obs": int(len(rets)),
        "first_mark": rets.index[0] if len(rets) else pd.NaT,
        "last_mark": rets.index[-1] if len(rets) else pd.NaT,
        "n_trades": int(len(book.trades)),
        "mean_gross_exposure": float(book.positions.groupby("decision_date")["weight"].sum().mean())
            if len(book.positions) else float("nan"),
    }
    if split is not None:
        row["split"] = split
    return row


# Kept for callers that imported the private name during Milestone 2.
_metrics_row = metrics_row


@dataclass
class PreparedBooks:
    """Scores and target weights ready to simulate on any date window."""

    common_start: pd.Timestamp
    tradable: list[str]
    raw_open: pd.DataFrame
    adj_open: pd.DataFrame
    scores: dict[str, pd.DataFrame]
    weights: dict[str, pd.DataFrame]
    dollar_volume: pd.DataFrame


def prepare_books(cfg: Config, panel: pd.DataFrame) -> PreparedBooks:
    """Score the panel and build capped target weights. No simulation yet.

    Separating prepare from simulate is what makes the same backtester
    reusable for a full-sample run and for chronological train/test
    windows without recomputing features twice with different rules.
    """
    if cfg.strategies is None:
        raise ValueError(
            "config has no `strategies:` section, so there is nothing to backtest. "
            "See config/default.yml."
        )
    spec = cfg.strategies
    tradable = list(cfg.universe.tickers)
    bench = cfg.universe.benchmark
    present = set(panel["symbol"].unique())
    for sym in (*tradable, bench):
        if sym not in present:
            raise ValueError(
                f"{sym} is in the config but has no rows in the curated panel. "
                "Run `python -m marketengine run` before `backtest` / `validate`."
            )

    raw_open = store.wide(panel, "open")
    close = store.wide(panel, "close")
    adj_close = store.wide(panel, "adj_close")
    volume = store.wide(panel, "volume")
    adj_open = features.adjusted_open(raw_open, close, adj_close)
    # Dollar volume uses the as-traded close and volume: that is the
    # liquidity that was on the tape, not a total-return reconstruction.
    dollar_volume = close.reindex(columns=tradable) * volume.reindex(columns=tradable)

    prices = adj_close.reindex(columns=tradable)
    score_map = strategies.score_all(
        prices,
        momentum_window=spec.momentum.window,
        momentum_skip=spec.momentum.skip,
        reversal_return_window=spec.reversal.return_window,
        reversal_vol_window=spec.reversal.vol_window,
        breakout_window=spec.breakout.window,
    )
    weight_map = {
        name: strategies.target_weights(frame, spec.portfolio.n_long)
        for name, frame in score_map.items()
    }
    weight_map = {
        name: strategies.apply_portfolio_caps(
            frame,
            dollar_volume,
            max_weight=spec.portfolio.max_weight,
            max_participation=spec.portfolio.max_participation,
            adv_window=spec.portfolio.adv_window,
            assumed_nav_usd=spec.portfolio.assumed_nav_usd,
        )
        for name, frame in weight_map.items()
    }
    common = max(strategies.first_invested(w) for w in weight_map.values())
    weight_map = {name: frame.loc[common:] for name, frame in weight_map.items()}
    score_window = {name: frame.loc[common:] for name, frame in score_map.items()}
    return PreparedBooks(
        common_start=common,
        tradable=tradable,
        raw_open=raw_open,
        adj_open=adj_open,
        scores=score_window,
        weights=weight_map,
        dollar_volume=dollar_volume.loc[common:],
    )


def simulate_prepared(prepared: PreparedBooks, cfg: Config, *,
                      decision_index: pd.DatetimeIndex | None = None,
                      cost_bps: float | None = None,
                      name_suffix: str = "") -> dict[str, BookResult]:
    """Simulate strategies and both benchmarks on a decision window."""
    spec = cfg.strategies
    assert spec is not None
    bench = cfg.universe.benchmark
    cost = spec.portfolio.cost_bps if cost_bps is None else cost_bps
    if decision_index is None:
        decision_index = prepared.weights["momentum"].index
    else:
        decision_index = pd.DatetimeIndex(decision_index).intersection(
            prepared.weights["momentum"].index)

    books: dict[str, BookResult] = {}
    for name, weights in prepared.weights.items():
        w = weights.reindex(decision_index).fillna(0.0)
        label = f"{name}{name_suffix}"
        books[name] = simulate(w, prepared.raw_open, prepared.adj_open, cost, name=label)

    ew = pd.DataFrame(1.0 / len(prepared.tradable), index=decision_index,
                      columns=prepared.tradable)
    if spec.portfolio.max_weight is not None or spec.portfolio.max_participation is not None:
        ew = strategies.apply_portfolio_caps(
            ew, prepared.dollar_volume,
            max_weight=spec.portfolio.max_weight,
            max_participation=spec.portfolio.max_participation,
            adv_window=spec.portfolio.adv_window,
            assumed_nav_usd=spec.portfolio.assumed_nav_usd,
        )
    spy = pd.DataFrame({bench: 1.0}, index=decision_index)
    books[BENCHMARK_EW] = simulate(
        ew, prepared.raw_open, prepared.adj_open, cost, name=f"{BENCHMARK_EW}{name_suffix}")
    books[bench] = simulate(
        spy, prepared.raw_open, prepared.adj_open, cost, name=f"{bench}{name_suffix}")
    return books


def cost_sweep(prepared: PreparedBooks, cfg: Config,
               decision_index: pd.DatetimeIndex | None = None) -> tuple[pd.DataFrame, dict[str, float]]:
    """CAGR at each configured cost, plus breakeven versus the benchmark."""
    spec = cfg.strategies
    assert spec is not None
    bench = cfg.universe.benchmark
    an = cfg.analytics
    grid = list(spec.portfolio.cost_grid_bps)
    if decision_index is None:
        decision_index = prepared.weights["momentum"].index

    cagr_by_cost: dict[str, list[float]] = {
        name: [] for name in (*strategies.NAMES, bench)
    }
    for bps in grid:
        books = simulate_prepared(prepared, cfg, decision_index=decision_index, cost_bps=bps)
        for name in cagr_by_cost:
            cagr_by_cost[name].append(
                analytics.annualised_return(books[name].returns, an.trading_days_per_year))

    rows: list[dict[str, object]] = []
    for name in strategies.NAMES:
        for i, bps in enumerate(grid):
            bench_cagr = cagr_by_cost[bench][i]
            cagr = cagr_by_cost[name][i]
            rows.append({
                "name": name,
                "cost_bps": bps,
                "cagr": cagr,
                "benchmark_cagr": bench_cagr,
                "excess_cagr": cagr - bench_cagr,
            })
    cost_grid = pd.DataFrame(rows)
    breakeven = {
        name: breakeven_cost_bps(
            grid, [c - s for c, s in zip(cagr_by_cost[name], cagr_by_cost[bench])])
        for name in strategies.NAMES
    }
    return cost_grid, breakeven


def run_backtest(cfg: Config, panel: pd.DataFrame) -> BacktestOutput:
    """Score, simulate, and write the backtest artefacts.

    Reads the curated panel only. No network. The same prepare/simulate
    path is what `validate` reuses on chronological windows.
    """
    if cfg.strategies is None:
        raise ValueError(
            "config has no `strategies:` section, so there is nothing to backtest. "
            "See config/default.yml."
        )
    spec = cfg.strategies
    bench = cfg.universe.benchmark
    an = cfg.analytics
    prepared = prepare_books(cfg, panel)

    print("[backtest] scoring and simulating")
    print(f"      common window starts {prepared.common_start.date()} "
          f"(warmup before every strategy can fire is dropped)")
    cost_note = f"{spec.portfolio.cost_bps:g} bp one-way"
    if spec.portfolio.commission_bps is not None:
        cost_note += (f" ({spec.portfolio.commission_bps:g} commission + "
                      f"{spec.portfolio.slippage_bps:g} slippage)")
    print(f"      long-only top {spec.portfolio.n_long} of {len(prepared.tradable)}, "
          f"{cost_note}")
    if spec.portfolio.max_weight is not None:
        print(f"      max_weight={spec.portfolio.max_weight:.0%}, "
              f"max_participation={spec.portfolio.max_participation}, "
              f"assumed_nav=${spec.portfolio.assumed_nav_usd:,.0f}")

    books = simulate_prepared(prepared, cfg)
    bench_rets = books[bench].returns
    rows = []
    for name in (*strategies.NAMES, BENCHMARK_EW, bench):
        kind = "benchmark" if name in (BENCHMARK_EW, bench) else "strategy"
        rows.append(metrics_row(name, kind, books[name], bench_rets,
                                an.risk_free_daily, an.trading_days_per_year))
    summary = pd.DataFrame(rows)

    cost_grid, breakeven = cost_sweep(prepared, cfg)
    score_corr = strategies.score_correlation(prepared.scores)

    _write_tables(cfg, prepared.scores, prepared.weights, books, summary, cost_grid, score_corr)
    figures = _write_figures(cfg, books, cost_grid, summary)
    from . import backtest_report

    report_path = backtest_report.write_report(
        cfg,
        common_start=prepared.common_start,
        books=books,
        summary=summary,
        cost_grid=cost_grid,
        breakeven_bps=breakeven,
        score_corr=score_corr,
        figures=figures,
    )
    _print_headline(summary, breakeven, score_corr, spec.portfolio.cost_bps, bench)
    print(f"      report  -> {report_path}")

    return BacktestOutput(
        common_start=prepared.common_start,
        books=books,
        scores=prepared.scores,
        weights=prepared.weights,
        summary=summary,
        cost_grid=cost_grid,
        breakeven_bps=breakeven,
        score_corr=score_corr,
        report_path=report_path,
        figures=figures,
    )


def _write_tables(cfg: Config, scores: dict[str, pd.DataFrame],
                  weights: dict[str, pd.DataFrame], books: dict[str, BookResult],
                  summary: pd.DataFrame, cost_grid: pd.DataFrame,
                  score_corr: pd.DataFrame) -> None:
    for name, frame in scores.items():
        store.write_table(cfg.output_dir, f"scores_{name}", frame)
    for name, frame in weights.items():
        store.write_table(cfg.output_dir, f"weights_{name}", frame)
    returns = pd.DataFrame({name: book.returns for name, book in books.items()})
    nav = pd.DataFrame({name: book.ledger["nav"] for name, book in books.items() if len(book.ledger)})
    store.write_table(cfg.output_dir, "backtest_returns", returns)
    store.write_table(cfg.output_dir, "backtest_nav", nav)
    store.write_table(cfg.output_dir, "backtest_summary", summary, index=False)
    store.write_table(cfg.output_dir, "backtest_cost_grid", cost_grid, index=False)
    store.write_table(cfg.output_dir, "backtest_score_corr", score_corr)
    for name, book in books.items():
        if name not in strategies.NAMES:
            continue
        store.write_table(cfg.output_dir, f"positions_{name}", book.positions, index=False)
        store.write_table(cfg.output_dir, f"trades_{name}", book.trades, index=False)


def _write_figures(cfg: Config, books: dict[str, BookResult],
                   cost_grid: pd.DataFrame, summary: pd.DataFrame) -> dict[str, object]:
    if not cfg.outputs.figures:
        return {}
    bench = cfg.universe.benchmark
    returns = pd.DataFrame({name: books[name].returns for name in
                            (*strategies.NAMES, BENCHMARK_EW, bench)})
    figures = {
        "Growth of $1, strategies against the two benchmarks":
            plots.plot_strategy_equity(
                returns, cfg.figure_dir / "backtest_equity.png", cfg.outputs.dpi,
                benchmark=bench,
                cost_bps=cfg.strategies.portfolio.cost_bps if cfg.strategies else 0,
            ),
        "Drawdown from peak":
            plots.plot_strategy_drawdown(
                returns, cfg.figure_dir / "backtest_drawdown.png", cfg.outputs.dpi),
        "CAGR against one-way cost":
            plots.plot_cost_sweep(
                cost_grid, cfg.figure_dir / "backtest_costs.png", cfg.outputs.dpi),
    }
    # `summary` is accepted so a later column can be plotted without
    # changing the call. Today the three figures above are the ones that
    # answer a question the table does not.
    del summary
    return figures


def _print_headline(summary: pd.DataFrame, breakeven: dict[str, float],
                    score_corr: pd.DataFrame, cost_bps: float, benchmark: str) -> None:
    show = summary.loc[:, ["name", "cagr", "ann_volatility", "sharpe", "max_drawdown",
                           "avg_holding_sessions", "annual_turnover", "alpha_annual"]].copy()
    for col in ("cagr", "ann_volatility", "max_drawdown", "annual_turnover", "alpha_annual"):
        show[col] = show[col].map(lambda v: "n/a" if not np.isfinite(v) else f"{v:.1%}")
    show["sharpe"] = summary["sharpe"].map(lambda v: "n/a" if not np.isfinite(v) else f"{v:.2f}")
    show["avg_holding_sessions"] = summary["avg_holding_sessions"].map(
        lambda v: "n/a" if not np.isfinite(v) else f"{v:.1f}")
    show["annual_turnover"] = summary["annual_turnover"].map(
        lambda v: "n/a" if not np.isfinite(v) else f"{v:.2f}x")
    print(f"      at {cost_bps:g} bp one-way")
    print(show.to_string(index=False))
    parts = []
    for name, bps in breakeven.items():
        if bps == float("inf"):
            parts.append(f"{name}: still ahead of {benchmark} at the top of the cost grid")
        elif not np.isfinite(bps):
            parts.append(f"{name}: behind {benchmark} even at the cheapest cost")
        else:
            parts.append(f"{name}: breakeven vs {benchmark} at {bps:.1f} bp")
    print("      " + "; ".join(parts))
    # The off-diagonal the reader should see without opening the CSV.
    pair = _strongest_pair(score_corr)
    if pair is not None:
        a, b, rho = pair
        print(f"      strongest score correlation: {a} vs {b} = {rho:.2f}")


def _strongest_pair(corr: pd.DataFrame) -> tuple[str, str, float] | None:
    best: tuple[str, str, float] | None = None
    cols = list(corr.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            rho = float(corr.loc[a, b])
            if not np.isfinite(rho):
                continue
            if best is None or abs(rho) > abs(best[2]):
                best = (a, b, rho)
    return best
