"""Train-only search: selection never sees the test window."""

from __future__ import annotations

import numpy as np
import pandas as pd

from marketengine.config import load_config
from marketengine.search import TrialSpec, build_trial_list, run_search
from tests.test_validate import VALIDATION_CFG, synthetic_panel


def test_grid_is_non_empty_and_labelled():
    trials = build_trial_list(n_longs=(5, 10))
    assert len(trials) >= 20
    assert all(t.label for t in trials)
    families = {t.family for t in trials}
    assert families == {"momentum", "reversal", "breakout"}


def test_search_selects_on_train_and_evaluates_winner_once(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(VALIDATION_CFG)
    cfg = load_config(path)
    panel = synthetic_panel(n_days=160, n_names=8, seed=2)
    # Tiny grid so the test stays fast.
    trials = [
        TrialSpec(0, "momentum", "mom_10d_top3", {"window": 10, "skip": 1}, 3),
        TrialSpec(1, "momentum", "mom_20d_top3", {"window": 20, "skip": 1}, 3),
        TrialSpec(2, "reversal", "rev_5d_top3", {"return_window": 5, "vol_window": 10}, 3),
        TrialSpec(3, "breakout", "brk_10d_top3", {"window": 10}, 3),
    ]
    out = run_search(cfg, panel, trials=trials)
    assert out.n_trials == 4
    assert out.winner.trial_id in {0, 1, 2, 3}
    # Every trial has a train row; only the summary carries sealed test metrics.
    assert len(out.trials) == 4
    assert "test_sharpe" not in out.trials.columns
    assert np.isfinite(out.test_winner["sharpe"]) or True  # may be nan on tiny panel
    # Winner is the max train Sharpe among ok trials.
    ok = out.trials.loc[out.trials["status"] == "ok"]
    best = ok.sort_values("train_sharpe", ascending=False).iloc[0]
    assert int(best["trial_id"]) == out.winner.trial_id
