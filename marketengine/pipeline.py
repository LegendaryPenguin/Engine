"""
Orchestration.

Each stage is a function that takes the config and returns what the next
stage needs, and each one is separately runnable from the CLI. The point
of the split is that only `ingest` touches the network: iterating on a
metric or a chart re-runs `analyze` against Parquet on disk and costs
nothing.

    ingest   provider -> data/raw/<provider>/<SYM>.parquet   (network)
    clean    raw      -> data/curated/<run_id>/prices.parquet
    analyze  curated  -> data/outputs/<run_id>/*.csv + figures
    report   all      -> reports/<run_id>/summary.md + run_manifest.json

`run` is all four in order.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import analytics, clean as clean_mod, ingest as ingest_mod, plots, report as report_mod, store
from .config import Config
from .providers import get_provider


def stage_ingest(cfg: Config, *, refresh: bool = False,
                 dry_run: bool = False) -> pd.DataFrame:
    """Download what is missing. Returns the per-symbol ingest report."""
    print("[1/4] ingest")
    cfg.ensure_dirs()
    report = ingest_mod.ingest(cfg, refresh=refresh, dry_run=dry_run)
    ok = int((report["error"] == "").sum())
    print(f"      {ok}/{len(report)} symbol(s) available on disk, "
          f"{int(report['rows_fetched'].sum()):,} new row(s) fetched")
    for _, row in report.loc[report["error"] != ""].iterrows():
        print(f"      ! {row['symbol']}: {row['error']}")
    return report


def stage_clean(cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align and gap-fill. Returns `(panel, quality_report)`."""
    print("[2/4] clean")
    raw = store.read_raw(cfg.raw_dir, list(cfg.universe.all_symbols))
    if raw.empty:
        raise SystemExit(
            f"no raw data under {cfg.raw_dir}. Run `python -m marketengine ingest` first."
        )
    panel, quality = clean_mod.clean(cfg, raw)
    store.write_curated(cfg.curated_dir, panel)
    store.register_duckdb(cfg.duckdb_path, cfg.curated_dir, cfg.raw_dir, cfg.run_id)
    print(f"      {len(panel):,} rows x {panel['symbol'].nunique()} symbols, "
          f"{panel['date'].min().date()} -> {panel['date'].max().date()} "
          f"({int(panel['filled'].sum()):,} forward-filled)")
    print(f"      curated -> {cfg.curated_dir / 'prices.parquet'}")
    print(f"      duckdb  -> {cfg.duckdb_path} (view: prices_{cfg.run_id})")
    return panel, quality


