"""
Figures.

Five PNGs, written to `reports/<run_id>/figures/`. The milestone asks for
at least two useful outputs; these five were chosen because each answers a
different question and none of them is decoration:

  1. growth_of_one       — what actually happened, benchmark included
  2. rolling_volatility  — when risk changed (the 2020 and 2022 spikes)
  3. correlation_heatmap — what is genuinely diversifying and what is not
  4. relative_to_benchmark — the only chart that shows out/under-performance
  5. risk_return          — return per unit of risk, in one glance

Conventions that make them readable rather than merely present:

  - The benchmark is always the same colour (black, dashed, thicker) in
    every chart. In a ten-line chart the eye needs one fixed reference.
  - Growth is on a LOG y-axis. On a linear axis a 10x winner compresses
    everything else into the bottom inch, and equal vertical distances
    stop meaning equal percentage moves.
  - Matplotlib's Agg backend is set before pyplot is imported, so the
    pipeline runs headless (CI, ssh) without trying to open a window.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # must precede the pyplot import

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from . import analytics  # noqa: E402

BENCH_STYLE = {"color": "#111111", "linewidth": 2.0, "linestyle": "--", "zorder": 5}
GRID_STYLE = {"color": "#CCCCCC", "linewidth": 0.6, "alpha": 0.8}
# Tab20 rather than the default cycle: ten distinguishable hues, and the
# default ten-colour cycle repeats before a ten-name universe is finished.
PALETTE = plt.get_cmap("tab20").colors


def _style_axes(ax, title: str, ylabel: str) -> None:
    ax.set_title(title, fontsize=12, fontweight="bold", loc="left")
    ax.set_ylabel(ylabel, fontsize=9)
    ax.grid(True, **GRID_STYLE)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(labelsize=8)


def _save(fig, path: Path, dpi: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path


def _colours(symbols, benchmark: str) -> dict[str, dict]:
    """A stable colour per symbol, with the benchmark forced to black.

    Keyed off the sorted symbol list so a colour does not change between
    runs when a ticker is added to the config.
    """
    out: dict[str, dict] = {}
    i = 0
    for sym in symbols:
        if sym == benchmark:
            out[sym] = dict(BENCH_STYLE)
        else:
            out[sym] = {"color": PALETTE[i % len(PALETTE)], "linewidth": 1.2}
            i += 1
    return out


def plot_growth(returns: pd.DataFrame, benchmark: str, path: Path, dpi: int = 150) -> Path:
    """Cumulative growth of $1, log scale."""
    curve = analytics.growth_of_one(returns)
    styles = _colours(list(curve.columns), benchmark)

    fig, ax = plt.subplots(figsize=(11, 6))
    for sym in curve.columns:
        ax.plot(curve.index, curve[sym], label=sym, **styles[sym])

    ax.set_yscale("log")
    # Explicit tick labels: on a log axis matplotlib defaults to
    # scientific notation, and "$3" is the point of the chart.
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.2f}"))
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    _style_axes(ax, f"Growth of $1 (total return, log scale) — benchmark {benchmark}",
                "value of $1 invested")
    ax.legend(ncols=6, fontsize=8, frameon=False, loc="upper left")
    return _save(fig, path, dpi)


def plot_rolling_volatility(returns: pd.DataFrame, benchmark: str, window: int,
                            path: Path, dpi: int = 150) -> Path:
    """Annualised rolling volatility at one window length."""
    vol = analytics.rolling_volatility(returns, window)
    styles = _colours(list(vol.columns), benchmark)

    fig, ax = plt.subplots(figsize=(11, 6))
    for sym in vol.columns:
        ax.plot(vol.index, vol[sym], label=sym, **styles[sym])

    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=1.0))
    _style_axes(ax, f"{window}-day rolling volatility, annualised", "annualised sigma")
    ax.legend(ncols=6, fontsize=8, frameon=False, loc="upper left")
    return _save(fig, path, dpi)


def plot_correlation_heatmap(corr: pd.DataFrame, path: Path, dpi: int = 150) -> Path:
    """Correlation matrix as an annotated heatmap.

    Fixed colour limits of -1..1, not the data range: an auto-scaled
    correlation heatmap makes 0.71-to-0.94 look like no-correlation to
    perfect, which is the opposite of what it shows.
    """
    labels = list(corr.columns)
    fig, ax = plt.subplots(figsize=(1.0 + 0.62 * len(labels), 0.9 + 0.58 * len(labels)))
    data = corr.to_numpy(dtype=float)
    im = ax.imshow(data, cmap="RdBu_r", vmin=-1.0, vmax=1.0)

    ax.set_xticks(range(len(labels)), labels, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(labels)), labels, fontsize=8)
    for i in range(len(labels)):
        for j in range(len(labels)):
            v = data[i, j]
            if np.isnan(v):
                continue
            # Flip the text to white on the dark ends of the ramp.
            ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7,
                    color="white" if abs(v) > 0.6 else "#222222")
    ax.set_title("Daily-return correlation", fontsize=12, fontweight="bold", loc="left")
    fig.colorbar(im, ax=ax, fraction=0.045, pad=0.04)
    return _save(fig, path, dpi)


def plot_relative(returns: pd.DataFrame, benchmark: str, path: Path, dpi: int = 150) -> Path:
    """Cumulative return minus the benchmark's, through time."""
    rel = analytics.relative_performance(returns, benchmark)
    styles = _colours(list(rel.columns) + [benchmark], benchmark)

    fig, ax = plt.subplots(figsize=(11, 6))
    for sym in rel.columns:
        ax.plot(rel.index, rel[sym], label=sym, **styles[sym])
    # Zero is the benchmark. Drawn as the reference line rather than as a
    # flat series, which would add a meaningless legend entry.
    ax.axhline(0.0, **BENCH_STYLE)

    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=1.0))
    _style_axes(ax, f"Cumulative performance relative to {benchmark} "
                    f"(0 = matching {benchmark})", f"excess vs {benchmark}")
    ax.legend(ncols=6, fontsize=8, frameon=False, loc="upper left")
    return _save(fig, path, dpi)


