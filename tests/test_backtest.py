"""
Strategy and backtest tests.

Every case is a number worked out by hand, or a property that has to
hold no matter what the prices are (no lookahead, a split is not a
loss, the fill is the next open). Comparing a function to a snapshot
of its own output is not one of the cases.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from marketengine import features, strategies
from marketengine.backtest import (
    average_holding_sessions,
    breakeven_cost_bps,
    simulate,
)
from marketengine.config import load_config


def days(n: int, start: str = "2020-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def panel(n: int = 40, seed: int = 0) -> pd.DataFrame:
    """Three names, deterministic, with enough history for a 20-day window."""
    rng = np.random.default_rng(seed)
    idx = days(n)
    cols = {}
    for sym, drift in (("AAA", 0.002), ("BBB", 0.0), ("CCC", -0.001)):
        rets = drift + rng.normal(0, 0.01, n)
        cols[sym] = 100.0 * np.cumprod(1.0 + rets)
    return pd.DataFrame(cols, index=idx)


# =====================================================================
# features
# =====================================================================

def test_trailing_return_with_skip_does_not_use_todays_price():
    # window=2, skip=1. At the last date the endpoints are t-1 and t-3.
    # Today's price is 100 for every name and must not appear in the result.
    idx = days(5)
    prices = pd.DataFrame({
        "AAA": [1.0, 1.0, 1.0, 2.0, 100.0],
        "BBB": [1.0, 1.0, 1.0, 1.0, 100.0],
        "CCC": [1.0, 4.0, 4.0, 2.0, 100.0],
    }, index=idx)
    got = features.trailing_return(prices, window=2, skip=1).iloc[-1]
    assert got["AAA"] == pytest.approx(2.0 / 1.0 - 1.0)
    assert got["BBB"] == pytest.approx(0.0)
    assert got["CCC"] == pytest.approx(2.0 / 4.0 - 1.0)


def test_distance_to_high_is_zero_on_a_new_high_and_negative_below_it():
    idx = days(4)
    prices = pd.DataFrame({
        "AAA": [1.0, 1.0, 1.0, 5.0],
        "BBB": [5.0, 5.0, 5.0, 4.0],
    }, index=idx)
    got = features.distance_to_high(prices, window=3).iloc[-1]
    assert got["AAA"] == pytest.approx(0.0)
    assert got["BBB"] == pytest.approx(4.0 / 5.0 - 1.0)


def test_adjusted_open_is_unchanged_by_a_split():
    # 2-for-1 overnight. The raw open halves. The economic open does not.
    idx = days(2)
    raw = pd.DataFrame({"AAA": [100.0, 50.0]}, index=idx)
    close = pd.DataFrame({"AAA": [100.0, 50.0]}, index=idx)
    adj = pd.DataFrame({"AAA": [100.0, 100.0]}, index=idx)
    got = features.adjusted_open(raw, close, adj)
    assert got["AAA"].iloc[0] == pytest.approx(100.0)
    assert got["AAA"].iloc[1] == pytest.approx(100.0)


def test_adjusted_open_puts_a_dividend_back():
    # $2 dividend, price prints 98, total-return close stays 100.
    idx = days(2)
    raw = pd.DataFrame({"AAA": [100.0, 98.0]}, index=idx)
    close = pd.DataFrame({"AAA": [100.0, 98.0]}, index=idx)
    adj = pd.DataFrame({"AAA": [100.0, 100.0]}, index=idx)
    got = features.adjusted_open(raw, close, adj)
    assert got["AAA"].iloc[1] == pytest.approx(100.0)


def test_truncating_the_future_does_not_change_past_features():
    full = panel()
    cut = full.iloc[:-7]
    for fn in (
        lambda p: features.trailing_return(p, 20, 1),
        lambda p: features.trailing_volatility(p, 20),
        lambda p: features.distance_to_high(p, 20),
    ):
        full_v = fn(full)
        cut_v = fn(cut)
        pd.testing.assert_frame_equal(full_v.loc[cut.index], cut_v)


# =====================================================================
# strategies
# =====================================================================

def test_momentum_ranks_the_strongest_skipped_return_first():
    idx = days(5)
    prices = pd.DataFrame({
        "AAA": [1.0, 1.0, 1.0, 2.0, 100.0],   # +100% over the skipped window
        "BBB": [1.0, 1.0, 1.0, 1.0, 100.0],   # flat
        "CCC": [1.0, 4.0, 4.0, 2.0, 100.0],   # -50%
    }, index=idx)
    scores = strategies.momentum_scores(prices, window=2, skip=1)
    last = scores.iloc[-1]
    assert last["AAA"] == pytest.approx(1.0)
    assert last["BBB"] == pytest.approx(2.0 / 3.0)
    assert last["CCC"] == pytest.approx(1.0 / 3.0)
    # And the 100s, which are today, did not decide it. Replacing them
    # with anything positive leaves the score where it was.
    bumped = prices.copy()
    bumped.iloc[-1] = [1.0, 7.0, 3.0]
    again = strategies.momentum_scores(bumped, window=2, skip=1)
    pd.testing.assert_series_equal(scores.iloc[-1], again.iloc[-1])


def test_reversal_ranks_the_sharpest_vol_adjusted_drop_first():
    # Identical paths, then a one-session move on the last day. With the
    # same history the volatility is the same, so the biggest drop wins.
    # return_window=1 compares today to yesterday, which is the whole move.
    idx = days(8)
    base = [10.0, 10.2, 9.9, 10.1, 10.0, 10.3, 10.0]
    prices = pd.DataFrame({
        "AAA": base + [8.0],    # down hard
        "BBB": base + [10.0],   # flat on the day
        "CCC": base + [12.0],   # up
    }, index=idx)
    scores = strategies.reversal_scores(prices, return_window=1, vol_window=5)
    last = scores.iloc[-1]
    assert last["AAA"] == pytest.approx(1.0)
    assert last["CCC"] == pytest.approx(1.0 / 3.0)
    assert last["AAA"] > last["BBB"] > last["CCC"]


def test_breakout_ranks_a_new_high_above_a_name_below_its_high():
    idx = days(4)
    prices = pd.DataFrame({
        "AAA": [1.0, 1.0, 1.0, 5.0],
        "BBB": [5.0, 5.0, 5.0, 4.0],
    }, index=idx)
    scores = strategies.breakout_scores(prices, window=3)
    last = scores.iloc[-1].dropna()
    assert last["AAA"] > last["BBB"]


def test_scores_do_not_change_when_the_future_is_rewritten():
    full = panel()
    for name, frame in strategies.score_all(
        full, momentum_window=10, momentum_skip=1,
        reversal_return_window=5, reversal_vol_window=10, breakout_window=10,
    ).items():
        mutated = full.copy()
        mutated.iloc[-1] *= 4.0
        again = strategies.score_all(
            mutated, momentum_window=10, momentum_skip=1,
            reversal_return_window=5, reversal_vol_window=10, breakout_window=10,
        )[name]
        pd.testing.assert_frame_equal(frame.iloc[:-1], again.iloc[:-1])


def test_top_n_is_equal_weight_and_breaks_ties_alphabetically():
    idx = days(1)
    # Columns deliberately unsorted, and AAA ties BBB.
    scores = pd.DataFrame({"BBB": [0.5], "AAA": [0.5], "CCC": [1.0]}, index=idx)
    top2 = strategies.target_weights(scores, n_long=2)
    assert top2.loc[idx[0], "CCC"] == pytest.approx(0.5)
    assert top2.loc[idx[0], "AAA"] == pytest.approx(0.5)
    assert top2.loc[idx[0], "BBB"] == pytest.approx(0.0)

    top1 = strategies.target_weights(scores, n_long=1)
    assert top1.loc[idx[0], "CCC"] == pytest.approx(1.0)
    assert top1.loc[idx[0], "AAA"] == pytest.approx(0.0)


def test_a_single_eligible_name_does_not_get_the_whole_book():
    # Missing history stays cash. It does not become a reason to put
    # 100% in the one name that happened to have a score.
    idx = days(1)
    scores = pd.DataFrame({"AAA": [0.8], "BBB": [np.nan]}, index=idx)
    weights = strategies.target_weights(scores, n_long=2)
    assert weights.loc[idx[0], "AAA"] == pytest.approx(0.5)
    assert weights.loc[idx[0], "BBB"] == pytest.approx(0.0)
    assert weights.sum(axis=1).iloc[0] == pytest.approx(0.5)


def test_flat_volatility_is_not_a_reversal_signal():
    # Constant prices have a zero trailing volatility. The ratio is
    # undefined, and undefined must not rank as the best name.
    idx = days(6)
    prices = pd.DataFrame({
        "AAA": [10.0] * 6,
        "BBB": [10.0, 10.0, 10.0, 10.0, 10.0, 9.0],
    }, index=idx)
    scores = strategies.reversal_scores(prices, return_window=1, vol_window=3)
    assert scores["AAA"].isna().all()
    assert scores["BBB"].iloc[-1] == pytest.approx(1.0)


# =====================================================================
# simulator
# =====================================================================

def _book(raw_open: pd.DataFrame, close: pd.DataFrame, adj: pd.DataFrame,
          weights: pd.DataFrame, cost_bps: float = 0.0):
    adj_open = features.adjusted_open(raw_open, close, adj)
    return simulate(weights, raw_open, adj_open, cost_bps, name="t")


def test_the_fill_is_the_next_open_and_not_the_signal_close():
    # Opens go 100, 100, 100, 150. The +50% is between D2 and D3.
    # The decision that earns it is D1 (entered at the D2 open of 100),
    # not D2. D2's close is irrelevant and is set to a spike to prove it.
    idx = days(4)
    raw = pd.DataFrame({"AAA": [100.0, 100.0, 100.0, 150.0]}, index=idx)
    close = pd.DataFrame({"AAA": [100.0, 100.0, 999.0, 150.0]}, index=idx)
    weights = pd.DataFrame({"AAA": [1.0, 1.0, 1.0, 1.0]}, index=idx)
    book = _book(raw, close, close, weights)

    assert list(book.ledger.index) == [idx[2], idx[3]]
    assert book.ledger.iloc[0]["decision_date"] == idx[0]
    assert book.ledger.iloc[0]["return"] == pytest.approx(0.0)
    assert book.ledger.iloc[1]["decision_date"] == idx[1]
    assert book.ledger.iloc[1]["entry_date"] == idx[2]
    assert book.ledger.iloc[1]["return"] == pytest.approx(0.50)
    # The spike in D2's close is not a fill and not a mark.
    assert 999.0 not in set(book.trades["raw_open"])


def test_a_split_is_not_booked_as_a_loss_and_the_fill_is_the_raw_open():
    idx = days(3)
    raw = pd.DataFrame({"AAA": [100.0, 100.0, 50.0]}, index=idx)
    close = pd.DataFrame({"AAA": [100.0, 100.0, 50.0]}, index=idx)
    adj = pd.DataFrame({"AAA": [100.0, 100.0, 100.0]}, index=idx)
    weights = pd.DataFrame({"AAA": [1.0, 1.0, 1.0]}, index=idx)
    book = _book(raw, close, adj, weights)

    assert book.returns.iloc[0] == pytest.approx(0.0)
    # Entered at D1's raw open, which is still 100, not the post-split 50.
    assert book.trades.iloc[0]["raw_open"] == pytest.approx(100.0)
    assert book.positions.iloc[0]["raw_open"] == pytest.approx(100.0)


def test_cost_is_charged_once_on_the_way_in_and_not_again_for_holding():
    idx = days(4)
    raw = pd.DataFrame({"AAA": [100.0, 100.0, 100.0, 100.0]}, index=idx)
    weights = pd.DataFrame({"AAA": [1.0, 1.0, 1.0, 1.0]}, index=idx)
    book = _book(raw, raw, raw, weights, cost_bps=100.0)  # 1% one-way of traded notional

    # From cash to fully invested: sum |Δw| = 1, cost = 1%, price unchanged.
    assert book.ledger.iloc[0]["return"] == pytest.approx(-0.01)
    assert book.ledger.iloc[1]["return"] == pytest.approx(0.0)
    assert book.ledger.iloc[-1]["nav"] == pytest.approx(0.99)
    assert book.ledger.iloc[0]["one_way_turnover"] == pytest.approx(0.5)
    assert book.ledger.iloc[1]["one_way_turnover"] == pytest.approx(0.0)


def test_a_missing_open_stays_cash_instead_of_earning_a_made_up_return():
    idx = days(4)
    # Decision D0 enters at D1 and marks at D2. AAA has no D2 open, so it
    # is not filled. BBB's open goes 100 -> 110 over that hold.
    raw = pd.DataFrame({
        "AAA": [100.0, 100.0, np.nan, 100.0],
        "BBB": [100.0, 100.0, 110.0, 110.0],
    }, index=idx)
    weights = pd.DataFrame({"AAA": [0.5, 0.5, 0.5, 0.5],
                            "BBB": [0.5, 0.5, 0.5, 0.5]}, index=idx)
    book = _book(raw, raw, raw, weights)
    first = book.positions.loc[book.positions["decision_date"] == idx[0]]
    assert list(first["symbol"]) == ["BBB"]
    assert book.ledger.iloc[0]["unfilled_weight"] == pytest.approx(0.5)
    # BBB goes 100 -> 110 from D1 to D2, at weight 0.5. AAA contributes nothing.
    assert book.ledger.iloc[0]["gross_return"] == pytest.approx(0.5 * 0.10)


def test_holding_period_is_the_length_of_consecutive_spells():
    idx = days(7)
    raw = pd.DataFrame({"AAA": [100.0] * 7, "BBB": [100.0] * 7}, index=idx)
    # AAA held for the first three decisions, then out, then one more.
    # BBB held the whole way. Calendar has room for every decision to complete.
    weights = pd.DataFrame({
        "AAA": [1.0, 1.0, 1.0, 0.0, 1.0],
        "BBB": [1.0, 1.0, 1.0, 1.0, 1.0],
    }, index=idx[:5])
    # Weights sum to 2, which this test is not about. The spell counter
    # only looks at weight > 0.
    book = _book(raw, raw, raw, weights)
    aaa = book.positions.loc[book.positions["symbol"] == "AAA", ["symbol", "decision_idx"]]
    # Two spells, lengths 3 and 1. Mean 2.
    assert average_holding_sessions(aaa) == pytest.approx(2.0)


def test_breakeven_interpolates_and_reports_the_two_edges():
    assert breakeven_cost_bps([0, 10], [0.02, -0.02]) == pytest.approx(5.0)
    assert math.isnan(breakeven_cost_bps([0, 10], [-0.01, -0.02]))
    assert breakeven_cost_bps([0, 10, 25], [0.02, 0.01, 0.005]) == float("inf")


# =====================================================================
# config
# =====================================================================

STRATEGIES = """
strategies:
  momentum: {window: 20, skip: 1}
  reversal: {return_window: 5, vol_window: 20}
  breakout: {window: 20}
  portfolio: {n_long: 3, cost_bps: 5, cost_grid_bps: [0, 5, 10, 25]}
"""

BASE = """
run_id: t
provider: yfinance
universe:
  tickers: [AAA, BBB]
  benchmark: SPY
window:
  start: "2020-01-01"
  end: "2020-12-31"
"""


def test_strategy_block_is_optional(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(BASE)
    assert load_config(path).strategies is None


def test_strategy_block_is_read(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(BASE + STRATEGIES)
    cfg = load_config(path)
    assert cfg.strategies is not None
    assert cfg.strategies.momentum.window == 20
    assert cfg.strategies.momentum.skip == 1
    assert cfg.strategies.portfolio.n_long == 3
    assert cfg.strategies.portfolio.cost_bps == pytest.approx(5.0)
    assert cfg.strategies.portfolio.cost_grid_bps == (0.0, 5.0, 10.0, 25.0)


def test_a_window_of_one_is_rejected(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(BASE + STRATEGIES.replace("window: 20, skip:", "window: 1, skip:", 1))
    with pytest.raises(ValueError, match="momentum.window"):
        load_config(path)


def test_headline_cost_must_be_on_the_grid(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(BASE + STRATEGIES.replace("cost_bps: 5", "cost_bps: 7"))
    with pytest.raises(ValueError, match="cost_grid_bps"):
        load_config(path)
