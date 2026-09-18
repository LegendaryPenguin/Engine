"""
Cleaning tests.

These pin the gap policy, because the gap policy is the part of this
pipeline that can quietly invent data. Each test builds a panel with a
hand-placed hole and asserts what the panel looks like afterwards.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from marketengine.clean import FILLED_FLAG, clean
from marketengine.config import load_config

CONFIG = """
run_id: test
provider: yfinance
universe:
  tickers: [AAA, BBB]
  benchmark: SPY
window:
  start: "2024-01-01"
  end: "2024-01-31"
calendar:
  align: benchmark
  max_ffill_days: 2
analytics:
  rolling_vol_windows: [5]
  corr_window: 5
  risk_free_annual: 0.02
  trading_days_per_year: 252
  min_history_days: 3
  extreme_return_threshold: 0.35
storage:
  root: data
  duckdb_file: test.duckdb
outputs:
  figures: false
"""


def write_config(tmp_path, **overrides) -> "object":
    """Materialise the config above, with `key: value` line overrides."""
    text = CONFIG
    for key, value in overrides.items():
        # Only ever used for single scalar keys in this file.
        text = "\n".join(
            f"  {key}: {value}" if line.strip().startswith(f"{key}:") else line
            for line in text.splitlines()
        )
    path = tmp_path / "cfg.yml"
    path.write_text(text)
    return load_config(path)


def panel(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Build a minimal raw panel from (symbol, date, price) triples."""
    return pd.DataFrame({
        "date": pd.to_datetime([d for _, d, _ in rows]),
        "symbol": pd.Series([s for s, _, _ in rows], dtype="string"),
        "open": [p for _, _, p in rows],
        "high": [p for _, _, p in rows],
        "low": [p for _, _, p in rows],
        "close": [p for _, _, p in rows],
        "adj_close": [p for _, _, p in rows],
        "volume": [1_000.0 for _ in rows],
    })


BDAYS = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=10)]


def full(symbol: str, prices=None) -> list[tuple[str, str, float]]:
    prices = prices or [100.0 + i for i in range(len(BDAYS))]
    return [(symbol, d, p) for d, p in zip(BDAYS, prices)]


def test_benchmark_alignment_drops_dates_the_benchmark_did_not_trade(tmp_path):
    cfg = write_config(tmp_path)
    extra_day = "2024-01-13"  # a Saturday: only AAA "traded"
    raw = panel(full("SPY") + full("AAA") + [("AAA", extra_day, 999.0)] + full("BBB"))

    out, _ = clean(cfg, raw)

    assert pd.Timestamp(extra_day) not in set(out["date"])
    assert 999.0 not in set(out["adj_close"])


def test_a_short_gap_is_forward_filled_and_flagged(tmp_path):
    cfg = write_config(tmp_path)  # max_ffill_days: 2
    aaa = [r for r in full("AAA") if r[1] != BDAYS[4]]  # one missing session
    raw = panel(full("SPY") + aaa + full("BBB"))

    out, quality = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"].set_index("date")

    assert pd.Timestamp(BDAYS[4]) in a.index
    assert bool(a.loc[pd.Timestamp(BDAYS[4]), FILLED_FLAG]) is True
    # The carried price is the previous session's, so the return is 0.
    assert a.loc[pd.Timestamp(BDAYS[4]), "adj_close"] == pytest.approx(
        a.loc[pd.Timestamp(BDAYS[3]), "adj_close"])
    # Volume is NOT carried: a filled bar has unknown volume.
    assert np.isnan(a.loc[pd.Timestamp(BDAYS[4]), "volume"])

    q = quality.set_index("symbol").loc["AAA"]
    assert q["gaps_found"] == 1
    assert q["gaps_forward_filled"] == 1
    assert q["rows_dropped_long_gap"] == 0


