"""
Analytics.

Every function here is pure: a DataFrame or Series in, a DataFrame or
Series out, no config object, no file access, no printing. That is what
makes `tests/test_analytics.py` able to check them against closed-form
answers, and it is what will let Milestones 3-5 reuse them unchanged.

Conventions, once, for the whole file:

  - A *returns frame* is dates x symbols of SIMPLE daily returns, already
    aligned. The first row of a price series has no return and is absent,
    not zero.
  - Annualisation uses `periods` (252 by default) and is geometric for
    returns, `sqrt(periods)` for volatility.
  - Pairwise statistics drop dates where either side is missing, so a
    late-listing symbol is compared against the benchmark only over the
    window they share. The number of observations actually used is
    reported alongside every such statistic.
  - `rf` is a DAILY risk-free rate (see `AnalyticsConfig.risk_free_daily`),
    because mixing an annual rate into a daily series is the single
    easiest way to get a Sharpe ratio wrong by a factor of 16.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_PERIODS = 252

#: Below this, a daily standard deviation is treated as "no variation at
#: all" and any ratio that divides by it returns NaN. A constant series
#: does not produce a sample standard deviation of exactly 0 in floating
#: point, so an `== 0` guard lets through Sharpe ratios of 1e16, which
#: then dominate every sort in the summary table. 1e-12 daily is 1.6e-11
#: annualised: far below anything a real price series can express.
FLAT_TOLERANCE = 1e-12


# =====================================================================
# returns
# =====================================================================

def simple_returns(prices: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Daily arithmetic returns, first row dropped.

    Simple (not log) returns are the primary series because they
    aggregate correctly across a portfolio: the return of a basket is the
    weighted mean of simple returns, which is not true of log returns.
    """
    out = prices.pct_change()
    return out.iloc[1:]


