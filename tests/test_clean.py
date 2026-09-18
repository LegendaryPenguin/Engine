"""
Cleaning tests.

These pin the gap policy and the quality flags, because those are the two
parts of this pipeline that can quietly invent or quietly destroy data.
Each test builds a panel with a hand-placed defect and asserts what the
panel, the per-symbol report, and the event log look like afterwards.

The bar for "flagged" is deliberately high here: a flag that is counted in
the report but not written onto the row is useless to a strategy, and a row
that disappears without an event is invisible, so most tests assert on all
three outputs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from marketengine import flags as F
from marketengine.clean import FILLED_FLAG, FLAGS_COLUMN, clean
from marketengine.config import load_config

CONFIG = """
run_id: test
provider: yfinance
universe:
  tickers: [AAA, BBB]
  benchmark: SPY
window:
  start: "2024-01-01"
  end: "2024-12-31"
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
  extreme_return_vol_units: 8.0
  quality_vol_window: 20
  stale_price_sessions: 3
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
    """Build a minimal raw panel from (symbol, date, price) triples.

    Every bar is a doji — open == high == low == close — which is
    degenerate but internally consistent, so the OHLC check stays silent
    unless a test deliberately breaks it.
    """
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


def events_for(events: pd.DataFrame, symbol: str, flag: str) -> pd.DataFrame:
    return events.loc[(events["symbol"] == symbol) & (events["flag"] == flag)]


# ---------------------------------------------------------------- alignment


def test_benchmark_alignment_drops_dates_the_benchmark_did_not_trade(tmp_path):
    cfg = write_config(tmp_path)
    extra_day = "2024-01-13"  # a Saturday: only AAA "traded"
    raw = panel(full("SPY") + full("AAA") + [("AAA", extra_day, 999.0)] + full("BBB"))

    out, _, _ = clean(cfg, raw)

    assert pd.Timestamp(extra_day) not in set(out["date"])
    assert 999.0 not in set(out["adj_close"])


def test_a_late_listing_starts_at_its_own_first_session(tmp_path):
    cfg = write_config(tmp_path)
    late = [(s, d, p) for s, d, p in full("BBB") if d in BDAYS[5:]]
    raw = panel(full("SPY") + full("AAA") + late)

    out, quality, _ = clean(cfg, raw)
    b = out.loc[out["symbol"] == "BBB"]

    assert b["date"].min() == pd.Timestamp(BDAYS[5])
    # No fabricated pre-listing history, and nothing counted as a gap.
    assert len(b) == 5
    q = quality.set_index("symbol").loc["BBB"]
    assert q["gaps_found"] == 0
    assert q["rows_dropped_missing"] == 0


def test_missing_benchmark_is_a_clear_error_under_benchmark_alignment(tmp_path):
    cfg = write_config(tmp_path)
    raw = panel(full("AAA") + full("BBB"))  # no SPY
    with pytest.raises(ValueError, match="benchmark"):
        clean(cfg, raw)


# ------------------------------------------------------------- gap policy


def test_prices_are_not_forward_filled_by_default(tmp_path):
    """The default is 0, and the default is the thing most likely to ship."""
    cfg = write_config(tmp_path, max_ffill_days=0)
    aaa = [r for r in full("AAA") if r[1] != BDAYS[4]]
    raw = panel(full("SPY") + aaa + full("BBB"))

    out, quality, events = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"]

    assert pd.Timestamp(BDAYS[4]) not in set(a["date"])
    q = quality.set_index("symbol").loc["AAA"]
    assert q["gaps_found"] == 1
    assert q["gaps_forward_filled"] == 0
    assert q["rows_dropped_missing"] == 1
    # The dropped session is not silently gone: it is in the log, named.
    missing = events_for(events, "AAA", F.MISSING_SESSION)
    assert list(missing["date"]) == [pd.Timestamp(BDAYS[4])]
    assert missing.iloc[0]["action"] == "dropped"


