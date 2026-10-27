"""
Feature layer.

Pure functions from a wide price matrix (dates x symbols) to a feature
matrix of the same shape. No I/O, no config object. Strategies consume
these and nothing else, which is what keeps a signal testable without a
download and what later milestones will extend without rewriting the
simulator.

Every value at date t is a function of rows with date <= t. There is no
`center=True`, no full-sample mean, and no unshifted rolling window on a
future target. `tests/test_strategies.py` pins that with a truncation
test rather than with a review comment.

Price basis, said once: this module does not choose one. The caller
passes the series the feature is allowed to see. Milestone 2 has two
series on disk, `close` (as-traded) and `adj_close` (splits and
dividends). It does not yet have a split-adjusted-but-not-dividend-adjusted
series, which is the one a price pattern should use. That is a phase-2
gap, and the backtest report says so where it matters.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .analytics import simple_returns


def trailing_return(prices: pd.DataFrame, window: int, skip: int = 0) -> pd.DataFrame:
    """Total return over `window` sessions, ending `skip` sessions ago.

    At date t the endpoints are t-skip and t-skip-window, both of which
    are on or before t. `skip=1` is the usual momentum convention: the
    most recent session is left out because short-horizon reversal
    contaminates it. `skip=0` includes today's close.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    if skip < 0:
        raise ValueError(f"skip must be >= 0, got {skip}")
    ended = prices.shift(skip)
    started = prices.shift(skip + window)
    return ended / started - 1.0


def trailing_volatility(prices: pd.DataFrame, window: int) -> pd.DataFrame:
    """Sample standard deviation of daily simple returns, ending at t.

    `min_periods=window`, so a short history is NaN rather than a
    volatility estimated from two observations. Returns (not prices) are
    the input to the standard deviation: a standard deviation of the
    price level is not a risk measure.
    """
    if window < 2:
        raise ValueError(f"volatility window must be >= 2, got {window}")
    rets = simple_returns(prices)
    vol = rets.rolling(window=window, min_periods=window).std(ddof=1)
    # Reindex onto the price calendar. The first price date has no
    # return, so it stays NaN, which is the truthful answer.
    return vol.reindex(prices.index)


def distance_to_high(prices: pd.DataFrame, window: int) -> pd.DataFrame:
    """Close divided by the trailing high, minus one.

    The window includes t, so a name printing a new high scores 0 and a
    name below its own high scores negative. The high is a rolling max
    of past and present prices only.
    """
    if window < 2:
        raise ValueError(f"window must be >= 2, got {window}")
    high = prices.rolling(window=window, min_periods=window).max()
    return prices / high - 1.0


def adjusted_open(raw_open: pd.DataFrame, close: pd.DataFrame,
                  adj_close: pd.DataFrame) -> pd.DataFrame:
    """As-traded open, scaled by the same day's close adjustment factor.

    Vendors in this pipeline adjust the close and leave the open raw.
    Multiplying the open by `adj_close / close` applies that day's split
    and dividend factor to the open, which is the standard reconstruction
    when a separate adjusted-open series does not exist. A 2-for-1 split
    that halves the raw open then leaves the adjusted open unchanged, so
    the simulator does not book a -50% return for an event that did not
    lose anyone money.

    The factor is that session's own close factor. It is not a guess at
    the intraday path. Non-positive prices become NaN: there is no
    defensible open to trade.
    """
    factor = adj_close / close.where(close > 0)
    out = raw_open.where(raw_open > 0) * factor
    # Inf from a zero close, and NaN from a missing bar, both mean "no trade".
    return out.where(np.isfinite(out))


def finite_or_nan(values: pd.DataFrame) -> pd.DataFrame:
    """Replace infinities with NaN. A division by a flat series is not a signal."""
    return values.where(np.isfinite(values))
