"""
Chronological validation and portfolio-cap tests for Milestone 3.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from marketengine import strategies
from marketengine.backtest import prepare_books, simulate_prepared
from marketengine.config import load_config
from marketengine.validate import (
    assert_split_is_clean,
    chronological_split,
    overfitting_table,
)


def days(n: int, start: str = "2018-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def synthetic_panel(n_days: int = 120, n_names: int = 8, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = days(n_days)
    rows = []
    for i, sym in enumerate([f"S{k:02d}" for k in range(n_names)] + ["SPY"]):
        drift = 0.0005 if sym == "SPY" else 0.001 - 0.0002 * i
        rets = drift + rng.normal(0, 0.012, n_days)
        px = 50.0 + i + np.cumprod(1.0 + rets)
        # Work backwards so level starts near 50+i
        level = 100.0 * np.cumprod(1.0 + rets)
        for dt, p, v in zip(idx, level, rng.integers(1_000_000, 5_000_000, n_days)):
            rows.append({
                "date": dt,
                "symbol": sym,
                "open": p * 0.999,
                "high": p * 1.01,
                "low": p * 0.99,
                "close": p,
                "adj_close": p,
                "volume": float(v),
                "filled": False,
                "flags": "",
            })
    return pd.DataFrame(rows)


VALIDATION_CFG = """
run_id: tval
provider: yfinance
universe:
  tickers: [S00, S01, S02, S03, S04, S05, S06, S07]
  benchmark: SPY
window:
  start: "2018-01-01"
  end: "2018-12-31"
calendar:
  align: benchmark
  max_ffill_days: 0
analytics:
  rolling_vol_windows: [21]
  corr_window: 21
  risk_free_annual: 0.02
  trading_days_per_year: 252
  min_history_days: 20
  extreme_return_threshold: 0.5
  extreme_return_vol_units: 8
  quality_vol_window: 20
  stale_price_sessions: 3
storage:
  root: data
  duckdb_file: t.duckdb
outputs:
  figures: false
  dpi: 100
strategies:
  momentum: {window: 10, skip: 1}
  reversal: {return_window: 3, vol_window: 10}
  breakout: {window: 10}
  portfolio:
    n_long: 3
    commission_bps: 1
    slippage_bps: 4
    cost_bps: 5
    cost_grid_bps: [0, 5, 10, 25]
    max_weight: 0.40
    max_participation: 0.50
    adv_window: 5
    assumed_nav_usd: 1000000
validation:
  train_fraction: 0.80
  overfitting_sharpe_drop: 0.40
"""


def test_chronological_split_never_puts_test_dates_in_train():
    index = days(100)
    split = chronological_split(index, 0.80)
    assert_split_is_clean(split)
    assert split.train_index.max() < split.boundary
    assert split.test_index.min() == split.boundary
    assert set(split.train_index).isdisjoint(split.test_index)
    assert len(split.train_index) + len(split.test_index) == len(index)


def test_shuffled_boundary_is_rejected_by_the_clean_check():
    index = days(50)
    split = chronological_split(index, 0.80)
    # Poison the train window with a test date.
    poisoned = split.train_index.append(pd.DatetimeIndex([split.boundary]))
    split.train_index = poisoned  # type: ignore[misc]
    with pytest.raises(ValueError, match="not before the test boundary|share"):
        assert_split_is_clean(split)


def test_overfit_flag_fires_when_test_sharpe_collapses():
    train = pd.DataFrame([
        {"name": "momentum", "sharpe": 1.0, "cagr": 0.10},
        {"name": "reversal", "sharpe": 0.8, "cagr": 0.08},
        {"name": "breakout", "sharpe": 0.5, "cagr": 0.05},
    ])
    test = pd.DataFrame([
        {"name": "momentum", "sharpe": 0.50, "cagr": 0.04},  # 50% of train -> flag at 40% drop
        {"name": "reversal", "sharpe": 0.75, "cagr": 0.07},  # fine
        {"name": "breakout", "sharpe": 0.40, "cagr": 0.03},  # fine (20% drop)
    ])
    flags = overfitting_table(train, test, drop=0.40)
    by_name = flags.set_index("name")["overfit_flag"].to_dict()
    assert by_name["momentum"] is True
    assert by_name["reversal"] is False
    assert by_name["breakout"] is False


def test_max_weight_clips_and_does_not_redistribute():
    idx = days(1)
    scores = pd.DataFrame({"AAA": [1.0], "BBB": [0.9], "CCC": [0.1]}, index=idx)
    weights = strategies.target_weights(scores, n_long=2)
    # 0.5 each. Cap at 0.3 -> 0.3 each, cash 0.4. Not renormalised to 1.
    dollar = pd.DataFrame({"AAA": [1e9], "BBB": [1e9], "CCC": [1e9]}, index=idx)
    capped = strategies.apply_portfolio_caps(
        weights, dollar, max_weight=0.3, max_participation=None,
        adv_window=5, assumed_nav_usd=1e6,
    )
    assert capped.loc[idx[0], "AAA"] == pytest.approx(0.3)
    assert capped.loc[idx[0], "BBB"] == pytest.approx(0.3)
    assert capped.sum(axis=1).iloc[0] == pytest.approx(0.6)


def test_participation_cap_uses_trailing_adv_only():
    idx = days(6)
    weights = pd.DataFrame({"AAA": [0.5] * 6}, index=idx)
    # Tiny dollar volume -> participation 2% of ADV at $1m NAV is a tiny weight.
    dollar = pd.DataFrame({"AAA": [100_000.0] * 6}, index=idx)
    capped = strategies.apply_portfolio_caps(
        weights, dollar, max_weight=None, max_participation=0.02,
        adv_window=5, assumed_nav_usd=1_000_000,
    )
    # max weight = 0.02 * 100000 / 1e6 = 0.002
    assert capped.iloc[-1, 0] == pytest.approx(0.002)
    # Before a full ADV window the name is zeroed, not guessed.
    assert capped.iloc[0, 0] == pytest.approx(0.0)


def test_commission_plus_slippage_must_equal_cost(tmp_path):
    # Keep cost_bps on the grid; break the commission + slippage identity.
    bad = VALIDATION_CFG.replace("slippage_bps: 4", "slippage_bps: 3", 1)
    path = tmp_path / "c.yml"
    path.write_text(bad)
    with pytest.raises(ValueError, match="commission_bps \\+ slippage_bps"):
        load_config(path)


def test_prepare_and_validate_split_on_synthetic_panel(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(VALIDATION_CFG)
    cfg = load_config(path)
    panel = synthetic_panel()
    prepared = prepare_books(cfg, panel)
    split = chronological_split(prepared.weights["momentum"].index, cfg.validation.train_fraction)
    assert_split_is_clean(split)
    train_books = simulate_prepared(prepared, cfg, decision_index=split.train_index)
    test_books = simulate_prepared(prepared, cfg, decision_index=split.test_index)
    # No test mark may use a train decision date.
    for book in test_books.values():
        if book.ledger.empty:
            continue
        assert book.ledger["decision_date"].min() >= split.boundary
    for book in train_books.values():
        if book.ledger.empty:
            continue
        assert book.ledger["decision_date"].max() < split.boundary
    # Metrics exist for every strategy on both windows.
    assert all(len(train_books[n].returns) > 0 for n in strategies.NAMES)
    assert all(len(test_books[n].returns) > 0 for n in strategies.NAMES)