def test_a_short_gap_is_forward_filled_and_flagged_when_filling_is_enabled(tmp_path):
    cfg = write_config(tmp_path)  # max_ffill_days: 2
    aaa = [r for r in full("AAA") if r[1] != BDAYS[4]]
    raw = panel(full("SPY") + aaa + full("BBB"))

    out, quality, events = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"].set_index("date")
    gap = pd.Timestamp(BDAYS[4])

    assert gap in a.index
    assert bool(a.loc[gap, FILLED_FLAG]) is True
    assert F.FORWARD_FILLED in a.loc[gap, FLAGS_COLUMN]
    # The carried price is the previous session's, so the return is 0.
    assert a.loc[gap, "adj_close"] == pytest.approx(a.loc[pd.Timestamp(BDAYS[3]), "adj_close"])
    # Volume is NOT carried: a filled bar has unknown volume.
    assert np.isnan(a.loc[gap, "volume"])

    q = quality.set_index("symbol").loc["AAA"]
    assert q["gaps_found"] == 1
    assert q["gaps_forward_filled"] == 1
    assert q["rows_dropped_missing"] == 0
    assert events_for(events, "AAA", F.MISSING_SESSION).iloc[0]["action"] == "filled"


def test_a_gap_longer_than_the_limit_is_dropped_not_filled(tmp_path):
    cfg = write_config(tmp_path)  # max_ffill_days: 2
    missing = {BDAYS[3], BDAYS[4], BDAYS[5], BDAYS[6]}  # four sessions
    aaa = [r for r in full("AAA") if r[1] not in missing]
    raw = panel(full("SPY") + aaa + full("BBB"))

    out, quality, _ = clean(cfg, raw)
    kept = set(out.loc[out["symbol"] == "AAA", "date"])

    # The first two of the gap are within the limit and get carried; the
    # rest stay missing and their rows are dropped rather than invented.
    assert pd.Timestamp(BDAYS[3]) in kept
    assert pd.Timestamp(BDAYS[4]) in kept
    assert pd.Timestamp(BDAYS[5]) not in kept
    assert pd.Timestamp(BDAYS[6]) not in kept

    assert int(quality.set_index("symbol").loc["AAA", "rows_dropped_missing"]) == 2


def test_forward_filling_never_runs_past_a_last_quote(tmp_path):
    """A symbol that stops trading must not acquire a flat tail.

    This is the failure the research report is most emphatic about: a
    forward-filled dead security reports near-zero volatility and a
    beautiful Sharpe ratio, and nothing downstream can tell.
    """
    cfg = write_config(tmp_path)  # filling ENABLED, so this is the real risk
    delisted = [(s, d, p) for s, d, p in full("BBB") if d in BDAYS[:4]]
    raw = panel(full("SPY") + full("AAA") + delisted)

    out, quality, _ = clean(cfg, raw)
    b = out.loc[out["symbol"] == "BBB"]

    assert b["date"].max() == pd.Timestamp(BDAYS[3])
    assert len(b) == 4
    assert int(quality.set_index("symbol").loc["BBB", "gaps_forward_filled"]) == 0


# ----------------------------------------------------------- quarantine


def test_a_nonpositive_price_is_quarantined_and_logged(tmp_path):
    cfg = write_config(tmp_path, max_ffill_days=0)
    aaa = [(s, d, -1.0 if d == BDAYS[4] else p) for s, d, p in full("AAA")]
    raw = panel(full("SPY") + aaa + full("BBB"))

    out, quality, events = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"]

    assert pd.Timestamp(BDAYS[4]) not in set(a["date"])
    assert -1.0 not in set(a["adj_close"])
    q = quality.set_index("symbol").loc["AAA"]
    assert q["rows_quarantined"] == 1
    assert q[f"flag_{F.NONPOSITIVE_PRICE.lower()}"] == 1

    logged = events_for(events, "AAA", F.NONPOSITIVE_PRICE)
    assert len(logged) == 1
    assert logged.iloc[0]["action"] == "quarantined"
    # The event keeps the value, so the vendor can be argued with.
    assert "-1.0" in logged.iloc[0]["detail"]


def test_an_impossible_ohlc_bar_is_quarantined(tmp_path):
    cfg = write_config(tmp_path, max_ffill_days=0)
    raw = panel(full("SPY") + full("AAA") + full("BBB"))
    bad = (raw["symbol"] == "AAA") & (raw["date"] == pd.Timestamp(BDAYS[6]))
    raw.loc[bad, "high"] = 50.0   # a high below its own low and close

    out, quality, events = clean(cfg, raw)

    assert pd.Timestamp(BDAYS[6]) not in set(out.loc[out["symbol"] == "AAA", "date"])
    assert int(quality.set_index("symbol").loc["AAA", "rows_quarantined"]) == 1
    logged = events_for(events, "AAA", F.OHLC_INCONSISTENT)
    assert len(logged) == 1
    assert "h=50" in logged.iloc[0]["detail"]


