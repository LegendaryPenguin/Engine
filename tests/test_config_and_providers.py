"""
Config validation, the provider schema contract, and the ingest plan.

The ingest-plan tests are the reason `plan()` is a separate function from
`ingest()`: incremental download logic is easy to get subtly wrong (an
off-by-one at the window edge re-downloads ten years every run, or worse,
never re-downloads a restated bar) and it is all testable with no network.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from marketengine import store
from marketengine.config import load_config
from marketengine.ingest import RESTATEMENT_LOOKBACK, plan
from marketengine.providers import ProviderError, get_provider
from marketengine.providers.base import normalise

BASE = """
run_id: t
provider: yfinance
universe:
  tickers: [AAA, BBB, AAA]
  benchmark: spy
window:
  start: "2020-01-01"
  end: "2020-12-31"
calendar:
  align: benchmark
  max_ffill_days: 3
analytics:
  rolling_vol_windows: [21, 63]
  corr_window: 63
  risk_free_annual: 0.02
  trading_days_per_year: 252
  min_history_days: 250
  extreme_return_threshold: 0.35
storage:
  root: data
  duckdb_file: t.duckdb
outputs:
  figures: true
  dpi: 150
"""


def cfg_from(tmp_path, text: str = BASE):
    path = tmp_path / "c.yml"
    path.write_text(text)
    return load_config(path)


# =====================================================================
# config
# =====================================================================

def test_tickers_are_uppercased_and_deduplicated(tmp_path):
    cfg = cfg_from(tmp_path)
    assert cfg.universe.tickers == ("AAA", "BBB")
    assert cfg.universe.benchmark == "SPY"


def test_all_symbols_appends_the_benchmark_once(tmp_path):
    cfg = cfg_from(tmp_path)
    assert cfg.universe.all_symbols == ("AAA", "BBB", "SPY")

    # ...and does not duplicate it when it is also in the universe.
    cfg2 = cfg_from(tmp_path, BASE.replace("[AAA, BBB, AAA]", "[AAA, SPY]"))
    assert cfg2.universe.all_symbols == ("AAA", "SPY")


def test_risk_free_daily_is_geometric_not_divided_by_252(tmp_path):
    cfg = cfg_from(tmp_path)
    daily = cfg.analytics.risk_free_daily
    assert (1 + daily) ** 252 == pytest.approx(1.02)
    assert daily != pytest.approx(0.02 / 252)


def test_config_hash_changes_with_the_file_contents(tmp_path):
    a = cfg_from(tmp_path, BASE)
    b = cfg_from(tmp_path, BASE.replace("risk_free_annual: 0.02", "risk_free_annual: 0.03"))
    assert a.source_sha256 != b.source_sha256


def test_output_paths_are_namespaced_by_run_and_provider(tmp_path):
    cfg = cfg_from(tmp_path)
    assert cfg.raw_dir.as_posix().endswith("data/raw/yfinance")
    assert cfg.curated_dir.as_posix().endswith("data/curated/t")
    assert cfg.report_dir.as_posix() == "reports/t"


def test_unknown_provider_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="provider"):
        cfg_from(tmp_path, BASE.replace("provider: yfinance", "provider: bloomberg"))


def test_unknown_align_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="align"):
        cfg_from(tmp_path, BASE.replace("align: benchmark", "align: sideways"))


def test_end_before_start_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="end"):
        cfg_from(tmp_path, BASE.replace('end: "2020-12-31"', 'end: "2019-12-31"'))


def test_empty_universe_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="tickers"):
        cfg_from(tmp_path, BASE.replace("tickers: [AAA, BBB, AAA]", "tickers: []"))


def test_a_malformed_date_names_the_field(tmp_path):
    with pytest.raises(ValueError, match="window.start"):
        cfg_from(tmp_path, BASE.replace('start: "2020-01-01"', 'start: "01/01/2020"'))


def test_null_end_means_latest_available(tmp_path):
    cfg = cfg_from(tmp_path, BASE.replace('end: "2020-12-31"', "end: null"))
    assert cfg.window.end is None


# =====================================================================
# provider schema contract
# =====================================================================

def raw_frame(**overrides) -> pd.DataFrame:
    base = {
        "date": ["2024-01-02", "2024-01-03"],
        "symbol": ["aapl", "aapl"],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "adj_close": [1.0, 2.0], "volume": [10, 20],
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_normalise_uppercases_symbols_and_normalises_dates():
    out = normalise(raw_frame(), source="t")
    assert out["symbol"].tolist() == ["AAPL", "AAPL"]
    assert str(out["date"].dtype) == "datetime64[ns]"
    assert out["date"].iloc[0] == pd.Timestamp("2024-01-02")


def test_normalise_strips_timezones_so_two_vendors_can_join():
    # Same session, two different vendor timestamps. Both must land on the
    # same date key or a merge between providers silently produces nothing.
    a = normalise(raw_frame(date=["2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z"]), source="a")
    b = normalise(raw_frame(date=["2024-01-02T05:00:00+00:00", "2024-01-03T05:00:00+00:00"]),
                  source="b")
    assert a["date"].tolist() == b["date"].tolist()


def test_normalise_rejects_a_missing_column():
    df = raw_frame().drop(columns=["adj_close"])
    with pytest.raises(ProviderError, match="adj_close"):
        normalise(df, source="t")


def test_normalise_keeps_the_later_row_for_a_duplicated_session():
    df = pd.concat([raw_frame(), raw_frame(close=[99.0, 2.0], adj_close=[99.0, 2.0])])
    out = normalise(df, source="t")
    assert len(out) == 2
    assert out.loc[out["date"] == pd.Timestamp("2024-01-02"), "close"].iloc[0] == 99.0


def test_normalise_drops_a_bar_with_no_price_at_all():
    df = raw_frame(close=[1.0, None], adj_close=[1.0, None])
    out = normalise(df, source="t")
    assert len(out) == 1


def test_normalise_coerces_volume_to_float_so_nan_survives():
    out = normalise(raw_frame(volume=[10, None]), source="t")
    assert str(out["volume"].dtype) == "float64"


def test_get_provider_rejects_an_unknown_name():
    with pytest.raises(ProviderError, match="unknown provider"):
        get_provider("bloomberg")


def test_get_provider_builds_the_yfinance_provider():
    p = get_provider("yfinance")
    assert p.name == "yfinance"
    assert "adjustment" in p.describe()


# =====================================================================
# the incremental download plan
# =====================================================================

def store_symbol(raw_dir, symbol: str, first: str, last: str,
                 requested_from: str | None = None) -> None:
    """Put bars on disk as a completed ingest would have.

    `requested_from` is the start date the (simulated) fetch asked for, which
    is what `plan` reads to decide whether the front of the window is
    covered. It defaults to `first`; pass it explicitly to model a symbol
    whose stored bars begin later than the request did.
    """
    dates = pd.bdate_range(first, last)
    df = pd.DataFrame({
        "date": dates,
        "symbol": pd.Series([symbol] * len(dates), dtype="string"),
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "adj_close": 1.0,
        "volume": 1.0,
    })
    store.write_raw(raw_dir, symbol, df)
    store.record_request(raw_dir, symbol, date.fromisoformat(requested_from or first))


def test_plan_requests_the_full_window_when_nothing_is_stored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    assert plan(cfg) == {
        "AAA": (date(2020, 1, 1), date(2020, 12, 31)),
        "BBB": (date(2020, 1, 1), date(2020, 12, 31)),
        "SPY": (date(2020, 1, 1), date(2020, 12, 31)),
    }


def test_plan_skips_a_symbol_that_is_already_complete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    store_symbol(cfg.raw_dir, "AAA", "2020-01-01", "2020-12-31")
    assert plan(cfg)["AAA"] is None


def test_plan_tops_up_with_a_restatement_overlap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    store_symbol(cfg.raw_dir, "AAA", "2020-01-01", "2020-06-30")
    start, end = plan(cfg)["AAA"]
    # Reaches BACK into stored data, so a restated recent bar is re-fetched.
    assert start == (pd.Timestamp("2020-06-30") - RESTATEMENT_LOOKBACK).date()
    assert start < date(2020, 6, 30)
    assert end == date(2020, 12, 31)


def test_plan_refetches_the_whole_window_when_history_is_missing_at_the_front(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    store_symbol(cfg.raw_dir, "AAA", "2020-07-01", "2020-12-31")
    assert plan(cfg)["AAA"] == (date(2020, 1, 1), date(2020, 12, 31))


def test_plan_does_not_refetch_when_the_window_starts_on_a_holiday(tmp_path, monkeypatch):
    # `window.start: 2020-01-01` is New Year's Day, so the earliest bar that
    # can ever exist is 2020-01-02. Comparing the stored first bar against
    # the configured start would report a permanent front gap and
    # re-download the whole history on every run.
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    store_symbol(cfg.raw_dir, "AAA", "2020-01-02", "2020-12-31",
                 requested_from="2020-01-01")
    assert plan(cfg)["AAA"] is None


def test_plan_does_not_refetch_a_symbol_that_listed_mid_window(tmp_path, monkeypatch):
    # The request covered the front of the window; the vendor simply has no
    # bars before the IPO. That is not a gap and must not be retried daily.
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    store_symbol(cfg.raw_dir, "AAA", "2020-09-01", "2020-12-31",
                 requested_from="2020-01-01")
    assert plan(cfg)["AAA"] is None


def test_plan_refetches_when_the_window_is_widened_backwards(tmp_path, monkeypatch):
    # Editing the config to ask for earlier history is a real front gap.
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    store_symbol(cfg.raw_dir, "AAA", "2020-01-02", "2020-12-31",
                 requested_from="2020-01-01")
    wider = cfg_from(tmp_path, BASE.replace('start: "2020-01-01"', 'start: "2018-01-01"'))
    assert plan(wider)["AAA"] == (date(2018, 1, 1), date(2020, 12, 31))


def test_a_failed_fetch_is_not_recorded_as_covered(tmp_path, monkeypatch):
    # No request recorded => the front is unknown => fetch the full window.
    # This is what keeps a partial failure from becoming a permanent hole.
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    assert store.read_requests(cfg.raw_dir) == {}
    assert plan(cfg)["AAA"] == (date(2020, 1, 1), date(2020, 12, 31))


def test_record_request_keeps_the_earliest_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    store.record_request(cfg.raw_dir, "AAA", date(2020, 1, 1))
    store.record_request(cfg.raw_dir, "AAA", date(2020, 6, 1))  # a top-up
    assert store.read_requests(cfg.raw_dir)["AAA"] == "2020-01-01"


def test_refresh_ignores_what_is_on_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    store_symbol(cfg.raw_dir, "AAA", "2020-01-01", "2020-12-31")
    assert plan(cfg, refresh=True)["AAA"] == (date(2020, 1, 1), date(2020, 12, 31))


def test_write_raw_merges_and_prefers_the_newer_row(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    store_symbol(cfg.raw_dir, "AAA", "2020-01-01", "2020-01-31")

    restated = pd.DataFrame({
        "date": [pd.Timestamp("2020-01-15")],
        "symbol": pd.Series(["AAA"], dtype="string"),
        "open": [9.0], "high": [9.0], "low": [9.0], "close": [9.0],
        "adj_close": [9.0], "volume": [9.0],
    })
    store.write_raw(cfg.raw_dir, "AAA", restated)

    got = store.read_raw(cfg.raw_dir, ["AAA"]).set_index("date")
    assert got.loc[pd.Timestamp("2020-01-15"), "close"] == 9.0
    assert got.index.is_unique          # merged, not appended
    assert len(got) == len(pd.bdate_range("2020-01-01", "2020-01-31"))


def test_raw_coverage_is_none_for_an_unknown_symbol(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = cfg_from(tmp_path)
    assert store.raw_coverage(cfg.raw_dir, "ZZZ") is None


def test_wide_pivot_is_sorted_and_stable(tmp_path):
    panel = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-03", "2024-01-02"] * 2),
        "symbol": ["ZZZ", "ZZZ", "AAA", "AAA"],
        "adj_close": [2.0, 1.0, 20.0, 10.0],
    })
    out = store.wide(panel, "adj_close")
    assert list(out.columns) == ["AAA", "ZZZ"]
    assert out.index.is_monotonic_increasing
