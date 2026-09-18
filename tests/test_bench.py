"""
Tests for the measurement code.

A benchmark that lies is worse than no benchmark, so the parts that can be
tested without a network are: the percentile function (off-by-one here
silently reports the median as the p95), the session calendar, and the
freshness comparison. The timing itself is not asserted on — a test that
checks `seconds > 0` proves nothing and a test that checks `seconds < 0.1`
fails on a loaded CI box.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from marketengine import bench, store
from marketengine.config import load_config

CONFIG = """
run_id: test
provider: yfinance
universe:
  tickers: [AAA, BBB]
  benchmark: SPY
window:
  start: "2024-01-01"
  end: null
calendar:
  align: benchmark
  max_ffill_days: 0
analytics:
  rolling_vol_windows: [5]
  corr_window: 5
  min_history_days: 3
storage:
  root: {root}
  duckdb_file: test.duckdb
outputs:
  figures: false
"""


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "cfg.yml"
    path.write_text(CONFIG.format(root=tmp_path / "data"))
    return load_config(path)


def store_bars(cfg, symbol: str, last: str, n: int = 30) -> None:
    dates = pd.bdate_range(end=last, periods=n)
    store.write_raw(cfg.raw_dir, symbol, pd.DataFrame({
        "date": dates,
        "symbol": pd.Series([symbol] * len(dates), dtype="string"),
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "adj_close": 1.0,
        "volume": 1.0,
    }))


# ------------------------------------------------------------- percentiles


def test_percentile_returns_an_observation_not_an_interpolation():
    """p95 of ten samples is the tenth-largest, a thing that happened."""
    samples = [float(x) for x in range(1, 11)]
    assert bench._percentile(samples, 0.50) == 5.0  # nearest rank: ceil(0.5*10)
    assert bench._percentile(samples, 0.95) == 10.0
    assert bench._percentile(samples, 1.00) == 10.0
    assert bench._percentile(samples, 0.0) == 1.0


def test_percentile_of_one_sample_is_that_sample():
    assert bench._percentile([42.0], 0.95) == 42.0


def test_percentile_of_nothing_is_nan():
    assert bench._percentile([], 0.5) != bench._percentile([], 0.5)  # NaN


def test_percentile_ignores_input_order():
    assert bench._percentile([9.0, 1.0, 5.0], 0.0) == 1.0


# ---------------------------------------------------------------- calendar


@pytest.mark.parametrize(("now", "expected"), [
    # Thursday mid-afternoon UTC: the US session has not closed, so the last
    # published close is Wednesday's.
    ("2026-09-17T15:00:00+00:00", "2026-09-16"),
    # Thursday late UTC, after the 22:00 cutoff: Thursday's close exists.
    ("2026-09-17T23:00:00+00:00", "2026-09-17"),
    # Saturday: still Friday's close, whatever the hour.
    ("2026-09-19T23:00:00+00:00", "2026-09-18"),
    # Sunday morning: Friday's.
    ("2026-09-20T09:00:00+00:00", "2026-09-18"),
    # Monday morning: Friday's, not Sunday's.
    ("2026-09-21T09:00:00+00:00", "2026-09-18"),
])
def test_expected_last_session(now, expected):
    got = bench.expected_last_session(datetime.fromisoformat(now))
    assert got.isoformat() == expected


# --------------------------------------------------------------- freshness


def test_freshness_marks_a_lagging_symbol(cfg):
    store_bars(cfg, "SPY", "2026-09-18")
    store_bars(cfg, "AAA", "2026-09-18")
    store_bars(cfg, "BBB", "2026-09-15")  # three sessions behind

    out = bench.freshness(cfg, now=datetime(2026, 9, 18, 23, tzinfo=timezone.utc))
    by_symbol = out.set_index("symbol")

    assert bool(by_symbol.loc["AAA", "current"]) is True
    assert bool(by_symbol.loc["BBB", "current"]) is False
    # Counted in SPY's sessions, not calendar days: 16th, 17th, 18th.
    assert int(by_symbol.loc["BBB", "sessions_behind_benchmark"]) == 3
    assert int(by_symbol.loc["AAA", "sessions_behind_benchmark"]) == 0


def test_freshness_reports_a_symbol_with_no_data_at_all(cfg):
    store_bars(cfg, "SPY", "2026-09-18")
    store_bars(cfg, "AAA", "2026-09-18")
    # BBB was never ingested.

    out = bench.freshness(cfg, now=datetime(2026, 9, 18, 23, tzinfo=timezone.utc)
                          ).set_index("symbol")
    assert pd.isna(out.loc["BBB", "last_bar"])
    assert bool(out.loc["BBB", "current"]) is False
    assert "no data" in out.loc["BBB", "note"]


def test_freshness_flags_an_unclosed_session_as_provisional(cfg):
    """yfinance serves a partial bar for the session in progress.

    Trading on it means trading on a "close" that is really the last trade
    so far, which will have changed by 16:00. The check has to fire.
    """
    store_bars(cfg, "SPY", "2026-09-18")
    store_bars(cfg, "AAA", "2026-09-18")
    store_bars(cfg, "BBB", "2026-09-18")

    # 15:00 UTC on the 18th: the 18th has not closed yet, but there is a bar.
    out = bench.freshness(cfg, now=datetime(2026, 9, 18, 15, tzinfo=timezone.utc))
    assert out.attrs["expected_last_session"] == "2026-09-17"
    assert out.attrs["benchmark_last_session"] == "2026-09-18"
    assert out.attrs["provisional_last_session"] is True


def test_freshness_does_not_cry_provisional_on_a_closed_session(cfg):
    store_bars(cfg, "SPY", "2026-09-18")
    store_bars(cfg, "AAA", "2026-09-18")
    store_bars(cfg, "BBB", "2026-09-18")

    out = bench.freshness(cfg, now=datetime(2026, 9, 18, 23, tzinfo=timezone.utc))
    assert out.attrs["provisional_last_session"] is False


# ------------------------------------------------------------- measurement


def test_measure_pipeline_without_network_times_every_compute_stage(cfg):
    for sym in ("SPY", "AAA", "BBB"):
        store_bars(cfg, sym, "2026-09-18", n=60)

    result = bench.measure_pipeline(cfg, network=False)
    names = [s.name for s in result.stages]

    assert names == ["read_raw", "clean", "write_curated", "analytics"]
    assert "ingest" not in names
    assert all(s.seconds >= 0 for s in result.stages)
    assert result.stages[0].rows == 180
    assert result.environment["provider"] == "yfinance"
    # The report has to say that ingest was skipped, or the total looks like
    # an end-to-end number when it is a compute-only one.
    assert any("no-network" in n for n in result.notes)


def test_measure_pipeline_refuses_to_measure_nothing(cfg):
    with pytest.raises(ValueError, match="no raw data"):
        bench.measure_pipeline(cfg, network=False)


def test_render_and_to_json_survive_a_measurement(cfg):
    for sym in ("SPY", "AAA", "BBB"):
        store_bars(cfg, sym, "2026-09-18", n=60)

    result = bench.measure_pipeline(cfg, network=False)
    result.freshness = bench.freshness(cfg, now=datetime(2026, 9, 18, 23, tzinfo=timezone.utc))

    text = bench.render(result)
    assert "stage timings" in text and "data freshness" in text

    blob = bench.to_json(result)
    assert blob["freshness"]["benchmark_last_session"] == "2026-09-18"
    assert len(blob["stages"]) == len(result.stages)
    # Must be JSON-serialisable for real, not just dict-shaped.
    import json
    json.loads(json.dumps(blob, default=str))


def test_timed_records_a_stage_even_when_the_body_raises():
    """Otherwise a failure mid-run reports a total that omits the slow part."""
    result = bench.Result()
    with pytest.raises(RuntimeError):
        with bench.timed(result, "boom"):
            raise RuntimeError("nope")
    assert [s.name for s in result.stages] == ["boom"]