def test_vendor_rounding_does_not_trip_the_ohlc_check(tmp_path):
    """One part in ten million of slop is rounding, not an impossible bar.

    Without the tolerance this check fires on thousands of ordinary rows,
    and a quality report that cries wolf is a quality report nobody reads.
    """
    cfg = write_config(tmp_path, max_ffill_days=0)
    raw = panel(full("SPY") + full("AAA") + full("BBB"))
    bad = (raw["symbol"] == "AAA") & (raw["date"] == pd.Timestamp(BDAYS[6]))
    raw.loc[bad, "high"] = float(raw.loc[bad, "close"].iloc[0]) - 1e-7

    _, quality, _ = clean(cfg, raw)
    assert int(quality.set_index("symbol").loc["AAA", "rows_quarantined"]) == 0


def test_a_symbol_that_is_entirely_invalid_is_a_loud_error(tmp_path):
    cfg = write_config(tmp_path, max_ffill_days=0)
    raw = panel(full("SPY") + [(s, d, 0.0) for s, d, _ in full("AAA")] + full("BBB"))
    with pytest.raises(ValueError, match="quarantined"):
        clean(cfg, raw)


def test_a_duplicate_date_keeps_the_later_record_and_logs_it(tmp_path):
    """A vendor restatement arrives after the thing it restates."""
    cfg = write_config(tmp_path, max_ffill_days=0)
    aaa = full("AAA")
    restated = aaa + [("AAA", BDAYS[4], 777.0)]  # appended => later record
    raw = panel(full("SPY") + restated + full("BBB"))

    out, quality, events = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"].set_index("date")

    assert a.loc[pd.Timestamp(BDAYS[4]), "adj_close"] == 777.0
    assert int(quality.set_index("symbol").loc["AAA", f"flag_{F.DUPLICATE_DATE.lower()}"]) == 1
    assert len(events_for(events, "AAA", F.DUPLICATE_DATE)) == 1


# ------------------------------------------------------- flag-not-delete


def test_an_extreme_move_is_flagged_on_the_row_and_kept(tmp_path):
    cfg = write_config(tmp_path, max_ffill_days=0)
    prices = [100.0, 101.0, 100.5, 101.5, 100.0, 200.0, 201.0, 200.5, 201.5, 200.0]
    raw = panel(full("SPY") + full("AAA", prices) + full("BBB"))

    out, quality, events = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"].set_index("date")
    jump = pd.Timestamp(BDAYS[5])

    assert a.loc[jump, "adj_close"] == 200.0             # kept
    assert F.EXTREME_RETURN in a.loc[jump, FLAGS_COLUMN]  # and named
    assert int(quality.set_index("symbol").loc["AAA", f"flag_{F.EXTREME_RETURN.lower()}"]) == 1
    assert events_for(events, "AAA", F.EXTREME_RETURN).iloc[0]["action"] == "kept"


def test_a_quiet_series_flags_a_move_the_absolute_threshold_would_miss(tmp_path):
    """The volatility-relative arm of the extreme check.

    A bond-ETF-shaped series moving 0.1% a day, then 5% in one session: far
    under the 35% absolute threshold, and about 50 trailing sigmas out. A
    fixed threshold alone would never see it, which is exactly how a
    decimal-point error survives into a backtest.
    """
    cfg = write_config(tmp_path, max_ffill_days=0)
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=40)]
    quiet = [100.0 + 0.1 * (i % 2) for i in range(len(days))]
    quiet[30] = quiet[29] * 1.05
    rows = ([("SPY", d, 100.0) for d in days]
            + [("AAA", d, p) for d, p in zip(days, quiet)]
            + [("BBB", d, 100.0) for d in days])

    out, quality, _ = clean(cfg, panel(rows))
    a = out.loc[out["symbol"] == "AAA"].set_index("date")

    assert F.EXTREME_RETURN in a.loc[pd.Timestamp(days[30]), FLAGS_COLUMN]
    # And the absolute threshold on its own would have found nothing.
    assert int(quality.set_index("symbol").loc["AAA", f"flag_{F.EXTREME_RETURN.lower()}"]) >= 1