def log_returns(prices: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Daily log returns. Additive through time, so they are the right
    thing for volatility scaling and for the normality diagnostics that
    show up in Milestone 3."""
    out = np.log(prices / prices.shift(1))
    return out.iloc[1:]


def cumulative_returns(returns: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Compounded return-to-date, starting at 0 on the first day.

    NaNs are treated as 0% for the compounding only — a missing day must
    not blank the entire remainder of the curve, which is what a plain
    `cumprod` over NaN does.
    """
    return (1.0 + returns.fillna(0.0)).cumprod() - 1.0


def growth_of_one(returns: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """What $1 becomes. The same information as `cumulative_returns`,
    shifted by one — it is the form that plots legibly on a log axis."""
    return (1.0 + returns.fillna(0.0)).cumprod()


# =====================================================================
# single-series statistics
# =====================================================================

def annualised_return(returns: pd.Series, periods: int = DEFAULT_PERIODS) -> float:
    """Geometric mean return, annualised (CAGR of the return series).

    Geometric, not `mean * 252`: the arithmetic version overstates the
    realised outcome whenever the series is volatile, and by a lot over
    ten years.
    """
    r = returns.dropna()
    if r.empty:
        return float("nan")
    total = float((1.0 + r).prod())
    if total <= 0:
        # A -100% path. CAGR is undefined rather than -100%, and saying so
        # is better than returning a number that invites comparison.
        return float("nan")
    return total ** (periods / len(r)) - 1.0


def annualised_volatility(returns: pd.Series, periods: int = DEFAULT_PERIODS) -> float:
    """Sample standard deviation of daily returns, scaled by sqrt(periods)."""
    r = returns.dropna()
    if len(r) < 2:
        return float("nan")
    return float(r.std(ddof=1) * np.sqrt(periods))


def sharpe_ratio(returns: pd.Series, rf: float = 0.0,
                 periods: int = DEFAULT_PERIODS) -> float:
    """Annualised Sharpe ratio on excess returns. `rf` is DAILY."""
    excess = (returns - rf).dropna()
    if len(excess) < 2:
        return float("nan")
    sd = excess.std(ddof=1)
    # Not `== 0`: the sample standard deviation of a genuinely constant
    # series comes out around 1e-19 rather than exactly zero, and dividing
    # by that produces a Sharpe of 7e16 instead of "undefined".
    if not np.isfinite(sd) or sd < FLAT_TOLERANCE:
        return float("nan")
    return float(excess.mean() / sd * np.sqrt(periods))


def sortino_ratio(returns: pd.Series, rf: float = 0.0,
                  periods: int = DEFAULT_PERIODS) -> float:
    """Sharpe with only downside deviation in the denominator.

    The downside deviation is taken over the whole sample with upside
    excess set to 0, not over the subset of negative days. Dividing by the
    standard deviation of only the losing days would make a series with
    few, small losses look arbitrarily good.
    """
    excess = (returns - rf).dropna()
    if len(excess) < 2:
        return float("nan")
    downside = np.minimum(excess, 0.0)
    dd = float(np.sqrt((downside ** 2).mean()))
    if not np.isfinite(dd) or dd < FLAT_TOLERANCE:
        return float("nan")
    return float(excess.mean() / dd * np.sqrt(periods))


def drawdown_series(returns: pd.Series) -> pd.Series:
    """Fractional drawdown from the running peak, <= 0 everywhere."""
    curve = growth_of_one(returns)
    return curve / curve.cummax() - 1.0


def max_drawdown(returns: pd.Series) -> dict[str, object]:
    """Worst peak-to-trough decline, with the dates that produced it.

    The peak date is found by looking BACKWARD from the trough for the
    running maximum, not forward from the start; the deepest drawdown's
    peak is not necessarily the highest point of the whole series.
    """
    dd = drawdown_series(returns)
    if dd.empty:
        return {"max_drawdown": float("nan"), "peak_date": None,
                "trough_date": None, "recovery_date": None}

    trough = dd.idxmin()
    curve = growth_of_one(returns)
    peak = curve.loc[:trough].idxmax()
    after = curve.loc[trough:]
    recovered = after[after >= curve.loc[peak]]
    return {
        "max_drawdown": float(dd.min()),
        "peak_date": peak,
        "trough_date": trough,
        # None means it had not recovered by the end of the sample, which
        # is a real and important answer, not missing data.
        "recovery_date": (recovered.index[0] if len(recovered) else None),
    }


def historical_var(returns: pd.Series, level: float = 0.95) -> float:
    """Historical one-day Value at Risk, returned as a negative number.

    Empirical quantile, no distribution assumed — daily equity returns are
    fat-tailed and a Gaussian VaR understates exactly the days that matter.
    """
    r = returns.dropna()
    if r.empty:
        return float("nan")
    return float(np.quantile(r, 1.0 - level))


def hit_rate(returns: pd.Series) -> float:
    """Share of days with a strictly positive return."""
    r = returns.dropna()
    if r.empty:
        return float("nan")
    return float((r > 0).mean())


def rolling_volatility(returns: pd.DataFrame | pd.Series, window: int,
                       periods: int = DEFAULT_PERIODS) -> pd.DataFrame | pd.Series:
    """Annualised rolling standard deviation.

    `min_periods=window`, so the series starts only once a full window
    exists. A partially-filled window produces a volatility estimate from
    three observations, which is noise wearing the label of a statistic.
    """
    return returns.rolling(window=window, min_periods=window).std(ddof=1) * np.sqrt(periods)


# =====================================================================
# cross-sectional
# =====================================================================

def correlation_matrix(returns: pd.DataFrame, min_periods: int = 60) -> pd.DataFrame:
    """Pearson correlation of daily returns, pairwise-complete.

    `min_periods` leaves a NaN rather than a number where two symbols
    barely overlap. A correlation computed from twelve shared days is not
    a weaker estimate of the truth; it is an unrelated number.
    """
    return returns.corr(method="pearson", min_periods=min_periods)


def rolling_correlation(returns: pd.DataFrame, benchmark: str,
                        window: int) -> pd.DataFrame:
    """Rolling correlation of every column against `benchmark`."""
    if benchmark not in returns.columns:
        raise KeyError(f"benchmark {benchmark!r} is not a column of the returns frame")
    bench = returns[benchmark]
    cols = [c for c in returns.columns if c != benchmark]
    out = {c: returns[c].rolling(window, min_periods=window).corr(bench) for c in cols}
    return pd.DataFrame(out, index=returns.index)


# =====================================================================
# benchmark-relative
# =====================================================================

def beta_alpha(asset: pd.Series, benchmark: pd.Series, rf: float = 0.0,
               periods: int = DEFAULT_PERIODS) -> dict[str, float]:
    """CAPM beta, annualised alpha, and R^2 over the shared window.

    Beta is `cov(excess_a, excess_b) / var(excess_b)`, which is the OLS
    slope — computed directly rather than by calling a regression library,
    because it is one line and it removes a dependency.

    Alpha is annualised ARITHMETICALLY (`daily_alpha * periods`). Alpha is
    a per-period average abnormal return, not a compounded path, so
    compounding it would imply a reinvestment story the number does not
    carry.
    """
    pair = pd.concat([asset, benchmark], axis=1, keys=["a", "b"]).dropna()
    n = len(pair)
    if n < 3:
        return {"beta": float("nan"), "alpha_annual": float("nan"),
                "r_squared": float("nan"), "n_obs": float(n)}

    ea = pair["a"] - rf
    eb = pair["b"] - rf
    var_b = float(eb.var(ddof=1))
    if not np.isfinite(var_b) or var_b < FLAT_TOLERANCE ** 2:
        return {"beta": float("nan"), "alpha_annual": float("nan"),
                "r_squared": float("nan"), "n_obs": float(n)}

    beta = float(ea.cov(eb) / var_b)
    alpha_daily = float(ea.mean() - beta * eb.mean())
    corr = float(ea.corr(eb))
    return {
        "beta": beta,
        "alpha_annual": alpha_daily * periods,
        "r_squared": corr ** 2,
        "n_obs": float(n),
    }


def tracking_error(asset: pd.Series, benchmark: pd.Series,
                   periods: int = DEFAULT_PERIODS) -> float:
    """Annualised standard deviation of the active (asset - benchmark) return."""
    active = (asset - benchmark).dropna()
    if len(active) < 2:
        return float("nan")
    return float(active.std(ddof=1) * np.sqrt(periods))


def information_ratio(asset: pd.Series, benchmark: pd.Series,
                      periods: int = DEFAULT_PERIODS) -> float:
    """Mean active return over its own volatility, annualised."""
    active = (asset - benchmark).dropna()
    if len(active) < 2:
        return float("nan")
    sd = active.std(ddof=1)
    if not np.isfinite(sd) or sd < FLAT_TOLERANCE:
        return float("nan")
    return float(active.mean() / sd * np.sqrt(periods))


def capture_ratios(asset: pd.Series, benchmark: pd.Series) -> dict[str, float]:
    """Up- and down-market capture over the shared window.

    Ratio of GEOMETRIC MEAN per-period returns on the benchmark's up days
    and down days separately: "on an average day the market rose, this
    rose 1.3x as much".

    Deliberately not the ratio of compounded totals, which is the textbook
    formula and which breaks down over a long daily sample. Across eleven
    years SPY's up days compound to roughly +1,400,000% and its down days
    to roughly -99.99%. Both sides saturate: every leveraged series shows a
    down-capture of 1.00 because -99.99% and -100.00% are the same number
    to three digits, and NVDA showed an up-capture of 115,008 because the
    ratio of two enormous compounded totals is itself enormous. Taking the
    geometric mean per period first removes the horizon from the statistic,
    so the same portfolio measured over one year and over ten years reports
    the same capture.
    """
    pair = pd.concat([asset, benchmark], axis=1, keys=["a", "b"]).dropna()
    out: dict[str, float] = {}
    for label, mask in (("up_capture", pair["b"] > 0), ("down_capture", pair["b"] < 0)):
        sub = pair.loc[mask]
        if sub.empty:
            out[label] = float("nan")
            continue
        bench = _geometric_mean(sub["b"])
        asset_g = _geometric_mean(sub["a"])
        if not np.isfinite(bench) or not np.isfinite(asset_g) or abs(bench) < FLAT_TOLERANCE:
            out[label] = float("nan")
        else:
            out[label] = asset_g / bench
    return out


def _geometric_mean(returns: pd.Series) -> float:
    """Per-period geometric mean return, or NaN if the path hits zero."""
    growth = float((1.0 + returns).prod())
    if growth <= 0.0:
        # A -100% day wipes the series out; there is no real geometric mean.
        return float("nan")
    return growth ** (1.0 / len(returns)) - 1.0


def relative_performance(returns: pd.DataFrame, benchmark: str) -> pd.DataFrame:
    """Cumulative return of each symbol MINUS the benchmark's, through time.

    The difference of two cumulative curves, not the cumulative of the
    daily differences. The former answers "how far ahead am I today",
    which is the question a relative-performance chart is read for.
    """
    if benchmark not in returns.columns:
        raise KeyError(f"benchmark {benchmark!r} is not a column of the returns frame")
    cum = cumulative_returns(returns)
    return cum.drop(columns=[benchmark]).sub(cum[benchmark], axis=0)


# =====================================================================
# the summary table
# =====================================================================

def summary_table(returns: pd.DataFrame, benchmark: str, *,
                  rf_daily: float = 0.0, periods: int = DEFAULT_PERIODS) -> pd.DataFrame:
    """One row per symbol: absolute stats, then benchmark-relative stats.

    The benchmark gets a row too, with its relative columns filled in
    trivially (beta 1, alpha 0, tracking error 0) rather than left blank,
    so the table can be read as "everything against SPY, including SPY".
    """
    if benchmark not in returns.columns:
        raise KeyError(f"benchmark {benchmark!r} is not a column of the returns frame")

    bench = returns[benchmark]
    rows = []
    for sym in returns.columns:
        r = returns[sym].dropna()
        dd = max_drawdown(r)
        ba = beta_alpha(r, bench, rf=rf_daily, periods=periods)
        caps = capture_ratios(r, bench)
        is_bench = sym == benchmark

        pair = pd.concat([r, bench], axis=1, keys=["a", "b"]).dropna()
        active_ann = (annualised_return(pair["a"], periods)
                      - annualised_return(pair["b"], periods)) if len(pair) else float("nan")

        rows.append({
            "symbol": sym,
            "role": "benchmark" if is_bench else "universe",
            "n_obs": int(len(r)),
            "first_date": r.index.min().date().isoformat() if len(r) else "",
            "last_date": r.index.max().date().isoformat() if len(r) else "",
            "total_return": float((1.0 + r).prod() - 1.0) if len(r) else float("nan"),
            "cagr": annualised_return(r, periods),
            "ann_volatility": annualised_volatility(r, periods),
            "sharpe": sharpe_ratio(r, rf_daily, periods),
            "sortino": sortino_ratio(r, rf_daily, periods),
            "max_drawdown": dd["max_drawdown"],
            "max_dd_peak": dd["peak_date"].date().isoformat() if dd["peak_date"] is not None else "",
            "max_dd_trough": dd["trough_date"].date().isoformat() if dd["trough_date"] is not None else "",
            "max_dd_recovered": (dd["recovery_date"].date().isoformat()
                                 if dd["recovery_date"] is not None else "not yet"),
            "hit_rate": hit_rate(r),
            "var_95_1d": historical_var(r, 0.95),
            "best_day": float(r.max()) if len(r) else float("nan"),
            "worst_day": float(r.min()) if len(r) else float("nan"),
            # ---- relative to the benchmark ----
            "beta": 1.0 if is_bench else ba["beta"],
            "alpha_annual": 0.0 if is_bench else ba["alpha_annual"],
            "r_squared": 1.0 if is_bench else ba["r_squared"],
            "corr_to_benchmark": 1.0 if is_bench else float(r.corr(bench)),
            "tracking_error": 0.0 if is_bench else tracking_error(r, bench, periods),
            "information_ratio": float("nan") if is_bench else information_ratio(r, bench, periods),
            "active_return_annual": 0.0 if is_bench else active_ann,
            "up_capture": 1.0 if is_bench else caps["up_capture"],
            "down_capture": 1.0 if is_bench else caps["down_capture"],
            "overlap_with_benchmark": int(ba["n_obs"]),
        })

    out = pd.DataFrame.from_records(rows)
    # Benchmark first, then the universe by descending Sharpe: the table
    # is read top-down, and "did anything beat the index" is the question.
    out = pd.concat([
        out.loc[out["role"] == "benchmark"],
        out.loc[out["role"] == "universe"].sort_values("sharpe", ascending=False),
    ]).reset_index(drop=True)
    return out
