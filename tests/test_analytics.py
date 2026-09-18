"""
Analytics tests.

Every case here has a closed-form answer worked out by hand, because the
only way to know a Sharpe ratio is right is to compare it against a number
that did not come from the same code. "It ran and produced a float" is not
a test of a metric.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from marketengine import analytics as an

PERIODS = 252


def days(n: int, start: str = "2020-01-01") -> pd.DatetimeIndex:
    """n consecutive business days. Business days, not calendar days, so a
    252-length series really is "one year" for annualisation purposes."""
    return pd.bdate_range(start, periods=n)


# =====================================================================
# returns
# =====================================================================

def test_simple_returns_drops_first_row_and_is_exact():
    prices = pd.Series([100.0, 110.0, 99.0], index=days(3))
    got = an.simple_returns(prices)
    assert len(got) == 2
    assert got.iloc[0] == pytest.approx(0.10)
    assert got.iloc[1] == pytest.approx(-0.10)


def test_log_returns_sum_to_total_log_return():
    prices = pd.Series([100.0, 105.0, 98.0, 121.0], index=days(4))
    lr = an.log_returns(prices)
    assert float(lr.sum()) == pytest.approx(np.log(121.0 / 100.0))


def test_cumulative_returns_compound_and_start_from_first_day():
    r = pd.Series([0.10, -0.10, 0.05], index=days(3))
    cum = an.cumulative_returns(r)
    assert cum.iloc[0] == pytest.approx(0.10)
    assert cum.iloc[-1] == pytest.approx(1.10 * 0.90 * 1.05 - 1.0)


def test_cumulative_returns_treats_missing_day_as_flat_not_as_poison():
    # A plain cumprod over a NaN blanks every later value. That behaviour
    # would silently truncate a whole equity curve, so it is pinned here.
    r = pd.Series([0.10, np.nan, 0.10], index=days(3))
    cum = an.cumulative_returns(r)
    assert cum.notna().all()
    assert cum.iloc[-1] == pytest.approx(1.10 * 1.10 - 1.0)


# =====================================================================
# single-series statistics
# =====================================================================

def test_annualised_return_is_geometric():
    # 252 days at exactly +0.1%/day compounds to 1.001**252 - 1.
    r = pd.Series([0.001] * PERIODS, index=days(PERIODS))
    assert an.annualised_return(r, PERIODS) == pytest.approx(1.001 ** PERIODS - 1.0)


def test_annualised_return_is_period_length_invariant_for_a_constant_series():
    # Two years of the same daily return must annualise to the same CAGR
    # as one year. This is the property `mean * 252` also has, so the test
    # that distinguishes them is the geometric one above.
    short = pd.Series([0.001] * PERIODS, index=days(PERIODS))
    long = pd.Series([0.001] * (2 * PERIODS), index=days(2 * PERIODS))
    assert an.annualised_return(short, PERIODS) == pytest.approx(
        an.annualised_return(long, PERIODS))


def test_annualised_return_is_nan_for_a_total_wipeout():
    r = pd.Series([-1.0, 0.05], index=days(2))
    assert np.isnan(an.annualised_return(r, PERIODS))


def test_annualised_volatility_scales_by_sqrt_periods():
    rng = np.random.default_rng(42)
    r = pd.Series(rng.normal(0, 0.01, 1000), index=days(1000))
    assert an.annualised_volatility(r, PERIODS) == pytest.approx(
        r.std(ddof=1) * np.sqrt(PERIODS))


def test_annualised_volatility_of_a_constant_series_is_zero():
    r = pd.Series([0.002] * 50, index=days(50))
    assert an.annualised_volatility(r, PERIODS) == pytest.approx(0.0)


def test_sharpe_uses_a_daily_risk_free_rate():
    r = pd.Series([0.001] * 100 + [-0.0005] * 100, index=days(200))
    rf = 0.0001
    excess = r - rf
    assert an.sharpe_ratio(r, rf, PERIODS) == pytest.approx(
        excess.mean() / excess.std(ddof=1) * np.sqrt(PERIODS))


def test_sharpe_is_nan_when_there_is_no_variation():
    r = pd.Series([0.001] * 30, index=days(30))
    assert np.isnan(an.sharpe_ratio(r, 0.0, PERIODS))


def test_sortino_ignores_upside_and_exceeds_sharpe_when_losses_are_small():
    # Many small gains, few small losses: downside deviation is below total
    # deviation, so Sortino must come out above Sharpe.
    r = pd.Series([0.01] * 40 + [-0.002] * 10, index=days(50))
    assert an.sortino_ratio(r, 0.0, PERIODS) > an.sharpe_ratio(r, 0.0, PERIODS)


def test_max_drawdown_finds_the_depth_and_the_right_peak():
    # 100 -> 120 -> 60 -> 130. The deepest drawdown is -50% from the 120
    # peak, NOT from the 130 high at the end.
    r = pd.Series([0.20, -0.50, 130.0 / 60.0 - 1.0], index=days(3))
    out = an.max_drawdown(r)
    assert out["max_drawdown"] == pytest.approx(-0.50)
    assert out["peak_date"] == r.index[0]
    assert out["trough_date"] == r.index[1]
    assert out["recovery_date"] == r.index[2]


def test_max_drawdown_reports_no_recovery_as_none():
    r = pd.Series([0.10, -0.40, 0.01], index=days(3))
    assert an.max_drawdown(r)["recovery_date"] is None


def test_drawdown_series_is_never_positive():
    rng = np.random.default_rng(7)
    r = pd.Series(rng.normal(0.0005, 0.02, 500), index=days(500))
    assert (an.drawdown_series(r) <= 1e-12).all()


def test_historical_var_is_the_empirical_quantile():
    r = pd.Series(np.linspace(-0.10, 0.10, 101), index=days(101))
    assert an.historical_var(r, 0.95) == pytest.approx(np.quantile(r, 0.05))


def test_hit_rate_counts_strictly_positive_days():
    r = pd.Series([0.01, 0.0, -0.01, 0.02], index=days(4))
    assert an.hit_rate(r) == pytest.approx(0.5)


def test_rolling_volatility_needs_a_full_window():
    r = pd.Series([0.01, -0.01] * 20, index=days(40))
    vol = an.rolling_volatility(r, 10, PERIODS)
    assert vol.iloc[:9].isna().all()
    assert vol.iloc[9:].notna().all()


# =====================================================================
# cross-sectional
# =====================================================================

def test_correlation_matrix_blanks_pairs_with_too_little_overlap():
    idx = days(100)
    a = pd.Series(np.linspace(0.01, 0.02, 100), index=idx)
    short = pd.Series([np.nan] * 90 + [0.01] * 10, index=idx)
    corr = an.correlation_matrix(pd.DataFrame({"A": a, "SHORT": short}), min_periods=60)
    assert corr.loc["A", "A"] == pytest.approx(1.0)
    assert np.isnan(corr.loc["A", "SHORT"])


def test_rolling_correlation_excludes_the_benchmark_column():
    rng = np.random.default_rng(3)
    idx = days(300)
    df = pd.DataFrame({"SPY": rng.normal(0, 0.01, 300),
                       "AAA": rng.normal(0, 0.01, 300)}, index=idx)
    out = an.rolling_correlation(df, "SPY", 60)
    assert list(out.columns) == ["AAA"]
    assert out["AAA"].iloc[:59].isna().all()


def test_rolling_correlation_rejects_an_unknown_benchmark():
    df = pd.DataFrame({"AAA": [0.01, 0.02]}, index=days(2))
    with pytest.raises(KeyError):
        an.rolling_correlation(df, "SPY", 2)


# =====================================================================
# benchmark-relative
# =====================================================================

def test_beta_of_an_exact_double_of_the_benchmark_is_two():
    rng = np.random.default_rng(11)
    bench = pd.Series(rng.normal(0, 0.01, 400), index=days(400))
    asset = 2.0 * bench
    out = an.beta_alpha(asset, bench, rf=0.0, periods=PERIODS)
    assert out["beta"] == pytest.approx(2.0)
    assert out["alpha_annual"] == pytest.approx(0.0, abs=1e-12)
    assert out["r_squared"] == pytest.approx(1.0)
    assert out["n_obs"] == 400


def test_alpha_is_the_constant_offset_annualised_arithmetically():
    rng = np.random.default_rng(5)
    bench = pd.Series(rng.normal(0, 0.01, 500), index=days(500))
    asset = bench + 0.0004  # 4 bp/day of pure alpha, beta 1
    out = an.beta_alpha(asset, bench, rf=0.0, periods=PERIODS)
    assert out["beta"] == pytest.approx(1.0)
    assert out["alpha_annual"] == pytest.approx(0.0004 * PERIODS)


def test_beta_alpha_uses_only_the_shared_window():
    idx = days(300)
    rng = np.random.default_rng(13)
    bench = pd.Series(rng.normal(0, 0.01, 300), index=idx)
    asset = (2.0 * bench).copy()
    asset.iloc[:100] = np.nan  # listed late
    out = an.beta_alpha(asset, bench, rf=0.0, periods=PERIODS)
    assert out["n_obs"] == 200
    assert out["beta"] == pytest.approx(2.0)


def test_beta_alpha_is_nan_when_the_overlap_is_too_short():
    idx = days(2)
    out = an.beta_alpha(pd.Series([0.01, 0.02], index=idx),
                        pd.Series([0.01, 0.02], index=idx))
    assert np.isnan(out["beta"])


def test_tracking_error_and_information_ratio_are_zero_and_nan_against_self():
    rng = np.random.default_rng(17)
    bench = pd.Series(rng.normal(0, 0.01, 200), index=days(200))
    assert an.tracking_error(bench, bench, PERIODS) == pytest.approx(0.0)
    assert np.isnan(an.information_ratio(bench, bench, PERIODS))


def test_tracking_error_of_a_constant_offset_is_zero():
    # A fixed daily outperformance has no volatility of active return, so
    # the tracking error is 0 even though the active return is positive.
    rng = np.random.default_rng(19)
    bench = pd.Series(rng.normal(0, 0.01, 200), index=days(200))
    assert an.tracking_error(bench + 0.001, bench, PERIODS) == pytest.approx(0.0)


def test_capture_ratios_of_a_two_times_levered_series_are_about_two():
    # Small moves, so compounding is close to linear and the capture of a
    # 2x series lands near 2 in both directions.
    rng = np.random.default_rng(23)
    bench = pd.Series(rng.normal(0, 0.001, 200), index=days(200))
    caps = an.capture_ratios(2.0 * bench, bench)
    assert caps["up_capture"] == pytest.approx(2.0, rel=0.05)
    assert caps["down_capture"] == pytest.approx(2.0, rel=0.05)


def test_capture_ratios_do_not_saturate_over_a_long_noisy_sample():
    # The bug this replaced: with a ratio of COMPOUNDED totals, 400 days at
    # 2% daily vol drove every leveraged series to a down-capture of 1.00
    # (both sides pinned against -100%) and blew up up-capture into the
    # thousands. Geometric means keep it near 2 in both directions.
    rng = np.random.default_rng(23)
    bench = pd.Series(rng.normal(0, 0.02, 400), index=days(400))
    caps = an.capture_ratios(2.0 * bench, bench)
    assert caps["up_capture"] == pytest.approx(2.0, rel=0.15)
    assert caps["down_capture"] == pytest.approx(2.0, rel=0.15)


def test_capture_ratios_are_horizon_invariant():
    # Same return process, ten times the sample: capture must not drift.
    rng = np.random.default_rng(41)
    long = pd.Series(rng.normal(0.0003, 0.012, 3000), index=days(3000))
    short = long.iloc[:300]
    for key in ("up_capture", "down_capture"):
        assert an.capture_ratios(1.5 * long, long)[key] == pytest.approx(
            an.capture_ratios(1.5 * short, short)[key], rel=0.15)


def test_capture_ratios_of_a_low_beta_asset_are_small_but_not_zero():
    # Under the old compounded formula this collapsed to exactly 0.0 for
    # GLD over an eleven-year window, which read as "never participates".
    rng = np.random.default_rng(43)
    bench = pd.Series(rng.normal(0.0004, 0.011, 2900), index=days(2900))
    sleepy = 0.1 * bench + rng.normal(0, 0.001, 2900)
    caps = an.capture_ratios(pd.Series(sleepy, index=bench.index), bench)
    assert 0.0 < caps["up_capture"] < 0.5


def test_capture_ratios_are_nan_after_a_total_wipeout():
    bench = pd.Series([0.01, 0.02, -0.01], index=days(3))
    asset = pd.Series([-1.0, 0.02, -0.01], index=days(3))
    assert np.isnan(an.capture_ratios(asset, bench)["up_capture"])


def test_information_ratio_is_nan_for_a_constant_active_return():
    # Constant active return means zero active volatility, so the ratio is
    # undefined. Floating point makes that std ~1e-19 rather than 0, and an
    # `== 0` guard would report an IR of 1e16.
    rng = np.random.default_rng(37)
    bench = pd.Series(rng.normal(0, 0.01, 200), index=days(200))
    assert np.isnan(an.information_ratio(bench + 0.001, bench, PERIODS))


def test_capture_ratios_against_self_are_one():
    rng = np.random.default_rng(29)
    bench = pd.Series(rng.normal(0, 0.015, 300), index=days(300))
    caps = an.capture_ratios(bench, bench)
    assert caps["up_capture"] == pytest.approx(1.0)
    assert caps["down_capture"] == pytest.approx(1.0)


def test_relative_performance_is_the_difference_of_cumulative_curves():
    idx = days(3)
    df = pd.DataFrame({"SPY": [0.01, 0.01, 0.01], "AAA": [0.02, 0.02, 0.02]}, index=idx)
    rel = an.relative_performance(df, "SPY")
    assert list(rel.columns) == ["AAA"]
    expected = (1.02 ** 3 - 1.0) - (1.01 ** 3 - 1.0)
    assert rel["AAA"].iloc[-1] == pytest.approx(expected)


# =====================================================================
# the summary table
# =====================================================================

@pytest.fixture
def frame() -> pd.DataFrame:
    rng = np.random.default_rng(31)
    idx = days(600)
    bench = rng.normal(0.0004, 0.010, 600)
    return pd.DataFrame(
        {"SPY": bench, "LEV": 2.0 * bench, "FLAT": np.full(600, 0.0002)}, index=idx
    )


def test_summary_table_has_one_row_per_symbol_and_benchmark_first(frame):
    out = an.summary_table(frame, "SPY", rf_daily=0.0, periods=PERIODS)
    assert len(out) == 3
    assert out.iloc[0]["symbol"] == "SPY"
    assert out.iloc[0]["role"] == "benchmark"


def test_summary_table_fills_the_benchmarks_own_relative_columns(frame):
    out = an.summary_table(frame, "SPY", rf_daily=0.0, periods=PERIODS).set_index("symbol")
    assert out.loc["SPY", "beta"] == pytest.approx(1.0)
    assert out.loc["SPY", "alpha_annual"] == pytest.approx(0.0)
    assert out.loc["SPY", "tracking_error"] == pytest.approx(0.0)
    assert out.loc["SPY", "corr_to_benchmark"] == pytest.approx(1.0)


def test_summary_table_agrees_with_the_standalone_functions(frame):
    out = an.summary_table(frame, "SPY", rf_daily=0.0001, periods=PERIODS).set_index("symbol")
    lev = frame["LEV"]
    assert out.loc["LEV", "cagr"] == pytest.approx(an.annualised_return(lev, PERIODS))
    assert out.loc["LEV", "sharpe"] == pytest.approx(an.sharpe_ratio(lev, 0.0001, PERIODS))
    assert out.loc["LEV", "beta"] == pytest.approx(2.0)
    assert out.loc["LEV", "max_drawdown"] == pytest.approx(
        an.max_drawdown(lev)["max_drawdown"])


def test_summary_table_rejects_a_missing_benchmark(frame):
    with pytest.raises(KeyError):
        an.summary_table(frame, "NOPE")
