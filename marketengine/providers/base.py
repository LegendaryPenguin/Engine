"""
The provider contract.

Everything downstream of ingestion — cleaning, analytics, plotting —
depends on exactly one thing: a long-format DataFrame with the columns in
`SCHEMA`. That is the whole reason a provider layer exists. Swapping
`provider: yfinance` for `provider: alpaca` in the config changes which
file does the HTTP work and changes nothing else in the package.

Two rules make that hold in practice rather than in principle:

  1. A provider is responsible for translating its vendor's shape into
     `SCHEMA`, including column names, index, dtypes, and timezone. No
     downstream module is allowed to special-case a provider.
  2. `normalise()` below is called on the way out of every provider and
     raises on anything that does not conform, so a vendor changing its
     response shape fails loudly at the boundary instead of quietly
     producing a column of NaN that only shows up in a chart.
"""

from __future__ import annotations

import abc
from datetime import date

import pandas as pd

# The canonical long-format panel. One row per (date, symbol).
#
# Long rather than wide on purpose: a wide frame with a MultiIndex column
# per (field, symbol) is what yfinance hands back, and it is miserable to
# store, filter, and union across providers. Long is one Parquet file, one
# schema, and it pivots to wide in a single call where wide is actually
# what the maths wants.
SCHEMA: dict[str, str] = {
    "date": "datetime64[ns]",
    "symbol": "string",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",       # as-traded, unadjusted
    "adj_close": "float64",   # adjusted for splits AND dividends -> total return
    "volume": "float64",      # float, not int: a missing bar is NaN, not 0
}

COLUMNS: tuple[str, ...] = tuple(SCHEMA)
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "adj_close")

#: Nanoseconds, spelled out. pandas 3 infers microsecond resolution from
#: strings and Parquet preserves whatever unit it was given, so without an
#: explicit cast the same column arrives as `datetime64[us]` from disk and
#: `datetime64[ns]` from a literal. Comparisons still work across units,
#: but dtype assertions and `concat` of mixed units do not, so every entry
#: and exit point pins one unit.
DATE_DTYPE = "datetime64[ns]"


def as_dates(values) -> pd.Series:
    """Coerce to tz-naive, midnight-normalised `DATE_DTYPE`."""
    parsed = pd.to_datetime(values, errors="coerce", utc=True)
    return parsed.dt.tz_localize(None).dt.normalize().astype(DATE_DTYPE)


class ProviderError(RuntimeError):
    """A provider could not return usable data.

    Distinct from a bug: this is the vendor being down, the symbol not
    existing, or credentials missing. The CLI prints it without a
    traceback, because a traceback implies the code is wrong.
    """


class PriceProvider(abc.ABC):
    """A source of daily OHLCV bars."""

    #: Matches the `provider:` value in the config.
    name: str = "abstract"

    @abc.abstractmethod
    def fetch(self, symbols: list[str], start: date, end: date | None) -> pd.DataFrame:
        """Return daily bars for `symbols` in `SCHEMA`, inclusive of both ends.

        `end=None` means "through the most recent available close".
        Implementations must return every symbol they successfully
        retrieved and raise `ProviderError` only if they retrieved
        nothing at all — one delisted ticker in a list of ten should not
        take down the run. Partial success is reported by the caller
        comparing the symbols it asked for against the symbols it got.
        """

    def describe(self) -> dict[str, str]:
        """Provenance for the run manifest.

        Vendor identity and adjustment policy belong in the manifest
        because they change the numbers: an unadjusted close produces a
        different return series than a total-return one, and six weeks
        later that is impossible to reconstruct from the Parquet alone.
        """
        return {"provider": self.name}


def empty_panel() -> pd.DataFrame:
    """A correctly-typed zero-row panel.

    Returning this instead of `pd.DataFrame()` keeps the "no data" path
    on the same code path as the normal one — `pd.concat` of an empty
    untyped frame silently widens dtypes to `object`.
    """
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in SCHEMA.items()})


def normalise(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """Coerce a provider's frame to `SCHEMA` and refuse anything that cannot be.

    Called by every provider on the way out. `source` only appears in
    error messages, and it is worth passing: "alpaca: missing column
    adj_close" is a one-line diagnosis.
    """
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ProviderError(f"{source}: provider frame is missing column(s) {missing}")

    out = df.loc[:, list(COLUMNS)].copy()

    # Timezone is dropped deliberately. A daily bar is a label for a
    # session, not an instant, and two providers stamping the same
    # session 00:00 UTC and 04:00 UTC would refuse to join.
    out["date"] = as_dates(out["date"])

    out["symbol"] = out["symbol"].astype("string").str.strip().str.upper()

    for col in (*PRICE_COLUMNS, "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")

    bad_date = out["date"].isna()
    bad_symbol = out["symbol"].isna() | (out["symbol"] == "")
    if bad_date.any() or bad_symbol.any():
        n = int((bad_date | bad_symbol).sum())
        out = out.loc[~(bad_date | bad_symbol)]
        # Not fatal: a single unparseable row is a vendor artefact, and
        # the count lands in the data-quality report either way.
        print(f"  ! {source}: dropped {n} row(s) with an unusable date or symbol")

    # A bar with no close is not a bar. Keeping it would put a NaN in the
    # middle of a price series and make every rolling window downstream
    # produce NaN for the length of the window.
    no_price = out["close"].isna() & out["adj_close"].isna()
    if no_price.any():
        out = out.loc[~no_price]

    # Deterministic order and no duplicate sessions. `keep="last"` because
    # when a vendor sends the same session twice the later row is the
    # correction.
    out = (out.sort_values(["symbol", "date"], kind="mergesort")
              .drop_duplicates(subset=["symbol", "date"], keep="last")
              .reset_index(drop=True))
    return out
