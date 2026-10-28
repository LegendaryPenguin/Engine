"""Report for the train-only configuration search."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .search import TrialSpec


def write_report(cfg: Config, *, trials: pd.DataFrame, winner: TrialSpec,
                 winner_train: dict[str, object], winner_test: dict[str, object],
                 train_ew: dict[str, object], test_ew: dict[str, object],
                 boundary: pd.Timestamp, n_train: int, n_test: int) -> Path:
    lines: list[str] = []

    def w(text: str = "") -> None:
        lines.append(text)

    ok = trials.loc[trials["status"] == "ok"]
    beats_ew_test = bool(np.isfinite(winner_test["cagr"])
                         and float(winner_test["cagr"]) > float(test_ew["cagr"]))

    w(f"# Train-only search — {cfg.run_id}")
    w()
    w(f"**{len(trials)} configurations** evaluated on the training window only. "
      f"The chronological boundary (**{boundary.date()}**) was fixed before "
      f"any trial. The winner is the best **train Sharpe**. The test window "
      f"was used **once**, for that winner alone.")
    w()
    w(f"**Winner:** `{winner.label}` ({winner.family}, top {winner.n_long}, "
      f"params {winner.params}).")
    w()
    w(f"- Train: CAGR {_pct(winner_train['train_cagr'])}, Sharpe "
      f"{_num(winner_train['train_sharpe'])}, "
      f"{'beats' if winner_train['train_beats_ew'] else 'loses to'} equal-weight "
      f"({_pct(train_ew['cagr'])}).")
    w(f"- Sealed test: CAGR {_pct(winner_test['cagr'])}, Sharpe "
      f"{_num(winner_test['sharpe'])}, max DD {_pct(winner_test['max_drawdown'])}. "
      f"Equal-weight test CAGR {_pct(test_ew['cagr'])} / Sharpe {_num(test_ew['sharpe'])}. "
      f"**{'Beats' if beats_ew_test else 'Does not beat'} equal-weight on the "
      f"untouched test window.**")
    w()
    if not beats_ew_test:
        w("Searching more configurations against this same test window would "
          "invalidate the result (Bailey et al., probability of backtest "
          "overfitting). The trial log is saved so a later CSCV/PBO estimate "
          "can use it without re-peeking.")
        w()

    w("## Method")
    w()
    w("- Split: chronological, same rule as `validate` "
      f"({cfg.validation.train_fraction:.0%} train). "
      f"Train {n_train} decisions, test {n_test}.")
    w("- Grid: literature-shaped momentum / reversal / breakout settings × "
      "`n_long ∈ {5,10,15}`. Costs and caps stay at the config values "
      f"({cfg.strategies.portfolio.cost_bps:g} bp one-way).")  # type: ignore[union-attr]
    w("- Selection: maximum train Sharpe; ties broken by train CAGR.")
    w("- Test metrics exist only for the selected trial. Other trials have "
      "train metrics only.")
    w(f"- Config SHA-256: `{cfg.source_sha256}`.")
    w()

    w("## Top train Sharpes")
    w()
    top = ok.sort_values("train_sharpe", ascending=False).head(10)
    w(_table(_display_top(top)))
    w()

    w("## How many beat equal-weight on train?")
    w()
    n_beat = int(ok["train_beats_ew"].sum())
    w(f"{n_beat} of {len(ok)} successful trials beat equal-weight CAGR on the "
      f"**training** window. That is an in-sample count; it is not evidence "
      f"about the test window.")
    w()

    w("## Reference")
    w()
    w("D. H. Bailey, J. Borwein, M. López de Prado, and Q. J. Zhu, "
      "\"The probability of backtest overfitting,\" *Journal of Computational "
      "Finance*, 2017. Ordinary holdout after repeated selection contaminates "
      "the test set; this run selects only on train and evaluates test once.")
    w()

    path = cfg.report_dir / "search.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _pct(v: object) -> str:
    x = float(v)  # type: ignore[arg-type]
    return "n/a" if not np.isfinite(x) else f"{x:.1%}"


def _num(v: object) -> str:
    x = float(v)  # type: ignore[arg-type]
    return "n/a" if not np.isfinite(x) else f"{x:.2f}"


def _display_top(top: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in top.iterrows():
        rows.append({
            "label": r["label"],
            "family": r["family"],
            "train Sharpe": _num(r["train_sharpe"]),
            "train CAGR": _pct(r["train_cagr"]),
            "vs EW (train)": "yes" if r["train_beats_ew"] else "no",
            "selected": "←" if r.get("selected") else "",
        })
    return pd.DataFrame(rows)


def _table(frame: pd.DataFrame) -> str:
    try:
        return frame.to_markdown(index=False, disable_numparse=True)
    except (ImportError, TypeError):
        return frame.to_string(index=False)
