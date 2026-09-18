"""
Alpaca provider — Alpaca Market Data v2 daily bars.

The syllabus's default stack, and the same account the paper-trading
integration in Milestone 5 will use, so it is wired now rather than
bolted on later.

Two things about Alpaca differ from Yahoo and are handled here:

  - Alpaca has no "adjusted close" column. Adjustment is a property of
    the REQUEST (`adjustment=raw|split|dividend|all`), so getting both an
    as-traded close and a total-return close means two calls, merged on
    (symbol, date). That is what `fetch` does.
  - The free tier serves the IEX feed. IEX is one venue, roughly 2-3% of
    consolidated volume, so its daily VOLUME is a fraction of the real
    number and its open/high/low can differ from the consolidated tape.
    Closes are close enough for return analytics; the volume caveat is
    recorded in the manifest and repeated in the README, because a
    volume-based feature built on IEX bars would be wrong in a way that
    is invisible in a chart.

Credentials are read from the environment (or a local `.env`) and never
from the config file, so the config can be committed.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from .base import PriceProvider, ProviderError, empty_panel, normalise

# Either spelling works; the first is what Alpaca's own docs use.
_KEY_VARS = ("ALPACA_API_KEY_ID", "APCA_API_KEY_ID", "ALPACA_API_KEY")
_SECRET_VARS = ("ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY")


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal `KEY=value` loader.

    Deliberately not python-dotenv: this is ten lines and it removes a
    dependency from the install list. Existing environment variables win,
    so an explicitly exported key is never silently overridden by a stale
    file.
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _first_env(names: tuple[str, ...]) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return None


def credentials_available() -> bool:
    """True if a key pair is visible. Used to skip live tests, not to gate `fetch`."""
    load_dotenv()
    return bool(_first_env(_KEY_VARS) and _first_env(_SECRET_VARS))


class AlpacaProvider(PriceProvider):
    name = "alpaca"

    def __init__(self, feed: str | None = None) -> None:
        # Imported inside __init__ so that `provider: yfinance` runs on a
        # machine where alpaca-py is not installed.
        try:
            from alpaca.data.enums import Adjustment, DataFeed
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
        except ImportError as exc:  # pragma: no cover - environment problem
            raise ProviderError(
                "provider 'alpaca' needs the alpaca-py package: pip install alpaca-py"
            ) from exc

        self._Adjustment = Adjustment
        self._StockBarsRequest = StockBarsRequest
        self._TimeFrame = TimeFrame

        load_dotenv()
        key = _first_env(_KEY_VARS)
        secret = _first_env(_SECRET_VARS)
        if not key or not secret:
            raise ProviderError(
                "Alpaca credentials not found. Put them in a .env file at the repo root:\n"
                "  ALPACA_API_KEY_ID=...\n"
                "  ALPACA_API_SECRET_KEY=...\n"
                "(paper keys are fine — this only reads market data)"
            )

        # Default feed is left to the SDK when unset: a paid account
        # resolves to SIP automatically, and hard-coding IEX would quietly
        # downgrade a user who is entitled to the full tape.
        env_feed = (feed or os.environ.get("ALPACA_DATA_FEED") or "").strip().lower()
        self._DataFeed = DataFeed
        self._feed = DataFeed(env_feed) if env_feed else None
        # Set once by _downgrade_to_iex so the warning prints one time and
        # a genuine IEX failure cannot loop.
        self._downgraded = False

        self._client = StockHistoricalDataClient(api_key=key, secret_key=secret)

    def describe(self) -> dict[str, str]:
        return {
            "provider": self.name,
            "vendor": "Alpaca Market Data v2",
            "feed": (f"{self._feed.value} (downgraded from sip: account not entitled)"
                     if self._downgraded else
                     self._feed.value if self._feed else "sip (SDK default)"),
            "adjustment": "close/open/high/low/volume from adjustment=raw (as-traded); "
                          "adj_close from a second adjustment=all request (splits + dividends)",
            "caveat": "on the free IEX feed, volume reflects IEX only (~2-3% of consolidated) "
                      "and o/h/l are IEX prints, not the consolidated tape",
        }

    def fetch(self, symbols: list[str], start: date, end: date | None) -> pd.DataFrame:
        if not symbols:
            return empty_panel()

        # Alpaca's window is timestamp-based and inclusive; end-of-day on
        # the end date captures that session's daily bar.
        start_ts = datetime.combine(start, time.min)
        end_ts = datetime.combine(end or (date.today() - timedelta(days=0)), time.max)

        raw = self._bars(symbols, start_ts, end_ts, self._Adjustment.RAW)
        if raw.empty:
            raise ProviderError(
                f"Alpaca returned no bars for {symbols} between {start} and {end}. "
                "Free accounts cannot request the most recent 15 minutes, and IEX "
                "history does not go back as far as some symbols' listing dates."
            )
        adj = self._bars(symbols, start_ts, end_ts, self._Adjustment.ALL)

        # Left join: a symbol present in RAW but absent from ALL keeps its
        # as-traded close and gets no adjusted series rather than vanishing.
        out = raw.merge(
            adj.loc[:, ["symbol", "date", "close"]].rename(columns={"close": "adj_close"}),
            on=["symbol", "date"],
            how="left",
        )
        out["adj_close"] = out["adj_close"].fillna(out["close"])
        return normalise(out, source="alpaca")

    def _downgrade_to_iex(self, exc: Exception) -> bool:
        """Retry on IEX when the account is not entitled to recent SIP data.

        Leaving `feed` unset lets a paid account resolve to the full SIP
        tape, which is the behaviour worth defaulting to. But the SDK's own
        default is SIP, and a free account asking for a window that ends
        today gets `subscription does not permit querying recent SIP data`
        for every symbol — so "let the account decide" silently means "fail
        on the free tier". Downgrading once, loudly, is better than either
        hard-coding IEX for everyone or making a free key look like broken
        credentials.

        Returns True if the feed was changed and the caller should retry.
        Only ever fires when no feed was requested explicitly: if the user
        asked for SIP, a subscription error is a real answer, not something
        to paper over.
        """
        if self._feed is not None or self._downgraded:
            return False
        if "subscription does not permit" not in str(exc).lower():
            return False

        self._feed = self._DataFeed("iex")
        self._downgraded = True
        print("  ! Alpaca account is not entitled to recent SIP data; "
              "falling back to the IEX feed (volume is IEX-only, ~2-3% of consolidated)")
        return True

    def _bars(self, symbols: list[str], start_ts: datetime, end_ts: datetime, adjustment):
        """One StockBars request, flattened to long rows (without adj_close)."""
        req = self._StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=self._TimeFrame.Day,
            start=start_ts,
            end=end_ts,
            adjustment=adjustment,
            feed=self._feed,
        )
        try:
            bars = self._client.get_stock_bars(req)
        except Exception as exc:  # noqa: BLE001 - SDK raises several types
            if self._downgrade_to_iex(exc):
                return self._bars(symbols, start_ts, end_ts, adjustment)
            raise ProviderError(f"Alpaca request failed ({adjustment.value}): {exc}") from exc

        df = bars.df
        if df is None or df.empty:
            return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])

        # alpaca-py returns a (symbol, timestamp) MultiIndex.
        df = df.reset_index()
        rename = {"timestamp": "date", "trade_count": "trade_count", "vwap": "vwap"}
        df = df.rename(columns=rename)
        keep = ["symbol", "date", "open", "high", "low", "close", "volume"]
        missing = [c for c in keep if c not in df.columns]
        if missing:
            raise ProviderError(f"alpaca: response is missing column(s) {missing}")
        return df.loc[:, keep]
