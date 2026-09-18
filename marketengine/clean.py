"""
Cleaning, alignment, and the data-quality report.

The assumptions in this file change every number the rest of the course
produces, so they are stated here, in the README, and in the generated
report rather than left implicit in the code:

  1. **Returns are computed from `adj_close`, not `close`.** `adj_close`
     is adjusted for splits *and* dividends, so it is a total-return
     series. Using `close` would show a dividend payment as a price drop
     and understate the return of every dividend payer — for a 10-year
     window on something like XOM or JNJ that is not a rounding error.
     `close` is kept in the panel because it is the price you could
     actually have transacted at, which matters for Milestone 3's
     transaction costs.
  2. **The trading calendar is taken from the data, not from a holiday
     library.** With `align: benchmark` the benchmark's own sessions
     define what a trading day is. This is exactly right for relative
     metrics (a date the benchmark did not trade has no benchmark return
     to be relative to) and it avoids depending on a market-calendar
     package being correct about 2015.
  3. **Gaps are forward-filled for at most `calendar.max_ffill_days`
     sessions, and every filled bar is flagged.** A short gap is a
     missing vendor bar or a halt; carrying the last price is the standard
     treatment and it produces a 0% return for that day. A long gap is
     something else — a listing that starts late, a delisting, a symbol
     the vendor does not cover — and filling it would fabricate a long
     flat stretch that reads as zero volatility. Those rows are dropped
     instead, so each symbol's series begins at its own first real
     session.
  4. **Nothing is deleted for being surprising.** Extreme daily moves,
     zero-volume sessions, and stale repeated prices are counted and
     reported, never silently removed. A 40% single-day move is usually
     real, and dropping it would be the actual data error.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config
from .providers.base import PRICE_COLUMNS

# Filled rows are marked with this column so downstream code (and a
# grader) can always tell an observed bar from a carried one.
FILLED_FLAG = "filled"


def _target_calendar(panel: pd.DataFrame, cfg: Config) -> pd.DatetimeIndex:
    """The set of dates the curated panel will be indexed on."""
    mode = cfg.calendar.align
    by_symbol = {sym: set(g["date"]) for sym, g in panel.groupby("symbol", observed=True)}

    if mode == "benchmark":
        bench = cfg.universe.benchmark
        if bench not in by_symbol or not by_symbol[bench]:
            raise ValueError(
                f"calendar.align is 'benchmark' but no data was stored for {bench}. "
                "Either fix the benchmark symbol or set calendar.align to 'union'."
            )
        dates = by_symbol[bench]
    elif mode == "intersect":
        dates = set.intersection(*by_symbol.values()) if by_symbol else set()
        if not dates:
            raise ValueError(
                "calendar.align is 'intersect' but no single date is common to every "
                "symbol — usually one symbol listed later than the window start. "
                "Use 'benchmark' or shorten window.start."
            )
    else:  # union
        dates = set.union(*by_symbol.values()) if by_symbol else set()

    return pd.DatetimeIndex(sorted(dates)).sort_values()


def clean(cfg: Config, raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align and gap-fill `raw`; return `(curated_panel, quality_report)`."""
    if raw.empty:
        raise ValueError("nothing to clean: the raw panel is empty. Run ingest first.")

    start = pd.Timestamp(cfg.window.start)
    end = pd.Timestamp(cfg.window.end) if cfg.window.end else None
    window = raw["date"] >= start
    if end is not None:
        window &= raw["date"] <= end
    panel = raw.loc[window].copy()
    if panel.empty:
        raise ValueError(
            f"no stored rows fall inside window {cfg.window.start} .. {cfg.window.end}. "
            "Check the window against the ingest report."
        )

    calendar = _target_calendar(panel, cfg)
    max_fill = cfg.calendar.max_ffill_days

    cleaned: list[pd.DataFrame] = []
    quality: list[dict] = []

    for sym, group in panel.groupby("symbol", observed=True, sort=True):
        g = (group.drop_duplicates(subset=["date"], keep="last")
                  .set_index("date")
                  .sort_index())
        observed_sessions = len(g)
        first_obs, last_obs = g.index.min(), g.index.max()

        # Only the part of the calendar this symbol could plausibly cover.
        # Reindexing across the whole calendar would manufacture rows
        # before a late listing and after a delisting, which are then
        # dropped anyway — doing it on the symbol's own span keeps the
        # "expected sessions" count in the report honest.
        span = calendar[(calendar >= first_obs) & (calendar <= last_obs)]
        g = g.reindex(span)

        missing_before_fill = int(g["adj_close"].isna().sum())
        filled = g["adj_close"].isna()

        # limit=max_fill so a gap longer than the limit stays NaN and the
        # rows are dropped below. limit=0 would be a TypeError in pandas,
        # so a configured 0 means "do not fill at all".
        if max_fill > 0:
            g[list(PRICE_COLUMNS)] = g[list(PRICE_COLUMNS)].ffill(limit=max_fill)

        # Volume is deliberately NOT forward-filled. Carrying a volume
        # would invent trading activity on a day with none; a filled bar
        # has an unknown volume, and NaN is what unknown means.
        still_missing = g["adj_close"].isna()
        dropped_long_gap = int(still_missing.sum())
        g = g.loc[~still_missing].copy()
        g[FILLED_FLAG] = filled.reindex(g.index).fillna(False).to_numpy()

        g["symbol"] = sym
        g.index.name = "date"
        g = g.reset_index()
        cleaned.append(g)

        # ---- quality signals (counted, never acted on) --------------
        adj = g["adj_close"]
        rets = adj.pct_change()
        nonpositive = int((g[list(PRICE_COLUMNS)] <= 0).any(axis=1).sum())
        zero_volume = int(((g["volume"] == 0) & ~g[FILLED_FLAG]).sum())
        extreme = int((rets.abs() > cfg.analytics.extreme_return_threshold).sum())
        # Three or more consecutive identical closes on observed bars —
        # a signature of a stale vendor feed rather than a real market.
        same_as_prev = (adj.diff() == 0) & ~g[FILLED_FLAG]
        stale_runs = int((same_as_prev & same_as_prev.shift(fill_value=False)).sum())
        no_adjustment = bool(np.allclose(
            g["adj_close"].to_numpy(dtype=float),
            g["close"].to_numpy(dtype=float),
            equal_nan=True,
        ))

        quality.append({
            "symbol": sym,
            "role": "benchmark" if sym == cfg.universe.benchmark else "universe",
            "first_date": g["date"].min().date().isoformat(),
            "last_date": g["date"].max().date().isoformat(),
            "rows": int(len(g)),
            "observed_bars": observed_sessions,
            "expected_sessions_in_span": int(len(span)),
            "gaps_found": missing_before_fill,
            "gaps_forward_filled": int(g[FILLED_FLAG].sum()),
            "rows_dropped_long_gap": dropped_long_gap,
            "coverage_pct": round(100.0 * observed_sessions / max(len(span), 1), 2),
            "nonpositive_price_rows": nonpositive,
            "zero_volume_sessions": zero_volume,
            f"abs_return_gt_{cfg.analytics.extreme_return_threshold:g}": extreme,
            "stale_price_runs": stale_runs,
            "adj_close_equals_close": no_adjustment,
            "short_history": bool(len(g) < cfg.analytics.min_history_days),
        })

    out = (pd.concat(cleaned, ignore_index=True)
             .sort_values(["symbol", "date"], kind="mergesort")
             .reset_index(drop=True))
    report = pd.DataFrame.from_records(quality).sort_values(
        ["role", "symbol"], ascending=[False, True]
    ).reset_index(drop=True)
    return out, report
