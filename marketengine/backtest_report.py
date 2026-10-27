"""
The Milestone 2 report.

Written from the run, not edited afterwards. A sentence that names a
winner is a comparison of two numbers this function just computed; if
the numbers change, the sentence changes. That is the same rule as
`summary.md`: a claim that can drift away from the artefact is worse
than no claim.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import strategies
from .backtest import BENCHMARK_EW, BookResult
from .config import Config


def write_report(cfg: Config, *, common_start: pd.Timestamp,
                 books: dict[str, BookResult], summary: pd.DataFrame,
                 cost_grid: pd.DataFrame, breakeven_bps: dict[str, float],
                 score_corr: pd.DataFrame, figures: dict[str, object]) -> Path:
    spec = cfg.strategies
    assert spec is not None
    bench = cfg.universe.benchmark
    cost = spec.portfolio.cost_bps
    lines: list[str] = []

    def w(text: str = "") -> None:
        lines.append(text)

    spy = _row(summary, bench)
    ew = _row(summary, BENCHMARK_EW)
    first = pd.Timestamp(spy["first_mark"])
    last = pd.Timestamp(spy["last_mark"])

    w(f"# Backtest — {cfg.run_id}")
    w()
    w(f"Three long-only strategies on the configured universe "
      f"({', '.join(cfg.universe.tickers)}), marked from {first.date()} to "
      f"{last.date()}. Signals start being tradable on {pd.Timestamp(common_start).date()}; "
      f"sessions before that are warmup and are not in these numbers. "
      f"The benchmark is {bench}. The second benchmark is an equal-weight "
      f"buy-and-hold of the same names, which is the honest null: it is what "
      f"you get for owning the universe and doing nothing else.")
    w()
    w(_verdict(summary, bench, cost))
    w()

    w("## 1. The rules")
    w()
    w("Each strategy emits a score per (date, symbol) and nothing else. "
      "The book is built afterwards, the same way for all three: hold the "
      f"top {spec.portfolio.n_long} scores in equal weight "
      f"(`1 / {spec.portfolio.n_long}` each), long only, and leave any "
      "unfilled slot in cash. A day with fewer eligible names does not "
      "concentrate into the names that happened to have data.")
    w()
    w("Parameters are the ones in the config. They were set as the "
      "short-horizon rules in `ROADMAP.md` and were not searched against "
      "this sample.")
    w()
    skip = spec.momentum.skip
    skip_phrase = "session" if skip == 1 else f"{skip} sessions"
    w(f"**Momentum.** {strategies.describe('momentum', window=spec.momentum.window, skip=skip_phrase)}")
    w()
    w(f"**Reversal.** {strategies.describe('reversal', return_window=spec.reversal.return_window, vol_window=spec.reversal.vol_window)}")
    w()
    w(f"**Breakout.** {strategies.describe('breakout', window=spec.breakout.window)}")
    w()

    w("## 2. How a score becomes a return")
    w()
    w("- The score on session t uses prices through t's close and no later.")
    w("- The position is entered at the **open of t+1**, never at t's close. "
      "The trade log records that raw open, which is the price that was on the screen.")
    w("- P&L from that open to the next session's open uses the **adjusted** "
      "open: raw open times `adj_close / close` that day. A split that halves "
      "the printed open does not become a -50% return, and a dividend does not "
      "become a loss. The adjustment is the close's own factor, not a model of "
      "the intraday path.")
    w(f"- Cost is **{cost:g} bp per side**, charged on the notional that "
      "changed hands (`sum |Δweight|`), taken out of the account at the entry "
      "open before the move. One-way turnover is half of that sum, so "
      "replacing the whole book is 1.")
    w("- A target whose next open is missing or non-positive is not filled. "
      "Its weight stays cash and earns zero. It is not given a carried price.")
    w("- No intraday stop. A daily bar cannot order the low against the high.")
    w("- Cash earns zero. The Sharpe ratio still subtracts the configured "
      f"daily risk-free rate ({cfg.analytics.risk_free_annual:.2%} annual, "
      "de-annualised geometrically), so a flat book does not look skilled.")
    w("- The last two sessions of the panel are not marked. A completed hold "
      "needs an entry open and a following open, and those sessions do not "
      "have both yet.")
    w()

    w("## 3. Results")
    w()
    w(_table(_display_summary(summary, bench)))
    w()
    w("CAGR is geometric. Volatility is the daily sample standard deviation "
      "times sqrt(252). Sharpe uses the daily risk-free rate. Alpha is the "
      "CAPM intercept, annualised by multiplying the daily intercept by 252, "
      "which is the right operation for an average abnormal return and the "
      "wrong one to confuse with the gap between two CAGRs. Holding period "
      "is the mean length of consecutive spells in the book, in sessions. "
      "Turnover is the mean fraction of the book traded one-way each session, "
      "times 252, written as a multiple of the book: 40x means the positions "
      "were replaced about forty times in a year. The buy-and-hold books are "
      "near 0x because they pay the cost once.")
    w()
    for name in strategies.NAMES:
        w(_strategy_paragraph(name, books[name], _row(summary, name), spy, ew, bench,
                              spec.portfolio.n_long))
        w()

    w(f"Equal-weight buy-and-hold of the universe returned {_pct(ew['cagr'])} "
      f"a year against {bench}'s {_pct(spy['cagr'])}. "
      + _universe_sentence(ew, spy, bench))
    w()

    w("## 4. Costs")
    w()
    w("The same weights, rerun at each cost. The benchmark pays the cost "
      "once, on the way in, so its CAGR barely moves. The strategies pay it "
      "on every rebalance, which is where a signal that looked real goes to die.")
    w()
    w(_table(_display_costs(cost_grid, bench)))
    w()
    for name in strategies.NAMES:
        w(_breakeven_sentence(name, breakeven_bps[name], bench, cost))
    w()
    w("The stated assumption for the headline table is "
      f"{cost:g} bp per side. Anything that only leads at 0 bp is a gross "
      "of the signal, not a strategy.")
    w()

    w("## 5. Are these three strategies or one?")
    w()
    w("Correlation of the scores themselves, stacked over every (date, symbol) "
      "in the common window. Portfolio-return correlation would mostly measure "
      "the fact that all three books hold equities. Score correlation measures "
      "whether the rules agree.")
    w()
    w(_table(_display_corr(score_corr)))
    w()
    w(_corr_sentence(score_corr))
    w()

    w("## 6. Figures")
    w()
    for caption, path in figures.items():
        rel = Path(path).relative_to(cfg.report_dir)
        w(f"**{caption}**")
        w()
        w(f"![{caption}]({rel.as_posix()})")
        w()

    w("## 7. Assumptions that change the numbers")
    w()
    w("1. **Total-return prices for every signal.** Momentum and reversal are "
      "return signals, so `adj_close` is the right series. The breakout is a "
      "price pattern. It should be measured on a split-adjusted series that "
      "has **not** been dividend-adjusted, because a dividend is not a break "
      "of support. That series does not exist yet (ROADMAP phase 2), so the "
      "breakout uses `adj_close` too. On a high-dividend name a payout looks "
      "like a step down, and the breakout score moves for a reason that is "
      "not a price.")
    w("2. **Adjusted open = raw open × (adj_close / close).** This is a "
      "reconstruction, not a vendor field. It is right about splits and "
      "about the close-to-close dividend, and silent about any corporate "
      "action that did not land in `adj_close`.")
    w("3. **Long only, equal weight, fixed top-N.** No short book, no "
      "volatility targeting, no cap on how much of a day's volume the weight "
      "implies. A 33% weight in one name is a position a real book of any "
      "size would have to justify, and nothing here checks the volume.")
    w(f"4. **A flat {cost:g} bp one-way cost**, the same for every name. "
      "TLT and a single stock do not cost the same to trade. The sweep in "
      "section 4 is the sensitivity to that number, not a market-impact model.")
    w("5. **No intraday stop, and no same-bar fill.** Both are consequences "
      "of using daily bars, not omissions to be patched by assuming a path "
      "inside the bar.")
    w("6. **The universe is ten names that exist today.** Every result on it "
      "is conditional on their survival. A name that was delisted along the "
      "way is not here to have lost money, and the fix is a point-in-time "
      "membership list, which this milestone does not have.")
    w("7. **Cash earns zero**, while Sharpe subtracts a flat "
      f"{cfg.analytics.risk_free_annual:.2%} risk-free rate. An unfilled slot "
      "is therefore a drag against both the benchmark and the Sharpe ratio.")
    w("8. **The warmup is dropped, and the last two sessions are not marked.** "
      "The window in this report is the one on which every strategy could "
      "actually fire and every hold could actually complete.")
    w("9. **A provisional last bar is not refused.** If this report was built "
      "on a session that had not closed, the newest close is a partial print "
      "and the newest score will change by the end of the day. Hard refusal "
      "of that bar is ROADMAP phase 4. It is not implemented here.")
    w("10. **Parameters were not fit to this sample.** That is a choice. It "
      "means a disappointing CAGR is evidence about the rule, not a cue to "
      "go and find a window that would have worked.")
    w()

    path = cfg.report_dir / "backtest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _row(summary: pd.DataFrame, name: str) -> pd.Series:
    hit = summary.loc[summary["name"] == name]
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
    """Annual one-way turnover as a multiple of the book.

    40x means the positions were replaced about forty times over the year.
    A percent sign on that number reads as a bug, so it is not one.
    """
    v = float(value)  # type: ignore[arg-type]
    if not np.isfinite(v):
        return "n/a"
    return f"{v:.2f}x"


def _signed_pp(value: float) -> str:
    """A CAGR gap, in percentage points, with a sign."""
    if not np.isfinite(value):
        return "n/a"
    return f"{value * 100:+.1f} pp"


def _pretty(name: str) -> str:
    if name == "equal_weight":
        return "equal-weight"
    if name.isupper():
        return name
    return name[0].upper() + name[1:]


def _and(names: list[str]) -> str:
    """First name capitalised, the rest left as ordinary words. SPY stays SPY."""
    shown = [_pretty(n) for n in names]
    if len(shown) > 1:
        rest = []
        for name in shown[1:]:
            rest.append(name if name.isupper() else name[0].lower() + name[1:])
        shown = [shown[0], *rest]
    if len(shown) == 1:
        return shown[0]
    if len(shown) == 2:
        return f"{shown[0]} and {shown[1]}"
    return ", ".join(shown[:-1]) + f", and {shown[-1]}"


def _verdict(summary: pd.DataFrame, bench: str, cost: float) -> str:
    spy = _row(summary, bench)
    ew = _row(summary, BENCHMARK_EW)
    strategies_only = summary.loc[summary["kind"] == "strategy"]
    beat_ew = [str(r["name"]) for _, r in strategies_only.iterrows()
               if np.isfinite(r["cagr"]) and r["cagr"] > float(ew["cagr"])]
    beat_spy = [str(r["name"]) for _, r in strategies_only.iterrows()
                if np.isfinite(r["cagr"]) and r["cagr"] > float(spy["cagr"])]
    if not beat_ew:
        lead = (f"None of the three strategies beat equal-weight buy-and-hold "
                f"of the same names ({_pct(ew['cagr'])} CAGR) after {cost:g} bp. "
                f"That is the result that matters: reshuffling this universe, "
                f"after costs, did not beat owning it.")
    else:
        lead = (f"{_and(beat_ew)} beat equal-weight buy-and-hold of the same "
                f"names ({_pct(ew['cagr'])} CAGR) after {cost:g} bp.")
    if not beat_spy:
        follow = f" None of them beat buy-and-hold {bench} ({_pct(spy['cagr'])}) either."
    else:
        verb = "beats" if len(beat_spy) == 1 else "beat"
        follow = (f" {_and(beat_spy)} {verb} {bench} ({_pct(spy['cagr'])}). "
                  f"A lead over the index, on a list of names that exist today, "
                  f"is not yet a tradable edge. Section 7 is why.")
    return lead + follow


def _strategy_paragraph(name: str, book: BookResult, row: pd.Series,
                        spy: pd.Series, ew: pd.Series, bench: str,
                        n_long: int) -> str:
    gap_spy = float(row["cagr"]) - float(spy["cagr"])
    gap_ew = float(row["cagr"]) - float(ew["cagr"])
    held, share = _most_held(book)
    hold = float(row["avg_holding_sessions"])
    if hold <= 20:
        hold_note = "inside the one-day to one-month band this engine is built for"
    else:
        hold_note = ("longer than a month. The signal is recomputed every session, "
                     "but a name that stays in the top ranks is not traded out, "
                     "so the holding period is an output of the ranks rather than "
                     "a parameter that was set")
    concentration = ""
    if held is not None and np.isfinite(share):
        if share >= 0.5:
            concentration = (f" {held} was in the book on {share:.0%} of decision "
                             f"dates. With {n_long} slots, that is a concentrated "
                             f"bet, and the CAGR is not separable from the path of "
                             f"{held} without a larger universe.")
        else:
            concentration = (f" No single name occupied a slot on a majority of "
                             f"days. The most frequent was {held} at {share:.0%}.")
    return (f"**{name}.** CAGR {_pct(row['cagr'])} ({_signed_pp(gap_spy)} versus "
            f"{bench}, {_signed_pp(gap_ew)} versus equal-weight), volatility "
            f"{_pct(row['ann_volatility'])}, Sharpe {_num(row['sharpe'])}, "
            f"max drawdown {_pct(row['max_drawdown'])}, CAPM alpha "
            f"{_pct(row['alpha_annual'])} a year. Average holding period "
            f"{hold:.1f} sessions, {hold_note}. Annualised one-way turnover "
            f"{_turns(row['annual_turnover'])}.{concentration}")


def _universe_sentence(ew: pd.Series, spy: pd.Series, bench: str) -> str:
    if float(ew["cagr"]) + 0.005 < float(spy["cagr"]):
        return (f"The universe itself lagged {bench}, so a strategy that "
                f"merely reshuffles these names starts the comparison behind.")
    if float(ew["cagr"]) > float(spy["cagr"]) + 0.005:
        return (f"The universe itself beat {bench}, so a strategy has to clear "
                f"a higher bar than the index before it has done anything "
                f"the passive book of the same names did not already do.")
    return f"The universe and {bench} were close, so the index is a fair null for this sample."


def _breakeven_sentence(name: str, bps: float, bench: str, stated: float) -> str:
    if bps == float("inf"):
        return (f"**{name}** is still ahead of {bench} at the top of the cost "
                f"grid. The stated {stated:g} bp assumption is inside that range, "
                f"not past it.")
    if not np.isfinite(bps):
        return (f"**{name}** lags {bench} even at the cheapest cost on the grid. "
                f"There is no breakeven to quote: charging more does not create "
                f"an edge that was absent at zero.")
    if bps < stated:
        return (f"**{name}** breaks even with {bench} near {bps:.1f} bp one-way, "
                f"which is below the stated {stated:g} bp. At the cost this "
                f"report uses, the lead over {bench} is already gone.")
    return (f"**{name}** breaks even with {bench} near {bps:.1f} bp one-way. "
            f"The stated {stated:g} bp cost is still on the side of that line "
            f"where the strategy leads.")


def _corr_sentence(corr: pd.DataFrame) -> str:
    best: tuple[str, str, float] | None = None
    cols = list(corr.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            rho = float(corr.loc[a, b])
            if not np.isfinite(rho):
                continue
            if best is None or abs(rho) > abs(best[2]):
                best = (str(a), str(b), rho)
    if best is None:
        return "The score correlation could not be computed."
    a, b, rho = best
    if abs(rho) >= 0.9:
        return (f"{a} and {b} move together at {rho:.2f}. At that level they are "
                f"one strategy written twice, and counting them as two is double counting.")
    if rho <= -0.3:
        return (f"The strongest relationship is {a} versus {b} at {rho:.2f}. "
                f"They disagree, which is what a trend rule and a reversal rule "
                f"are supposed to do. Neither pair is high enough to collapse "
                f"the three into one.")
    return (f"The strongest relationship is {a} versus {b} at {rho:.2f}. "
            f"Related, not interchangeable. None of the pairs is close to 0.9, "
            f"which is the line at which this report would call them one strategy.")


def _most_held(book: BookResult) -> tuple[str | None, float]:
    if book.positions.empty or book.ledger.empty:
        return None, float("nan")
    n = int(book.ledger["decision_date"].nunique())
    if n == 0:
        return None, float("nan")
    counts = (book.positions.groupby("symbol")["decision_date"].nunique()
              .sort_values(ascending=False))
    return str(counts.index[0]), float(counts.iloc[0]) / n


def _display_summary(summary: pd.DataFrame, bench: str) -> pd.DataFrame:
    spy_cagr = float(_row(summary, bench)["cagr"])
    rows = []
    for _, r in summary.iterrows():
        rows.append({
            "book": r["name"],
            "CAGR": _pct(r["cagr"]),
            "vol": _pct(r["ann_volatility"]),
            "Sharpe": _num(r["sharpe"]),
            "max DD": _pct(r["max_drawdown"]),
            "hit rate": _pct(r["hit_rate"]),
            "hold (sessions)": _num(r["avg_holding_sessions"], 1),
            "turnover / yr": _turns(r["annual_turnover"]),
            f"CAGR vs {bench}": _signed_pp(float(r["cagr"]) - spy_cagr),
            "alpha / yr": _pct(r["alpha_annual"]),
        })
    return pd.DataFrame(rows)


def _display_costs(cost_grid: pd.DataFrame, bench: str) -> pd.DataFrame:
    """One row per strategy, one column per cost, plus the benchmark once."""
    costs = list(dict.fromkeys(cost_grid["cost_bps"].tolist()))
    rows = []
    for name in strategies.NAMES:
        sub = cost_grid.loc[cost_grid["name"] == name].set_index("cost_bps")
        row: dict[str, str] = {"book": name}
        for bps in costs:
            row[f"{bps:g} bp"] = _pct(sub.loc[bps, "cagr"]) if bps in sub.index else "n/a"
        rows.append(row)
    bench_row: dict[str, str] = {"book": bench}
    # The benchmark CAGR is repeated on every strategy row; take the first.
    first = cost_grid.drop_duplicates("cost_bps").set_index("cost_bps")
    for bps in costs:
        bench_row[f"{bps:g} bp"] = _pct(first.loc[bps, "benchmark_cagr"])
    rows.append(bench_row)
    return pd.DataFrame(rows)


def _display_corr(corr: pd.DataFrame) -> pd.DataFrame:
    out = corr.copy()
    out.index.name = "score"
    return out.map(lambda v: f"{v:.2f}" if np.isfinite(v) else "n/a")


def _table(frame: pd.DataFrame) -> str:
    # disable_numparse keeps "1.00" and "18.6%" as written. Tabulate
    # otherwise decides that 1.00 is an integer and reprints it as 1.
    kwargs = {"disable_numparse": True}
    try:
        return frame.to_markdown(index=frame.index.name is not None, **kwargs)
    except (ImportError, TypeError):
        return frame.to_string()