def stage_analyze(cfg: Config, panel: pd.DataFrame) -> dict[str, object]:
    """Compute every required metric; write CSVs and figures.

    Returns a dict carrying the summary table, the correlation matrix, the
    written table paths and the figure paths, which `stage_report` turns
    into Markdown and JSON.
    """
    print("[3/4] analyze")
    bench = cfg.universe.benchmark
    an = cfg.analytics

    prices = store.wide(panel, "adj_close")
    if bench not in prices.columns:
        raise SystemExit(
            f"the benchmark {bench} has no data in the curated panel, so no "
            "benchmark-relative metric can be computed. Check the ingest report."
        )

    rets = analytics.simple_returns(prices)
    cum = analytics.cumulative_returns(rets)
    corr = analytics.correlation_matrix(rets, min_periods=an.min_history_days)
    rel = analytics.relative_performance(rets, bench)
    roll_corr = analytics.rolling_correlation(rets, bench, an.corr_window)
    summary = analytics.summary_table(rets, bench, rf_daily=an.risk_free_daily,
                                      periods=an.trading_days_per_year)

    tables: list[Path] = [
        store.write_table(cfg.output_dir, "prices_adj_close", prices),
        store.write_table(cfg.output_dir, "daily_returns", rets),
        store.write_table(cfg.output_dir, "cumulative_returns", cum),
        store.write_table(cfg.output_dir, "correlation_matrix", corr),
        store.write_table(cfg.output_dir, f"rolling_corr_{an.corr_window}d_vs_{bench}", roll_corr),
        store.write_table(cfg.output_dir, f"relative_cumulative_vs_{bench}", rel),
        store.write_table(cfg.output_dir, "metrics_summary", summary, index=False),
    ]
    for window in an.rolling_vol_windows:
        vol = analytics.rolling_volatility(rets, window)
        tables.append(store.write_table(cfg.output_dir, f"rolling_volatility_{window}d", vol))

    figures: dict[str, Path] = {}
    if cfg.outputs.figures:
        primary_vol_window = an.rolling_vol_windows[0]
        figures = {
            "Growth of $1, total return, log scale":
                plots.plot_growth(rets, bench, cfg.figure_dir / "growth_of_one.png",
                                  cfg.outputs.dpi),
            f"{primary_vol_window}-day rolling volatility, annualised":
                plots.plot_rolling_volatility(rets, bench, primary_vol_window,
                                              cfg.figure_dir / "rolling_volatility.png",
                                              cfg.outputs.dpi),
            "Daily-return correlation matrix":
                plots.plot_correlation_heatmap(corr, cfg.figure_dir / "correlation_heatmap.png",
                                               cfg.outputs.dpi),
            f"Cumulative performance relative to {bench}":
                plots.plot_relative(rets, bench, cfg.figure_dir / "relative_to_benchmark.png",
                                    cfg.outputs.dpi),
            "Risk vs return, annualised":
                plots.plot_risk_return(summary, bench, cfg.figure_dir / "risk_return.png",
                                       cfg.outputs.dpi),
        }

    print(f"      {len(rets):,} return rows x {len(rets.columns)} symbols; "
          f"{len(tables)} table(s), {len(figures)} figure(s)")
    print(f"      tables  -> {cfg.output_dir}")
    if figures:
        print(f"      figures -> {cfg.figure_dir}")

    return {"prices": prices, "returns": rets, "summary": summary, "corr": corr,
            "tables": tables, "figures": figures}


def stage_report(cfg: Config, *, panel: pd.DataFrame, quality: pd.DataFrame,
                 ingest_report: pd.DataFrame, analysis: dict[str, object]) -> dict[str, Path]:
    """Write `summary.md` and `run_manifest.json`."""
    print("[4/4] report")
    tables = list(analysis["tables"])  # type: ignore[arg-type]
    tables.append(store.write_table(cfg.output_dir, "data_quality", quality, index=False))
    tables.append(store.write_table(cfg.output_dir, "ingest_report", ingest_report, index=False))

    # The provider is described without being constructed where possible:
    # `describe()` on Alpaca needs credentials, and a report should still
    # be generatable from cached Parquet on a machine that has none.
    try:
        provider_info = get_provider(cfg.provider).describe()
    except Exception as exc:  # noqa: BLE001
        provider_info = {"provider": cfg.provider,
                         "note": f"provider not reachable while reporting: {exc}"}

    figures = analysis["figures"]  # type: ignore[assignment]
    summary_path = report_mod.write_summary(
        cfg, summary=analysis["summary"], quality=quality, corr=analysis["corr"],  # type: ignore[arg-type]
        ingest_report=ingest_report, panel=panel, figures=figures,  # type: ignore[arg-type]
    )
    manifest_path = report_mod.write_manifest(
        cfg, provider_info=provider_info, panel=panel, ingest_report=ingest_report,
        quality=quality, figures=list(figures.values()), tables=tables,  # type: ignore[union-attr]
    )
    print(f"      summary  -> {summary_path}")
    print(f"      manifest -> {manifest_path}")
    return {"summary": summary_path, "manifest": manifest_path}


def run(cfg: Config, *, refresh: bool = False) -> dict[str, Path]:
    """The whole pipeline, in order."""
    ingest_report = stage_ingest(cfg, refresh=refresh)
    panel, quality = stage_clean(cfg)
    analysis = stage_analyze(cfg, panel)
    return stage_report(cfg, panel=panel, quality=quality,
                        ingest_report=ingest_report, analysis=analysis)
