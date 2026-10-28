"""
Strategy layer.

Three strategies, each a pure function from prices to a score per
(date, symbol). A score is not a position and not an order. Keeping the
signal separate from the book is what makes the three comparable, and
what lets a later milestone change the sizing without touching the rules.

Higher score means "more attractive to hold, long only". Scores are
cross-sectional percentile ranks, not z-scores: daily returns are
fat-tailed, and a z-score lets one extreme day dominate the ranking.
A percentile is bounded by construction and needs no winsorisation
parameter to justify.

The rules and the parameter reasons live here, next to the code, and
the report prints them. They were fixed before any result on this
universe was looked at. They are the short-horizon set from ROADMAP
phase 6, not a search over the test window.
"""

from __future__ import annotations

import pandas as pd

from . import features

#: Names, in the order the report and the figures use.
NAMES: tuple[str, ...] = ("momentum", "reversal", "breakout")

#: One paragraph per strategy. Placeholders are filled from the config,
#: so the sentence cannot drift from the parameters that actually ran.
RULES: dict[str, str] = {
    "momentum": (
        "Long the names with the strongest {window}-session total return, "
        "skipping the most recent {skip}. The score is the "
        "cross-sectional percentile of that return: 1 is the strongest name "
        "that day, and a name without a full window is left blank rather "
        "than ranked on a partial one. The skipped session is the "
        "short-term reversal that otherwise contaminates a one-month "
        "momentum signal. {window} sessions is about a month, which is the "
        "long end of the holding horizon this engine is built for, and it "
        "was chosen for that reason rather than by scanning other windows."
    ),
    "reversal": (
        "Long the names with the most negative {return_window}-session "
        "return per unit of trailing {vol_window}-session volatility. "
        "Dividing by volatility is the whole point: a 5% move in a bond "
        "ETF and a 5% move in a single stock are not the same event, and "
        "a rule that treats them as one is mostly a volatility rank. The "
        "score is the percentile of the negated ratio, so the sharpest "
        "vol-adjusted drop ranks first. A name whose trailing volatility "
        "is flat has no defined ratio and is left blank."
    ),
    "breakout": (
        "Long the names closest to their own trailing {window}-session "
        "high. Distance is close divided by that high, minus one, so a "
        "new high scores 0 and everything else scores negative, and the "
        "percentile rank puts the names at their highs first. This is the "
        "trend-following counterweight to the reversal rule. The window "
        "matches the momentum window on purpose: same horizon, opposite "
        "construction, so a high correlation between them would mean they "
        "are one strategy."
    ),
}


def cross_sectional_percentile(values: pd.DataFrame) -> pd.DataFrame:
    """Percentile rank within each date. Top name is 1, bottom is 1/N.

    Ties take the average rank. NaN stays NaN and does not consume a
    rank slot: a name with no history is ineligible, not "average".
    """
    return values.rank(axis=1, method="average", pct=True, na_option="keep")


def momentum_scores(prices: pd.DataFrame, window: int, skip: int) -> pd.DataFrame:
    """Percentile of the trailing return. See `RULES["momentum"]`."""
    raw = features.trailing_return(prices, window, skip)
    return cross_sectional_percentile(features.finite_or_nan(raw))


def reversal_scores(prices: pd.DataFrame, return_window: int, vol_window: int) -> pd.DataFrame:
    """Percentile of the negated vol-scaled return. See `RULES["reversal"]`."""
    move = features.trailing_return(prices, return_window, skip=0)
    vol = features.trailing_volatility(prices, vol_window)
    # A zero volatility makes the ratio infinite, which a rank would then
    # treat as the single most interesting name in the universe. It is
    # undefined, so it is blank.
    scaled = features.finite_or_nan(move / vol.where(vol > 0))
    return cross_sectional_percentile(features.finite_or_nan(-scaled))


def breakout_scores(prices: pd.DataFrame, window: int) -> pd.DataFrame:
    """Percentile of distance to the trailing high. See `RULES["breakout"]`."""
    raw = features.distance_to_high(prices, window)
    return cross_sectional_percentile(features.finite_or_nan(raw))


