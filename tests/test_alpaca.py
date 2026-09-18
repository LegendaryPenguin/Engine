"""
Alpaca provider tests.

The part worth testing offline is the two-request adjustment merge. Alpaca
has no adjusted-close column — adjustment is a property of the request — so
`fetch` issues one `RAW` request for as-traded OHLCV and one `ALL` request
whose close becomes `adj_close`. If that merge misaligns, every return in
the pipeline is quietly computed from the wrong series, and the numbers
still look plausible. So the merge is exercised against a fake client here,
and the live API is exercised separately when credentials exist.
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import pytest

from marketengine.providers import ProviderError
from marketengine.providers.alpaca_provider import (
    AlpacaProvider,
    credentials_available,
    load_dotenv,
)


class FakeBars:
    """Stands in for alpaca-py's `BarSet`, which exposes `.df`."""

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df


def bars_df(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    """alpaca-py hands back a (symbol, timestamp) MultiIndex frame."""
    frame = pd.DataFrame({
        "symbol": [s for s, _, _, _ in rows],
        "timestamp": pd.to_datetime([d for _, d, _, _ in rows], utc=True),
        "open": [c for _, _, c, _ in rows],
        "high": [c for _, _, c, _ in rows],
        "low": [c for _, _, c, _ in rows],
        "close": [c for _, _, c, _ in rows],
        "volume": [v for _, _, _, v in rows],
        "trade_count": 1,
        "vwap": [c for _, _, c, _ in rows],
    })
    return frame.set_index(["symbol", "timestamp"])


class FakeClient:
    """Returns a different close depending on the requested adjustment."""

    def __init__(self, raw: pd.DataFrame, adjusted: pd.DataFrame) -> None:
        self.raw, self.adjusted = raw, adjusted
        self.calls: list[str] = []

    def get_stock_bars(self, req):
        self.calls.append(req.adjustment.value)
        return FakeBars(self.adjusted if req.adjustment.value == "all" else self.raw)


@pytest.fixture
def provider(monkeypatch) -> AlpacaProvider:
    """A real AlpacaProvider with a fake HTTP client bolted in.

    Constructed for real so the SDK's own request objects and enums are
    used — a hand-rolled stub of `StockBarsRequest` would not catch an
    argument the SDK renamed.
    """
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    monkeypatch.delenv("ALPACA_DATA_FEED", raising=False)
    return AlpacaProvider()


def test_missing_credentials_are_a_clear_provider_error(monkeypatch):
    for var in ("ALPACA_API_KEY_ID", "APCA_API_KEY_ID", "ALPACA_API_KEY",
                "ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Neutralise any repo-root .env so this test does not depend on the
    # machine it runs on.
    monkeypatch.setattr("marketengine.providers.alpaca_provider.load_dotenv",
                        lambda *a, **k: None)
    with pytest.raises(ProviderError, match="credentials not found"):
        AlpacaProvider()


def test_fetch_issues_exactly_two_requests_one_per_adjustment(provider):
    raw = bars_df([("AAPL", "2024-01-02", 100.0, 1_000.0)])
    adj = bars_df([("AAPL", "2024-01-02", 95.0, 1_000.0)])
    provider._client = FakeClient(raw, adj)

    provider.fetch(["AAPL"], date(2024, 1, 2), date(2024, 1, 2))

    assert provider._client.calls == ["raw", "all"]


def test_adj_close_comes_from_the_adjusted_request_and_close_from_the_raw_one(provider):
    # A dividend-paying symbol: as-traded close 100, total-return close 95.
    raw = bars_df([("AAPL", "2024-01-02", 100.0, 1_000.0),
                   ("AAPL", "2024-01-03", 102.0, 1_100.0)])
    adj = bars_df([("AAPL", "2024-01-02", 95.0, 1_000.0),
                   ("AAPL", "2024-01-03", 96.9, 1_100.0)])
    provider._client = FakeClient(raw, adj)

    out = provider.fetch(["AAPL"], date(2024, 1, 2), date(2024, 1, 3)).set_index("date")

    assert out.loc[pd.Timestamp("2024-01-02"), "close"] == 100.0
    assert out.loc[pd.Timestamp("2024-01-02"), "adj_close"] == 95.0
    # Volume is only ever taken from the raw request; adjustment does not
    # change share counts the way it changes prices.
    assert out.loc[pd.Timestamp("2024-01-03"), "volume"] == 1_100.0


def test_the_merge_aligns_per_symbol_and_does_not_cross_symbols(provider):
    # The failure this guards against: merging on date alone would pair
    # AAPL's raw close with MSFT's adjusted close and produce a return
    # series that is wrong but entirely plausible-looking.
    raw = bars_df([("AAPL", "2024-01-02", 100.0, 10.0),
                   ("MSFT", "2024-01-02", 400.0, 20.0)])
    adj = bars_df([("AAPL", "2024-01-02", 95.0, 10.0),
                   ("MSFT", "2024-01-02", 390.0, 20.0)])
    provider._client = FakeClient(raw, adj)

    out = provider.fetch(["AAPL", "MSFT"], date(2024, 1, 2), date(2024, 1, 2))
    got = out.set_index("symbol")["adj_close"].to_dict()

    assert got == {"AAPL": 95.0, "MSFT": 390.0}
    assert len(out) == 2  # no row multiplication from a sloppy join


def test_a_symbol_missing_from_the_adjusted_response_keeps_its_raw_close(provider):
    # Left join, deliberately: losing the row entirely would turn a vendor
    # hiccup on one symbol into a hole in the panel.
    raw = bars_df([("AAPL", "2024-01-02", 100.0, 10.0),
                   ("MSFT", "2024-01-02", 400.0, 20.0)])
    adj = bars_df([("AAPL", "2024-01-02", 95.0, 10.0)])
    provider._client = FakeClient(raw, adj)

    out = provider.fetch(["AAPL", "MSFT"], date(2024, 1, 2), date(2024, 1, 2))
    got = out.set_index("symbol")

    assert len(out) == 2
    assert got.loc["MSFT", "adj_close"] == 400.0  # fell back to close


def test_timestamps_are_normalised_to_tz_naive_sessions(provider):
    # Alpaca stamps daily bars 05:00Z (US market open in UTC). Left as-is,
    # those keys would never join with yfinance's midnight-local dates.
    raw = bars_df([("AAPL", "2024-01-02T05:00:00", 100.0, 10.0)])
    provider._client = FakeClient(raw, raw)

    out = provider.fetch(["AAPL"], date(2024, 1, 2), date(2024, 1, 2))

    assert out["date"].iloc[0] == pd.Timestamp("2024-01-02")
    assert out["date"].dt.tz is None


def test_an_empty_raw_response_is_a_provider_error_not_an_empty_panel(provider):
    empty = pd.DataFrame()
    provider._client = FakeClient(empty, empty)
    with pytest.raises(ProviderError, match="no bars"):
        provider.fetch(["AAPL"], date(2024, 1, 2), date(2024, 1, 3))


def test_an_sdk_exception_is_wrapped_with_the_adjustment_that_failed(provider):
    class Boom:
        def get_stock_bars(self, req):
            raise RuntimeError("403 forbidden")

    provider._client = Boom()
    with pytest.raises(ProviderError, match="raw.*403"):
        provider.fetch(["AAPL"], date(2024, 1, 2), date(2024, 1, 3))


def test_fetch_of_no_symbols_is_not_a_network_call(provider):
    class Boom:
        def get_stock_bars(self, req):  # pragma: no cover - must not be reached
            raise AssertionError("should not have been called")

    provider._client = Boom()
    assert provider.fetch([], date(2024, 1, 2), date(2024, 1, 3)).empty


def test_describe_records_the_adjustment_policy_and_the_iex_caveat(provider):
    info = provider.describe()
    assert "adjustment=raw" in info["adjustment"]
    assert "adjustment=all" in info["adjustment"]
    assert "IEX" in info["caveat"]


def test_dotenv_does_not_override_an_exported_variable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ALPACA_API_KEY_ID=from-file\nSOME_NEW_KEY=from-file\n")
    monkeypatch.setenv("ALPACA_API_KEY_ID", "from-shell")
    monkeypatch.delenv("SOME_NEW_KEY", raising=False)

    load_dotenv()

    assert os.environ["ALPACA_API_KEY_ID"] == "from-shell"
    assert os.environ["SOME_NEW_KEY"] == "from-file"


# =====================================================================
# live API — skipped unless credentials are present
# =====================================================================

live = pytest.mark.skipif(
    not credentials_available(),
    reason="no Alpaca credentials; put ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY in .env",
)


@live
def test_live_fetch_returns_a_schema_conformant_panel():
    from marketengine.providers.base import COLUMNS

    out = AlpacaProvider().fetch(["AAPL", "SPY"], date(2024, 1, 2), date(2024, 1, 31))

    assert list(out.columns) == list(COLUMNS)
    assert set(out["symbol"]) == {"AAPL", "SPY"}
    assert out["close"].gt(0).all()
    assert out["adj_close"].gt(0).all()
    assert not out.duplicated(subset=["symbol", "date"]).any()
    # 2024-01-02 through 2024-01-31 inclusive is 21 sessions: 22 weekdays
    # less MLK Day on the 15th. Asserting the exact count is the point —
    # an off-by-one in the provider's end-date handling is invisible in a
    # "returned some rows" check, and Alpaca's `end` is inclusive where
    # yfinance's is exclusive.
    assert out.groupby("symbol").size().eq(21).all()


@live
def test_live_adjusted_and_as_traded_closes_differ_for_a_dividend_payer():
    # AAPL paid dividends in this window, so the total-return series must
    # sit below the as-traded one. If these are identical, the ALL request
    # is not doing what the manifest claims it does.
    out = AlpacaProvider().fetch(["AAPL"], date(2023, 1, 3), date(2024, 1, 31))
    assert not out["close"].equals(out["adj_close"])
    assert (out["adj_close"] <= out["close"] * 1.001).all()