def test_a_gap_longer_than_the_limit_is_dropped_not_filled(tmp_path):
    cfg = write_config(tmp_path)  # max_ffill_days: 2
    missing = {BDAYS[3], BDAYS[4], BDAYS[5], BDAYS[6]}  # four sessions
    aaa = [r for r in full("AAA") if r[1] not in missing]
    raw = panel(full("SPY") + aaa + full("BBB"))

    out, quality = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"]
    kept = set(a["date"])

    # The first two of the gap are within the limit and get carried; the
    # rest stay missing and their rows are dropped rather than invented.
    assert pd.Timestamp(BDAYS[3]) in kept
    assert pd.Timestamp(BDAYS[4]) in kept
    assert pd.Timestamp(BDAYS[5]) not in kept
    assert pd.Timestamp(BDAYS[6]) not in kept

    q = quality.set_index("symbol").loc["AAA"]
    assert q["rows_dropped_long_gap"] == 2


def test_no_fill_at_all_when_max_ffill_days_is_zero(tmp_path):
    cfg = write_config(tmp_path, max_ffill_days=0)
    aaa = [r for r in full("AAA") if r[1] != BDAYS[4]]
    raw = panel(full("SPY") + aaa + full("BBB"))

    out, quality = clean(cfg, raw)
    assert pd.Timestamp(BDAYS[4]) not in set(out.loc[out["symbol"] == "AAA", "date"])
    assert int(quality.set_index("symbol").loc["AAA", "gaps_forward_filled"]) == 0


def test_a_late_listing_starts_at_its_own_first_session(tmp_path):
    cfg = write_config(tmp_path)
    late = [(s, d, p) for s, d, p in full("BBB") if d in BDAYS[5:]]
    raw = panel(full("SPY") + full("AAA") + late)

    out, quality = clean(cfg, raw)
    b = out.loc[out["symbol"] == "BBB"]

    assert b["date"].min() == pd.Timestamp(BDAYS[5])
    # No fabricated pre-listing history, and nothing counted as a gap.
    assert len(b) == 5
    q = quality.set_index("symbol").loc["BBB"]
    assert q["gaps_found"] == 0
    assert q["rows_dropped_long_gap"] == 0


def test_an_extreme_move_is_counted_and_kept(tmp_path):
    cfg = write_config(tmp_path)
    prices = [100.0] * 5 + [200.0] * 5  # +100% in one session
    raw = panel(full("SPY") + full("AAA", prices) + full("BBB"))

    out, quality = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"]

    assert 200.0 in set(a["adj_close"])  # kept
    assert int(quality.set_index("symbol").loc["AAA", "abs_return_gt_0.35"]) == 1


def test_short_history_is_flagged_not_removed(tmp_path):
    cfg = write_config(tmp_path)  # min_history_days: 3
    tiny = [(s, d, p) for s, d, p in full("BBB") if d in BDAYS[8:]]  # 2 sessions
    raw = panel(full("SPY") + full("AAA") + tiny)

    out, quality = clean(cfg, raw)
    assert "BBB" in set(out["symbol"])
    assert bool(quality.set_index("symbol").loc["BBB", "short_history"]) is True


def test_unadjusted_data_is_flagged(tmp_path):
    cfg = write_config(tmp_path)
    raw = panel(full("SPY") + full("AAA") + full("BBB"))  # adj_close == close
    _, quality = clean(cfg, raw)
    assert bool(quality.set_index("symbol").loc["AAA", "adj_close_equals_close"]) is True


def test_clean_refuses_an_empty_panel(tmp_path):
    cfg = write_config(tmp_path)
    with pytest.raises(ValueError, match="empty"):
        clean(cfg, panel([]))


def test_clean_refuses_a_window_with_no_rows_in_it(tmp_path):
    cfg = write_config(tmp_path)
    raw = panel([("SPY", "2019-06-03", 100.0), ("AAA", "2019-06-03", 50.0)])
    with pytest.raises(ValueError, match="window"):
        clean(cfg, raw)


def test_missing_benchmark_is_a_clear_error_under_benchmark_alignment(tmp_path):
    cfg = write_config(tmp_path)
    raw = panel(full("AAA") + full("BBB"))  # no SPY
    with pytest.raises(ValueError, match="benchmark"):
        clean(cfg, raw)