def score_all(prices: pd.DataFrame, *, momentum_window: int, momentum_skip: int,
              reversal_return_window: int, reversal_vol_window: int,
              breakout_window: int) -> dict[str, pd.DataFrame]:
    """All three score matrices, keyed by strategy name."""
    return {
        "momentum": momentum_scores(prices, momentum_window, momentum_skip),
        "reversal": reversal_scores(prices, reversal_return_window, reversal_vol_window),
        "breakout": breakout_scores(prices, breakout_window),
    }


def target_weights(scores: pd.DataFrame, n_long: int) -> pd.DataFrame:
    """Equal weight on the `n_long` highest scores. Everyone else is zero.

    Each selected name gets `1 / n_long`, not `1 / (however many were
    eligible)`. A day with only one name scored still puts `1 / n_long`
    in that name and leaves the rest in cash. Filling the empty slots by
    concentrating into whatever happened to have data would turn missing
    history into a bigger bet.

    Ties break toward the alphabetically earlier symbol. Columns are
    sorted first so a `method="first"` rank is deterministic rather than
    dependent on whatever order the scores arrived in.
    """
    if n_long < 1:
        raise ValueError(f"n_long must be >= 1, got {n_long}")
    scores = scores.reindex(sorted(scores.columns), axis=1)
    # Higher score -> better (lower) rank. First among ties = earlier column.
    ranks = scores.rank(axis=1, method="first", ascending=False, na_option="keep")
    selected = ranks.le(n_long) & scores.notna()
    return selected.astype(float) * (1.0 / n_long)


def apply_portfolio_caps(weights: pd.DataFrame, dollar_volume: pd.DataFrame, *,
                         max_weight: float | None,
                         max_participation: float | None,
                         adv_window: int,
                         assumed_nav_usd: float) -> pd.DataFrame:
    """Clip target weights. Clipped mass stays cash; it is not redistributed.

    `max_weight` is a hard position cap. `max_participation` caps the
    notional of a name at `participation * trailing median dollar volume`
    for an assumed book of `assumed_nav_usd`. A signal that wants more is
    clipped rather than silently filled at an untradeable size.
    """
    out = weights.astype(float).copy()
    if max_weight is not None:
        out = out.clip(upper=max_weight)
    if max_participation is not None:
        if assumed_nav_usd <= 0:
            raise ValueError("assumed_nav_usd must be > 0 when max_participation is set")
        if adv_window < 2:
            raise ValueError(f"adv_window must be >= 2, got {adv_window}")
        adv = (dollar_volume.reindex(index=out.index, columns=out.columns)
               .rolling(window=adv_window, min_periods=adv_window)
               .median())
        liq_cap = (max_participation * adv) / assumed_nav_usd
        out = out.clip(upper=liq_cap)
        # A missing ADV cannot justify a position of any size.
        out = out.where(adv.notna() & (adv > 0), 0.0)
    return out.fillna(0.0)


def first_invested(weights: pd.DataFrame) -> pd.Timestamp:
    """First date the book actually holds something."""
    invested = weights.sum(axis=1) > 0
    if not bool(invested.any()):
        raise ValueError("weights are zero on every date; the signal never fired")
    return pd.Timestamp(weights.index[int(invested.to_numpy().argmax())])


def score_correlation(scores: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Pearson correlation of the score panels, pairwise over (date, symbol).

    One number per pair of strategies. Computed on the stacked scores
    rather than on the portfolio returns, because the question is whether
    the *rules* agree, not whether the equity curves happened to.
    """
    parts: list[pd.Series] = []
    for name, frame in scores.items():
        stacked = frame.stack()
        stacked.name = name
        parts.append(stacked)
    return pd.concat(parts, axis=1).corr()


def describe(name: str, **params: object) -> str:
    """The rule, with this run's parameters filled in."""
    return RULES[name].format(**params)
