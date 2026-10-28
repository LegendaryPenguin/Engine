"""
Milestone 3 validation report.

Train and test side by side, the gap as the headline, and a bias section
that names the risks this engine still carries. Written from the run.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import strategies
from .backtest import BENCHMARK_EW, BookResult, PreparedBooks
from .config import Config
from .validate import Split


def write_report(cfg: Config, *, split: Split, prepared: PreparedBooks,
                 train_summary: pd.DataFrame, test_summary: pd.DataFrame,
                 comparison: pd.DataFrame, flags: pd.DataFrame,
                 train_cost_grid: pd.DataFrame, test_cost_grid: pd.DataFrame,
                 train_breakeven: dict[str, float], test_breakeven: dict[str, float],
                 train_books: dict[str, BookResult], test_books: dict[str, BookResult],
                 figures: dict[str, object]) -> Path:
    spec = cfg.strategies
    val = cfg.validation
    assert spec is not None and val is not None
    bench = cfg.universe.benchmark
    lines: list[str] = []

    def w(text: str = "") -> None:
        lines.append(text)

    w(f"# Validation — {cfg.run_id}")
    w()
    w(_lede(cfg, split, prepared, comparison, flags))
    w()

    w("## 1. Method")
    w()
    w(f"- **Universe.** {len(prepared.tradable)} tradable names + {bench}. "
      f"Listed in `config/validation.yml`. Still a list of names that exist "
      f"today — survivorship is not cured by listing more of them.")
    w(f"- **Strategies.** The Milestone 2 rules, unchanged: 20-day momentum "
      f"(skip 1), 5-day vol-scaled reversal, 20-day breakout. Top "
      f"{spec.portfolio.n_long}, long only.")
    w(f"- **Costs.** {spec.portfolio.cost_bps:g} bp one-way"
      + (f" = {spec.portfolio.commission_bps:g} commission + "
         f"{spec.portfolio.slippage_bps:g} slippage"
         if spec.portfolio.commission_bps is not None else "")
      + f". Sweep at {list(spec.portfolio.cost_grid_bps)} bp.")
    if spec.portfolio.max_weight is not None:
        w(f"- **Portfolio caps.** max_weight={spec.portfolio.max_weight:.0%}; "
          f"max_participation={spec.portfolio.max_participation:.0%} of "
          f"{spec.portfolio.adv_window}-day median dollar volume at an "
          f"assumed book of ${spec.portfolio.assumed_nav_usd:,.0f}. "
          f"Clipped mass stays cash.")
    w(f"- **Fill.** Signal at t's close → raw open of t+1. P&L on the "
      f"adjusted open. No intraday stop.")
    w(f"- **Split.** Chronological {val.train_fraction:.0%} / "
      f"{1 - val.train_fraction:.0%} on decision dates after warmup. "
      f"Boundary **{split.boundary.date()}**. Train "
      f"{split.train_index[0].date()} → {split.train_index[-1].date()} "
      f"({len(split.train_index)} decisions). Test "
      f"{split.test_index[0].date()} → {split.test_index[-1].date()} "
      f"({len(split.test_index)} decisions). No shuffle.")
    w(f"- **Config hash.** `{cfg.source_sha256}` — the exact bytes of "
      f"`{cfg.source_path.as_posix()}`.")
    w("- **Parameters.** Not searched against the test window. Fixed in "
      "Milestone 2 / this config before the test metrics were computed.")
    w()

    w("## 2. Train vs test")
    w()
    w(_table(_display_comparison(comparison, bench)))
    w()
    w("CAGR, vol, Sharpe, Sortino, max drawdown, empirical 95% VaR, hit rate, "
      "holding period, turnover, and CAPM alpha — train and test. The gap "
      "columns are the headline. VaR is the empirical 5th percentile of daily "
      "returns, not a Gaussian approximation.")
    w()
    for name in strategies.NAMES:
        w(_strategy_block(name, comparison, flags, train_books, test_books, bench))
        w()

    ew = _row(comparison, BENCHMARK_EW)
    spy = _row(comparison, bench)
    w(f"**Equal-weight buy-and-hold** of the same universe: train CAGR "
      f"{_pct(ew['train_cagr'])}, test CAGR {_pct(ew['test_cagr'])}. "
      f"**{bench}** train {_pct(spy['train_cagr'])}, test {_pct(spy['test_cagr'])}. "
      + _null_sentence(comparison, bench))
    w()

    w("## 3. Overfitting check")
    w()
    w(f"A strategy is flagged when its test Sharpe is more than "
      f"{val.overfitting_sharpe_drop:.0%} below its train Sharpe "
      f"(test < train × {1 - val.overfitting_sharpe_drop:.2f}), or when "
      f"train Sharpe is already non-positive and test is worse. This is "
      f"reported in the output, not omitted.")
    w()
    w(_table(_display_flags(flags)))
    w()
    flagged = flags.loc[flags["overfit_flag"], "name"].tolist()
    if flagged:
        w(f"**Flagged:** {', '.join(flagged)}. Treat the train result as "
          f"an upper bound the live book has not earned.")
    else:
        w("**No strategy tripped the configured flag.** That is not a "
          "certificate of robustness — the test window is one path, the "
          "universe is survivors, and a quiet flag can still be a lucky draw.")
    w()

    w("## 4. Costs on each window")
    w()
    w("Same weights, resimulated at each cost. Breakeven is versus "
      f"{bench} on that window.")
    w()
    w("**Train**")
    w()
    w(_table(_display_costs(train_cost_grid, bench)))
    w()
    for name in strategies.NAMES:
        w(f"- {name}: {_breakeven(name, train_breakeven[name], bench)}")
    w()
    w("**Test**")
    w()
    w(_table(_display_costs(test_cost_grid, bench)))
    w()
    for name in strategies.NAMES:
        w(f"- {name}: {_breakeven(name, test_breakeven[name], bench)}")
    w()

    w("## 5. Bias and limitation analysis")
    w()
    w("These are the risks that are still live after this milestone. "
      "Naming them is the deliverable; curing them is later work.")
    w()
    w("1. **Look-ahead / leakage.** Features and scores at date t use only "
      "rows with date ≤ t (truncation tests in `tests/test_backtest.py`). "
      "Fills are the next open, never the signal close "
      "(`test_the_fill_is_the_next_open_and_not_the_signal_close`). A "
      "provisional last bar from an unclosed session is still not refused "
      "(ROADMAP phase 4) — if this report was built mid-session, the newest "
      "score will move by the close.")
    w("2. **Overfitting.** Parameters were not grid-searched on this sample. "
      "The train/test Sharpe gap is still the honest check: section 3 flags "
      "a drop above the configured threshold. Running the test window more "
      "than once while tuning would invalidate that check; this config's "
      "SHA-256 is recorded so a later retune is visible.")
    w("3. **Data leakage through the universe.** Membership is today's list. "
      "Names that delisted, went bankrupt, or never made it into this YAML "
      "are absent. Every CAGR here is conditional on survival. A "
      "point-in-time constituent list is ROADMAP phase 3.")
    w("4. **Unstable / regime-dependent parameters.** A 20-day momentum "
      "window that worked in a long equity bull tape can fail when "
      "cross-sectional dispersion collapses. The test window is one regime "
      "draw; it is not a stress library.")
    w("5. **Cost model risk.** Commission and slippage are flat bp, identical "
      "across names. TLT and a single stock do not cost the same. The sweep "
      "shows sensitivity to the level, not a market-impact model. "
      "Participation is capped against trailing ADV at an assumed NAV; that "
      "cap is still an assumption about the book size.")
    w("6. **Price-basis risk.** Breakout still scores `adj_close` (total "
      "return). A dividend looks like a downside break. The split-adjusted, "
      "not-dividend-adjusted series is ROADMAP phase 2.")
    w("7. **Survivorship and selection.** Expanding from 10 names to "
      f"{len(prepared.tradable)} diversifies idiosyncratic bets; it does "
      "not remove the bias that every name printed a quote through the "
      "end of the sample.")
    w("8. **Multiple testing.** Three strategies, two benchmarks, a cost "
      "grid, and a train/test cut produce many numbers. The report leads "
      "with the equal-weight null and the overfit flag so a single lucky "
      "Sharpe is not the takeaway.")
    w()

    w("## 6. Figures")
    w()
    for caption, path in figures.items():
        rel = Path(path).relative_to(cfg.report_dir)
        w(f"**{caption}**")
        w()
        w(f"![{caption}]({rel.as_posix()})")
        w()

    w("## 7. Reproducibility")
    w()
    w("```bash")
    w("python -m marketengine --config config/validation.yml run")
    w("python -m marketengine --config config/validation.yml validate")
    w("python submission/milestone3/capture.py")
    w("```")
    w()
    w(f"Config path: `{cfg.source_path.as_posix()}`. SHA-256: "
      f"`{cfg.source_sha256}`. Split boundary: **{split.boundary.date()}**. "
      f"Train fraction: {split.train_fraction:.0%}. Stated cost: "
      f"{spec.portfolio.cost_bps:g} bp one-way.")
    w()

    path = cfg.report_dir / "validation.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _lede(cfg: Config, split: Split, prepared: PreparedBooks,
          comparison: pd.DataFrame, flags: pd.DataFrame) -> str:
    bench = cfg.universe.benchmark
    ew = _row(comparison, BENCHMARK_EW)
    strategies_cmp = comparison.loc[comparison["kind"] == "strategy"]
    beat_ew_test = [str(r["name"]) for _, r in strategies_cmp.iterrows()
                    if np.isfinite(r["test_cagr"]) and r["test_cagr"] > ew["test_cagr"]]
    flagged = flags.loc[flags["overfit_flag"], "name"].tolist()
    if beat_ew_test:
        result = (f"On the untouched test window, {_and(beat_ew_test)} beat "
                  f"equal-weight buy-and-hold of the same "
                  f"{len(prepared.tradable)} names.")
    else:
        result = (f"On the untouched test window, none of the three strategies "
                  f"beat equal-weight buy-and-hold of the same "
                  f"{len(prepared.tradable)} names "
                  f"({_pct(ew['test_cagr'])} CAGR). That is the result.")
    if flagged:
        overfit = f" Overfitting flag: {', '.join(flagged)}."
    else:
        overfit = " No strategy tripped the configured Sharpe-drop overfit flag."
    return (f"{result} Chronological split at **{split.boundary.date()}** "
            f"({cfg.validation.train_fraction:.0%} train / "
            f"{1 - cfg.validation.train_fraction:.0%} test) after warmup "
            f"from {prepared.common_start.date()}. Benchmark {bench}. "
            f"Parameters were not retuned on the test window.{overfit}")


def _strategy_block(name: str, comparison: pd.DataFrame, flags: pd.DataFrame,
                    train_books: dict[str, BookResult],
                    test_books: dict[str, BookResult], bench: str) -> str:
    row = _row(comparison, name)
    flag = flags.loc[flags["name"] == name].iloc[0]
    flag_note = f" Overfit flag: {flag['reason']}." if flag["overfit_flag"] else ""
    return (f"**{name}.** Train CAGR {_pct(row['train_cagr'])} "
            f"(Sharpe {_num(row['train_sharpe'])}), test CAGR "
            f"{_pct(row['test_cagr'])} (Sharpe {_num(row['test_sharpe'])}). "
            f"Gaps: CAGR {_signed_pp(float(row['cagr_gap']))}, Sharpe "
            f"{float(row['sharpe_gap']):+.2f}. Test max drawdown "
            f"{_pct(row['test_max_drawdown'])}, empirical 95% VaR "
            f"{_pct(row['test_var_95'])} per day, turnover "
            f"{_turns(row['test_annual_turnover'])}, mean gross exposure "
            f"{_pct(row['test_mean_gross_exposure'])}. "
            f"Vs {bench} test alpha {_pct(row['test_alpha_annual'])}/yr."
            f"{flag_note}")


def _null_sentence(comparison: pd.DataFrame, bench: str) -> str:
    ew = _row(comparison, BENCHMARK_EW)
    spy = _row(comparison, bench)
    if float(ew["test_cagr"]) + 0.005 < float(spy["test_cagr"]):
        return (f"On the test window the universe lagged {bench}, so a "
                f"strategy that only reshuffles these names starts behind.")
    if float(ew["test_cagr"]) > float(spy["test_cagr"]) + 0.005:
        return (f"On the test window the universe itself beat {bench}, so "
                f"clearing the index is not enough — clearing equal-weight "
                f"is the harder null.")
    return f"Universe and {bench} were close on the test window."


def _breakeven(name: str, bps: float, bench: str) -> str:
    if bps == float("inf"):
        return f"still ahead of {bench} at the top of the cost grid"
    if not np.isfinite(bps):
        return f"behind {bench} even at the cheapest cost on the grid"
    return f"breakeven vs {bench} near {bps:.1f} bp one-way"


def _row(frame: pd.DataFrame, name: str) -> pd.Series:
    hit = frame.loc[frame["name"] == name]
    if hit.empty:
        raise KeyError(name)
    return hit.iloc[0]


def _pct(value: object) -> str:
    v = float(value)  # type: ignore[arg-type]
    if not np.isfinite(v):
        return "n/a"
    return f"{v:.1%}"


def _num(value: object, digits: int = 2) -> str:
    v = float(value)  # type: ignore[arg-type]
    if not np.isfinite(v):
        return "n/a"
    return f"{v:.{digits}f}"


def _turns(value: object) -> str:
    v = float(value)  # type: ignore[arg-type]
    if not np.isfinite(v):
        return "n/a"
    return f"{v:.2f}x"


def _signed_pp(value: float) -> str:
    if not np.isfinite(value):
        return "n/a"
    return f"{value * 100:+.1f} pp"


def _and(names: list[str]) -> str:
    shown = [n[0].upper() + n[1:] for n in names]
    if len(shown) == 1:
        return shown[0]
    if len(shown) == 2:
        return f"{shown[0]} and {shown[1].lower()}"
    return ", ".join(shown[:-1]) + f", and {shown[-1].lower()}"


def _display_comparison(comparison: pd.DataFrame, bench: str) -> pd.DataFrame:
    rows = []
    for _, r in comparison.iterrows():
        rows.append({
            "book": r["name"],
            "train CAGR": _pct(r["train_cagr"]),
            "test CAGR": _pct(r["test_cagr"]),
            "CAGR gap": _signed_pp(float(r["cagr_gap"])),
            "train Sharpe": _num(r["train_sharpe"]),
            "test Sharpe": _num(r["test_sharpe"]),
            "Sharpe gap": _num(r["sharpe_gap"]),
            "test max DD": _pct(r["test_max_drawdown"]),
            "test VaR 95%": _pct(r["test_var_95"]),
            "test turnover": _turns(r["test_annual_turnover"]),
        })
    del bench
    return pd.DataFrame(rows)


def _display_flags(flags: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in flags.iterrows():
        rows.append({
            "strategy": r["name"],
            "train Sharpe": _num(r["train_sharpe"]),
            "test Sharpe": _num(r["test_sharpe"]),
            "flag": "YES" if r["overfit_flag"] else "no",
            "note": r["reason"] or "—",
        })
    return pd.DataFrame(rows)


def _display_costs(cost_grid: pd.DataFrame, bench: str) -> pd.DataFrame:
    costs = list(dict.fromkeys(cost_grid["cost_bps"].tolist()))
    rows = []
    for name in strategies.NAMES:
        sub = cost_grid.loc[cost_grid["name"] == name].set_index("cost_bps")
        row: dict[str, str] = {"book": name}
        for bps in costs:
            row[f"{bps:g} bp"] = _pct(sub.loc[bps, "cagr"]) if bps in sub.index else "n/a"
        rows.append(row)
    first = cost_grid.drop_duplicates("cost_bps").set_index("cost_bps")
    bench_row: dict[str, str] = {"book": bench}
    for bps in costs:
        bench_row[f"{bps:g} bp"] = _pct(first.loc[bps, "benchmark_cagr"])
    rows.append(bench_row)
    return pd.DataFrame(rows)


def _table(frame: pd.DataFrame) -> str:
    try:
        return frame.to_markdown(index=False, disable_numparse=True)
    except (ImportError, TypeError):
        return frame.to_string(index=False)