def test_a_stale_price_run_is_flagged(tmp_path):
    cfg = write_config(tmp_path, max_ffill_days=0)
    prices = [100.0, 101.0, 102.0, 102.0, 102.0, 102.0, 103.0, 104.0, 105.0, 106.0]
    raw = panel(full("SPY") + full("AAA", prices) + full("BBB"))

    out, quality, events = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"].set_index("date")

    # Four identical closes: the session that set the price and its three
    # repeats. All four are flagged — "these sessions are one price" is the
    # fact worth seeing.
    flagged = [d for d in a.index if F.STALE_PRICE in a.loc[d, FLAGS_COLUMN]]
    assert flagged == [pd.Timestamp(BDAYS[i]) for i in (2, 3, 4, 5)]
    assert int(quality.set_index("symbol").loc["AAA", f"flag_{F.STALE_PRICE.lower()}"]) == 4
    assert len(events_for(events, "AAA", F.STALE_PRICE)) == 4


def test_two_identical_closes_are_not_a_stale_run(tmp_path):
    """`stale_price_sessions: 3`. Two in a row is a Tuesday."""
    cfg = write_config(tmp_path, max_ffill_days=0)
    prices = [100.0, 101.0, 102.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0]
    raw = panel(full("SPY") + full("AAA", prices) + full("BBB"))

    _, quality, _ = clean(cfg, raw)
    assert int(quality.set_index("symbol").loc["AAA", f"flag_{F.STALE_PRICE.lower()}"]) == 0


def test_a_zero_volume_session_is_flagged_and_kept(tmp_path):
    cfg = write_config(tmp_path, max_ffill_days=0)
    raw = panel(full("SPY") + full("AAA") + full("BBB"))
    halt = (raw["symbol"] == "AAA") & (raw["date"] == pd.Timestamp(BDAYS[7]))
    raw.loc[halt, "volume"] = 0.0

    out, quality, _ = clean(cfg, raw)
    a = out.loc[out["symbol"] == "AAA"].set_index("date")

    assert pd.Timestamp(BDAYS[7]) in a.index
    assert F.ZERO_VOLUME in a.loc[pd.Timestamp(BDAYS[7]), FLAGS_COLUMN]
    assert int(quality.set_index("symbol").loc["AAA", f"flag_{F.ZERO_VOLUME.lower()}"]) == 1


def test_a_clean_row_carries_an_empty_flag_string(tmp_path):
    """Not NaN, not "NONE": empty, so `if row.flags` reads correctly."""
    cfg = write_config(tmp_path, max_ffill_days=0)
    raw = panel(full("SPY") + full("AAA") + full("BBB"))
    out, _, events = clean(cfg, raw)

    assert set(out[FLAGS_COLUMN]) == {""}
    assert events.empty
    assert list(events.columns) == ["date", "symbol", "flag", "action", "detail"]


def test_short_history_is_flagged_not_removed(tmp_path):
    cfg = write_config(tmp_path)  # min_history_days: 3
    tiny = [(s, d, p) for s, d, p in full("BBB") if d in BDAYS[8:]]  # 2 sessions
    raw = panel(full("SPY") + full("AAA") + tiny)

    out, quality, _ = clean(cfg, raw)
    assert "BBB" in set(out["symbol"])
    assert bool(quality.set_index("symbol").loc["BBB", "short_history"]) is True


def test_unadjusted_data_is_flagged(tmp_path):
    cfg = write_config(tmp_path)
    raw = panel(full("SPY") + full("AAA") + full("BBB"))  # adj_close == close
    _, quality, _ = clean(cfg, raw)
    assert bool(quality.set_index("symbol").loc["AAA", "adj_close_equals_close"]) is True


# ------------------------------------------------------------ hard errors


def test_clean_refuses_an_empty_panel(tmp_path):
    cfg = write_config(tmp_path)
    with pytest.raises(ValueError, match="empty"):
        clean(cfg, panel([]))


def test_clean_refuses_a_window_with_no_rows_in_it(tmp_path):
    cfg = write_config(tmp_path)
    raw = panel([("SPY", "2019-06-03", 100.0), ("AAA", "2019-06-03", 50.0)])
    with pytest.raises(ValueError, match="window"):
        clean(cfg, raw)
