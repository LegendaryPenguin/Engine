"""Provider registry.

Lazy on purpose: importing this package must not import alpaca-py or
yfinance, so a run with `provider: yfinance` does not pay for (or fail on)
the other vendor's SDK.
"""

from __future__ import annotations

from .base import (
    COLUMNS,
    PRICE_COLUMNS,
    SCHEMA,
    PriceProvider,
    ProviderError,
    empty_panel,
    normalise,
)

__all__ = [
    "COLUMNS",
    "PRICE_COLUMNS",
    "SCHEMA",
    "PriceProvider",
    "ProviderError",
    "empty_panel",
    "get_provider",
    "normalise",
]


def get_provider(name: str) -> PriceProvider:
    """Build the provider named in the config."""
    key = name.strip().lower()
    if key == "yfinance":
        from .yfinance_provider import YFinanceProvider

        return YFinanceProvider()
    if key == "alpaca":
        from .alpaca_provider import AlpacaProvider

        return AlpacaProvider()
    raise ProviderError(f"unknown provider {name!r}; expected 'yfinance' or 'alpaca'")
