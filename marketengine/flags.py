"""
Named data-quality flags.

The rule this module exists to enforce is **flag, do not delete**. A bar
that looks wrong is usually right: a 45% single-day move is an earnings
gap, a zero-volume session is a halt, a repeated close is a thin ETF.
Deleting those rows is how a backtest quietly starts outperforming — the
hard days go missing and nothing in the output says so.

So every suspicion becomes a *name* attached to the row, and the row
stays. Downstream code can exclude `EXTREME_RETURN` rows if it wants to;
it cannot accidentally not know they were there.

The two exceptions are structural impossibilities — a non-positive price,
or a high below its own low — which cannot be interpreted at all and are
quarantined out of the analytical table. Even then they are not thrown
away: they land in the event log with the values that triggered them.

Flags live on the curated panel as a comma-joined string in one `flags`
column rather than as a bitmask or one column per flag. A bitmask means
`flags & 4` in every later query and a lookup table to read a CSV; a
column per flag means the schema changes whenever a check is added.
`"MISSING_SESSION,FORWARD_FILLED"` is greppable, survives a round trip
through Parquet and CSV unchanged, and reads correctly to a human looking
at the file in a terminal, which is most of what a quality log is for.
"""

from __future__ import annotations

import pandas as pd

#: The row is not in the vendor's response at all — the symbol traded on
#: either side of it, so the session is missing rather than nonexistent.
MISSING_SESSION = "MISSING_SESSION"

#: Prices on this row were carried forward from an earlier session. Only
#: appears when `calendar.max_ffill_days > 0`, which is not the default.
FORWARD_FILLED = "FORWARD_FILLED"

#: The vendor returned the same date twice; the later record was kept, on
#: the assumption that a restatement arrives after the thing it restates.
DUPLICATE_DATE = "DUPLICATE_DATE"

#: A price <= 0. Structurally impossible. Quarantined.
NONPOSITIVE_PRICE = "NONPOSITIVE_PRICE"

#: `high < low`, or a high below the open/close, or a low above them.
#: Structurally impossible. Quarantined.
OHLC_INCONSISTENT = "OHLC_INCONSISTENT"

#: |daily return| past the absolute threshold, or past N rolling standard
#: deviations measured on data that ended *before* this bar. Kept.
EXTREME_RETURN = "EXTREME_RETURN"

#: N or more consecutive observed sessions at an identical adjusted close.
#: A stale vendor feed looks exactly like a market with no trades. Kept.
STALE_PRICE = "STALE_PRICE"

#: An observed bar reporting zero shares traded. Kept.
ZERO_VOLUME = "ZERO_VOLUME"

#: Flags that remove a row from the analytical table. Nothing else does.
QUARANTINE_FLAGS = (NONPOSITIVE_PRICE, OHLC_INCONSISTENT)

#: Every flag, in the order they are reported. Used to build the columns
#: of the per-symbol quality table so it has a stable shape even when a
#: given run happens to trigger none of them.
ALL_FLAGS = (
    MISSING_SESSION,
    FORWARD_FILLED,
    DUPLICATE_DATE,
    NONPOSITIVE_PRICE,
    OHLC_INCONSISTENT,
    EXTREME_RETURN,
    STALE_PRICE,
    ZERO_VOLUME,
)

SEPARATOR = ","


def join(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    """Collapse boolean flag columns into one comma-joined string column.

    `frame` holds one boolean column per flag name. Rows with nothing wrong
    get `""`, not `"NONE"` or NaN — an empty string is falsy, filters
    cleanly, and does not need documenting.
    """
    acc = pd.Series("", index=frame.index, dtype="string")
    for name in names:
        if name not in frame.columns:
            continue
        mask = frame[name].fillna(False).astype(bool)
        acc = acc + mask.map({True: SEPARATOR + name, False: ""}).astype("string")
    # Every name was prefixed with the separator so the concatenation above
    # needs no special case for "is this the first flag"; one lstrip at the
    # end is cheaper than a per-row branch.
    return acc.str.lstrip(SEPARATOR)


def has(flags: pd.Series, name: str) -> pd.Series:
    """Boolean mask: does each row carry `name`?

    Matches on the whole token, so `STALE_PRICE` does not match
    `NOT_STALE_PRICE` if such a flag is ever added.
    """
    padded = SEPARATOR + flags.fillna("").astype(str) + SEPARATOR
    return padded.str.contains(SEPARATOR + name + SEPARATOR, regex=False)
