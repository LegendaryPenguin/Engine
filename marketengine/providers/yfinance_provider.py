"""
yfinance provider — Yahoo Finance daily bars.

Default source because it needs no account and no API key, so `python -m
marketengine run` works on a clean checkout. Yahoo is a scraped,
best-effort feed with no SLA; that is an acceptable trade for coursework
and it is recorded in the manifest so nobody mistakes it for a vendor
guarantee.

Two Yahoo-specific shapes are handled here and nowhere else:

  - `end` is EXCLUSIVE in the Yahoo API, so the requested end date would
    silently go missing. One day is added on the way out.
  - the response is a wide frame with a MultiIndex of (field, symbol) —
    except for a single symbol, where the second level is sometimes
    absent entirely depending on the yfinance version. Both are flattened
    to the long `SCHEMA` here.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from .base import PriceProvider, ProviderError, empty_panel, normalise

# Yahoo's field names -> ours.
_FIELD_MAP = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
}


class YFinanceProvider(PriceProvider):
    name = "yfinance"

    def describe(self) -> dict[str, str]:
        return {
            "provider": self.name,
            "vendor": "Yahoo Finance (unofficial)",
            "library": f"yfinance {getattr(yf, '__version__', 'unknown')}",
            # The adjustment policy is the part that changes the numbers,
            # so it is stated explicitly rather than implied.
            "adjustment": "close = as-traded; adj_close = Yahoo 'Adj Close' "
                          "(splits + dividends, i.e. total return)",
        }

    def fetch(self, symbols: list[str], start: date, end: date | None) -> pd.DataFrame:
        if not symbols:
            return empty_panel()

        # Yahoo's `end` is exclusive; without the +1 the last session the
        # user asked for is missing from every output.
        end_exclusive = (end + timedelta(days=1)) if end is not None else None

        try:
            raw = yf.download(
                tickers=list(symbols),
                start=start.isoformat(),
                end=end_exclusive.isoformat() if end_exclusive else None,
                interval="1d",
                # False so that `Close` stays as-traded and `Adj Close`
                # arrives as a separate column. With auto_adjust=True
                # (the yfinance default since 0.2.51) Yahoo overwrites
                # Close with the adjusted series and drops Adj Close, and
                # the two would be indistinguishable downstream.
                auto_adjust=False,
                actions=False,
                group_by="column",
                progress=False,
                threads=True,
            )
        except Exception as exc:  # noqa: BLE001 - vendor errors are not one type
            raise ProviderError(f"yfinance download failed: {exc}") from exc

        if raw is None or raw.empty:
            raise ProviderError(
                f"yfinance returned no rows for {symbols} between {start} and {end}. "
                "Check the symbols and the date window."
            )

        frames: list[pd.DataFrame] = []
        if isinstance(raw.columns, pd.MultiIndex):
            # (field, symbol). Cross-section per symbol so a symbol that
            # came back all-NaN can be skipped by name.
            available = [s for s in symbols if s in raw.columns.get_level_values(1)]
            for sym in available:
                wide = raw.xs(sym, axis=1, level=1)
                frames.append(self._to_long(wide, sym))
        else:
            # Single symbol, flat columns.
            if len(symbols) != 1:
                raise ProviderError(
                    "yfinance returned flat columns for a multi-symbol request; "
                    "cannot tell the symbols apart"
                )
            frames.append(self._to_long(raw, symbols[0]))

        frames = [f for f in frames if not f.empty]
        if not frames:
            raise ProviderError(
                f"yfinance returned only empty/NaN series for {symbols}; nothing to store"
            )

        return normalise(pd.concat(frames, ignore_index=True), source="yfinance")

    @staticmethod
    def _to_long(wide: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """One symbol's wide frame -> long rows for `SCHEMA`."""
        out = pd.DataFrame(index=wide.index)
        for yahoo_name, ours in _FIELD_MAP.items():
            # Reindexed rather than indexed: Yahoo omits `Adj Close`
            # entirely for some instruments, and a missing optional field
            # should become a NaN column, not a KeyError.
            out[ours] = wide[yahoo_name] if yahoo_name in wide.columns else pd.NA

        # If Yahoo gave no adjusted series, as-traded is the honest
        # fallback. It is not silent — the manifest records the vendor's
        # policy and clean.py reports any symbol whose adj_close it had
        # to substitute.
        out["adj_close"] = out["adj_close"].fillna(out["close"])

        out = out.dropna(subset=["close"], how="all")
        out.index.name = "date"
        out = out.reset_index()
        out["symbol"] = symbol
        return out
