"""
Cleaning, alignment, and the data-quality outputs.

Three things come out of this module, deliberately separated:

  * the **curated panel** — the analytical table, one row per
    (date, symbol), with a `flags` column naming everything suspicious
    about each row;
  * the **per-symbol quality report** — one row per symbol, counting each
    flag, for a human to scan;
  * the **event log** — one row per (date, symbol, flag), with the values
    that triggered it, for a human to investigate.

`data/raw/` is never modified, so the immutable vendor response remains
available to re-derive all three. That is the whole reason cleaning is a
separate stage rather than something `ingest` does on the way in.

The assumptions below change every number the rest of the course
produces, so they are stated here, in the README, and in the generated
report rather than left implicit in the code:

  1. **Returns are computed from `adj_close`, not `close`.** `adj_close`
     is adjusted for splits *and* dividends, so it is a total-return
     series. Using `close` would show a dividend payment as a price drop
     and understate the return of every dividend payer — for a 10-year
     window on something like XOM or JNJ that is not a rounding error.
     `close` is kept in the panel because it is the price you could
     actually have transacted at, which matters as soon as there are
     transaction costs to model.
  2. **The trading calendar is taken from the data, not from a holiday
     library.** With `align: benchmark` the benchmark's own sessions
     define what a trading day is. This is exactly right for relative
     metrics (a date the benchmark did not trade has no benchmark return
     to be relative to) and it avoids depending on a market-calendar
     package being correct about 2015.
  3. **Prices are not forward-filled by default.** `calendar.
     max_ffill_days: 0`. A carried price produces a fabricated 0% return
     on the fill day and a fabricated real return on the day after, and
     for a short holding period — the one this project is aimed at — that
     is a directly tradeable-looking artefact rather than a rounding
     error. Worse, forward-filling past a security's last real quote
     manufactures a flat, zero-volatility series for something that no
     longer trades, which every risk statistic then rewards. Sessions
     with no bar are flagged `MISSING_SESSION` and dropped from the
     panel, so a return computed across the hole is honestly a two-day
     return rather than a made-up pair of one-day returns. The
     forward-fill mechanism is kept, and filled rows are flagged
     `FORWARD_FILLED`, for anyone who needs a gap-free matrix and accepts
     the cost.
  4. **Nothing is deleted for being surprising.** Extreme daily moves,
     zero-volume sessions and stale repeated prices are flagged and
     counted, never removed. A 45% single-day move is usually real, and
     dropping it is the actual data error.
  5. **Structural impossibilities are quarantined, not repaired.** A
     non-positive price, or a bar whose high is below its own low or its
     own close, cannot be interpreted — there is no defensible guess at
     what the real number was. Those rows leave the analytical table and
     land in the event log with their values intact. They are then just
     missing sessions, and rule 3 applies.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import flags as F
from .config import Config
from .providers.base import PRICE_COLUMNS

# Filled rows keep a dedicated boolean as well as the FORWARD_FILLED flag.
# The flag is for reading, the boolean is for filtering in a `where` clause
# without a string comparison, and `filled` was in the schema before flags
# existed.
FILLED_FLAG = "filled"

# Name of the string column carrying the comma-joined flag names.
FLAGS_COLUMN = "flags"

#: Relative tolerance for the OHLC consistency check. Vendors round prices
#: to four decimals and occasionally disagree with themselves in the last
#: digit; a strict `high < close` comparison turns that into thousands of
#: false positives, which is how a quality report gets ignored. One part in
#: a million of the bar's own price is far below any real inconsistency.
OHLC_TOLERANCE = 1e-6


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


def _ohlc_inconsistent(g: pd.DataFrame) -> pd.Series:
    """Rows where the four prices cannot all be true at once.

    A daily bar asserts `low <= open, close <= high`. Any violation means
    at least one of the four numbers is wrong and there is no way to tell
    which, so the row is unusable rather than merely odd. Checked on
    as-traded prices, since `adj_close` is scaled by a different factor and
    is not comparable to the untouched high and low.
    """
    o, h, l, c = (g[k].astype(float) for k in ("open", "high", "low", "close"))
    # Scale the tolerance by the bar's own price: a penny of slop is
    # nothing on a $900 stock and enormous on a $0.30 one.
    tol = OHLC_TOLERANCE * h.abs().fillna(0.0).clip(lower=1e-9)
    bad = (
        (h + tol < l)
        | (h + tol < o) | (h + tol < c)
        | (l - tol > o) | (l - tol > c)
    )
    # A row with a missing price is not inconsistent, it is incomplete —
    # the NaN comparisons above already return False, this just makes the
    # intent explicit rather than incidental.
    return bad.fillna(False)


def _extreme_returns(adj: pd.Series, cfg: Config) -> pd.Series:
    """Returns past an absolute threshold, or past N trailing sigmas.

    Two tests because one is not enough. A fixed threshold is the only
    thing that catches a decimal-point error on a quiet bond ETF, whose
    "8 sigma" is a 3% move it makes anyway. A volatility-relative test is
    the only thing that catches a bad print on a name that routinely moves
    15%, where a fixed 50% threshold never fires.

    The sigma is measured on a window that **ends before** the bar being
    tested (`shift(1)`), so a genuine outlier does not inflate the
    yardstick it is being measured against and hide itself. That is also
    the only version of this check that a strategy could run in real time.
    """
    rets = adj.pct_change()
    absolute = rets.abs() > cfg.analytics.extreme_return_threshold

    units = cfg.analytics.extreme_return_vol_units
    window = cfg.analytics.quality_vol_window
    sigma = rets.rolling(window, min_periods=max(window // 2, 5)).std().shift(1)
    relative = rets.abs() > (units * sigma)

    return (absolute | relative.fillna(False)).fillna(False)


def _stale_runs(adj: pd.Series, observed: pd.Series, min_run: int) -> pd.Series:
    """Rows inside a run of `min_run`+ consecutive identical observed closes.

    Every row of the run is flagged, not just the repeats, because "these
    five sessions are one price" is the fact worth seeing in the log.
    """
    same_as_prev = (adj.diff() == 0) & observed
    # Run length so far, reset by any change. `cumsum` of the negation gives
    # a group id per run; the size of that group is the run length.
    # Every row that did NOT repeat its predecessor opens a new group, so a
    # run of identical prices shares one group id and its length is the
    # number of repeats inside it plus the row that started it.
    group = (~same_as_prev).cumsum()
    run_len = same_as_prev.groupby(group).transform("sum") + 1
    return (run_len >= min_run).fillna(False)


def clean(cfg: Config, raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Quarantine, align, flag.

    Returns `(curated_panel, quality_report, event_log)`.
    """
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
    price_cols = list(PRICE_COLUMNS)

    cleaned: list[pd.DataFrame] = []
    quality: list[dict] = []
    events: list[pd.DataFrame] = []

    def log(sym: str, dates, flag: str, action: str, detail) -> None:
        """Append event rows. `detail` carries the values that triggered it."""
        dates = pd.DatetimeIndex(dates)
        if len(dates) == 0:
            return
        events.append(pd.DataFrame({
            "date": dates,
            "symbol": sym,
            "flag": flag,
            "action": action,
            "detail": list(detail) if not isinstance(detail, str) else detail,
        }))

    for sym, group in panel.groupby("symbol", observed=True, sort=True):
        raw_rows = len(group)
        g = group.sort_values("date", kind="mergesort")

        # ---- duplicates ---------------------------------------------
        # Keep the LAST record for a date: a vendor restatement arrives
        # after the thing it restates. Logged because a vendor that starts
        # duplicating dates is a vendor worth distrusting.
        dup = g["date"].duplicated(keep="last")
        if dup.any():
            log(sym, g.loc[dup, "date"], F.DUPLICATE_DATE, "superseded",
                [f"kept later record for {d.date()}" for d in g.loc[dup, "date"]])
        g = g.loc[~dup].set_index("date")

        # ---- quarantine: structural impossibilities ------------------
        nonpositive = (g[price_cols] <= 0).any(axis=1).fillna(False)
        inconsistent = _ohlc_inconsistent(g)
        if nonpositive.any():
            log(sym, g.index[nonpositive], F.NONPOSITIVE_PRICE, "quarantined",
                [f"close={c!r} low={lo!r}" for c, lo in
                 zip(g.loc[nonpositive, "close"], g.loc[nonpositive, "low"])])
        if inconsistent.any():
            sub = g.loc[inconsistent]
            log(sym, sub.index, F.OHLC_INCONSISTENT, "quarantined",
                [f"o={o:g} h={h:g} l={lo:g} c={c:g}" for o, h, lo, c in
                 zip(sub["open"], sub["high"], sub["low"], sub["close"])])
        quarantined = int((nonpositive | inconsistent).sum())
        g = g.loc[~(nonpositive | inconsistent)]
        if g.empty:
            raise ValueError(
                f"{sym}: every bar in the window was quarantined as structurally "
                "invalid (non-positive prices or impossible OHLC). The vendor "
                "response is unusable; inspect data/raw/ before continuing."
            )

        observed_sessions = len(g)
        first_obs, last_obs = g.index.min(), g.index.max()

        # ---- align --------------------------------------------------
        # Only the part of the calendar this symbol could plausibly cover.
        # Reindexing across the whole calendar would manufacture rows
        # before a late listing and after a delisting, which are then
        # dropped anyway — doing it on the symbol's own span keeps the
        # "expected sessions" count in the report honest, and it is what
        # stops a forward fill from ever running past a last quote.
        span = calendar[(calendar >= first_obs) & (calendar <= last_obs)]
        g = g.reindex(span)

        missing = g["adj_close"].isna()
        gaps_found = int(missing.sum())

        # limit=max_fill so a gap longer than the limit stays NaN and the
        # rows are dropped below. limit=0 is a TypeError in pandas, so a
        # configured 0 means "do not fill at all" — which is the default.
        if max_fill > 0:
            g[price_cols] = g[price_cols].ffill(limit=max_fill)

        # Volume is deliberately NOT forward-filled. Carrying a volume
        # would invent trading activity on a day with none; a filled bar
        # has an unknown volume, and NaN is what unknown means.
        still_missing = g["adj_close"].isna()
        dropped = int(still_missing.sum())
        filled_mask = (missing & ~still_missing)

        if gaps_found:
            log(sym, g.index[missing], F.MISSING_SESSION,
                "filled" if max_fill > 0 else "dropped",
                f"no vendor bar; max_ffill_days={max_fill}")

        g = g.loc[~still_missing].copy()
        g[FILLED_FLAG] = filled_mask.reindex(g.index).fillna(False).to_numpy()

        # ---- flags that do not change the data ----------------------
        adj = g["adj_close"]
        observed = ~g[FILLED_FLAG]
        extreme = _extreme_returns(adj, cfg)
        stale = _stale_runs(adj, observed, cfg.analytics.stale_price_sessions)
        zero_vol = ((g["volume"] == 0) & observed).fillna(False)

        # MISSING_SESSION is deliberately absent here: by construction every
        # surviving row either had a real bar or was forward-filled, so on the
        # panel the flag would be an exact alias of FORWARD_FILLED. It lives
        # in the event log and the per-symbol counts, where dropped sessions
        # still exist to be counted.
        marks = pd.DataFrame({
            F.FORWARD_FILLED: g[FILLED_FLAG],
            F.EXTREME_RETURN: extreme,
            F.STALE_PRICE: stale,
            F.ZERO_VOLUME: zero_vol,
        }, index=g.index)
        g[FLAGS_COLUMN] = F.join(marks, F.ALL_FLAGS).to_numpy()

        rets = adj.pct_change()
        if extreme.any():
            log(sym, g.index[extreme], F.EXTREME_RETURN, "kept",
                [f"return={r:+.2%}" for r in rets.loc[extreme]])
        if stale.any():
            log(sym, g.index[stale], F.STALE_PRICE, "kept",
                [f"adj_close={p:g} unchanged" for p in adj.loc[stale]])
        if zero_vol.any():
            log(sym, g.index[zero_vol], F.ZERO_VOLUME, "kept", "volume=0 on an observed bar")

        g["symbol"] = sym
        g.index.name = "date"
        cleaned.append(g.reset_index())

        no_adjustment = bool(np.allclose(
            adj.to_numpy(dtype=float),
            g["close"].to_numpy(dtype=float),
            equal_nan=True,
        ))

        row = {
            "symbol": sym,
            "role": "benchmark" if sym == cfg.universe.benchmark else "universe",
            "first_date": g.index.min().date().isoformat(),
            "last_date": g.index.max().date().isoformat(),
            "rows": int(len(g)),
            "raw_rows_in_window": raw_rows,
            "observed_bars": observed_sessions,
            "expected_sessions_in_span": int(len(span)),
            "coverage_pct": round(100.0 * observed_sessions / max(len(span), 1), 2),
            "rows_quarantined": quarantined,
            "gaps_found": gaps_found,
            "gaps_forward_filled": int(g[FILLED_FLAG].sum()),
            "rows_dropped_missing": dropped,
        }
        # One count per flag, always present even at zero, so the table has
        # the same columns on every run and two runs can be diffed.
        flag_counts = {
            F.MISSING_SESSION: gaps_found,
            F.FORWARD_FILLED: int(g[FILLED_FLAG].sum()),
            F.DUPLICATE_DATE: int(dup.sum()),
            F.NONPOSITIVE_PRICE: int(nonpositive.sum()),
            F.OHLC_INCONSISTENT: int(inconsistent.sum()),
            F.EXTREME_RETURN: int(extreme.sum()),
            F.STALE_PRICE: int(stale.sum()),
            F.ZERO_VOLUME: int(zero_vol.sum()),
        }
        row.update({f"flag_{name.lower()}": flag_counts[name] for name in F.ALL_FLAGS})
        row["adj_close_equals_close"] = no_adjustment
        row["short_history"] = bool(len(g) < cfg.analytics.min_history_days)
        quality.append(row)

    out = (pd.concat(cleaned, ignore_index=True)
             .sort_values(["symbol", "date"], kind="mergesort")
             .reset_index(drop=True))
    report = pd.DataFrame.from_records(quality).sort_values(
        ["role", "symbol"], ascending=[False, True]
    ).reset_index(drop=True)

    if events:
        log_df = (pd.concat(events, ignore_index=True)
                    .sort_values(["date", "symbol", "flag"], kind="mergesort")
                    .reset_index(drop=True))
    else:
        log_df = pd.DataFrame(columns=["date", "symbol", "flag", "action", "detail"])
    return out, report, log_df