def plot_risk_return(summary: pd.DataFrame, benchmark: str, path: Path,
                     dpi: int = 150) -> Path:
    """Annualised return against annualised volatility, one point per symbol."""
    fig, ax = plt.subplots(figsize=(8.5, 6.5))
    for _, row in summary.iterrows():
        is_bench = row["symbol"] == benchmark
        ax.scatter(row["ann_volatility"], row["cagr"],
                   s=150 if is_bench else 90,
                   color="#111111" if is_bench else "#2E6F9E",
                   marker="D" if is_bench else "o", zorder=4 if is_bench else 3)
        ax.annotate(row["symbol"], (row["ann_volatility"], row["cagr"]),
                    textcoords="offset points", xytext=(7, 4), fontsize=8,
                    fontweight="bold" if is_bench else "normal")

    bench = summary.loc[summary["symbol"] == benchmark]
    if not bench.empty:
        # The line through the origin and the benchmark: anything above it
        # earned more return per unit of risk than the index did.
        v = float(bench.iloc[0]["ann_volatility"])
        c = float(bench.iloc[0]["cagr"])
        if v > 0:
            xs = np.linspace(0, float(summary["ann_volatility"].max()) * 1.1, 50)
            ax.plot(xs, xs * (c / v), color="#999999", linewidth=1.0, linestyle=":",
                    label=f"{benchmark} return-per-risk line")
            ax.legend(fontsize=8, frameon=False, loc="upper left")

    ax.axhline(0.0, color="#888888", linewidth=0.8)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=1.0))
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(xmax=1.0))
    _style_axes(ax, "Risk vs return, annualised, full sample", "CAGR")
    ax.set_xlabel("annualised volatility", fontsize=9)
    return _save(fig, path, dpi)
