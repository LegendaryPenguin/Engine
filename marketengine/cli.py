"""
Command line.

    python -m marketengine run                 # everything, default config
    python -m marketengine run --refresh       # re-download the full window
    python -m marketengine ingest --dry-run    # what WOULD be downloaded
    python -m marketengine clean
    python -m marketengine analyze
    python -m marketengine bench               # latency, throughput, freshness
    python -m marketengine query "SELECT ..."  # SQL against the curated panel
    python -m marketengine config              # resolved config + paths

Every subcommand takes `--config PATH`, so an alternative universe is a
second YAML file and not a code change.

`ProviderError` and the config's own `ValueError`s are printed as one-line
messages without a traceback: those mean the input or the vendor is wrong,
and a traceback would imply the program is broken.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import pipeline, store
from .config import load_config
from .providers import ProviderError

DEFAULT_CONFIG = Path("config/default.yml")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m marketengine",
        description="MarketEngine — reproducible market data and analytics pipeline.",
    )
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help=f"run configuration (default: {DEFAULT_CONFIG})")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="ingest + clean + analyze + report")
    run.add_argument("--refresh", action="store_true",
                     help="re-download the whole window instead of only what is missing")

    ing = sub.add_parser("ingest", help="download missing bars only")
    ing.add_argument("--refresh", action="store_true", help="re-download the whole window")
    ing.add_argument("--dry-run", action="store_true",
                     help="print the per-symbol download plan and exit")

    sub.add_parser("clean", help="rebuild the curated panel from stored raw data")
    sub.add_parser("analyze", help="recompute metrics and figures from the curated panel")
    sub.add_parser("config", help="print the resolved configuration and output paths")

    bn = sub.add_parser("bench", help="measure stage timings, fetch latency and data freshness")
    bn.add_argument("--no-network", action="store_true",
                    help="skip ingest and fetch-latency trials; measure compute only")
    bn.add_argument("--refresh", action="store_true",
                    help="re-download the full window, for a cold ingest measurement")
    bn.add_argument("--trials", type=int, default=5,
                    help="live fetch requests to time (default: 5)")
    bn.add_argument("--json", type=Path, default=None,
                    help="also write the measurements as JSON to this path")

    q = sub.add_parser("query", help="run SQL against the curated panel via DuckDB")
    q.add_argument("sql", help="e.g. \"SELECT symbol, count(*) FROM prices_latest GROUP BY 1\"")
    return p


def _cmd_config(cfg) -> int:
    print(f"config       {cfg.source_path}  (sha256 {cfg.source_sha256[:16]}…)")
    print(f"run_id       {cfg.run_id}")
    print(f"provider     {cfg.provider}")
    print(f"universe     {', '.join(cfg.universe.tickers)}")
    print(f"benchmark    {cfg.universe.benchmark}")
    print(f"window       {cfg.window.start} -> {cfg.window.end or 'latest available'}")
    print(f"calendar     align={cfg.calendar.align} max_ffill_days={cfg.calendar.max_ffill_days}")
    print(f"risk-free    {cfg.analytics.risk_free_annual:.4%} annual "
          f"= {cfg.analytics.risk_free_daily:.8%} daily")
    print(f"vol windows  {', '.join(str(w) for w in cfg.analytics.rolling_vol_windows)}")
    print("paths:")
    for label, path in (("raw", cfg.raw_dir), ("curated", cfg.curated_dir),
                        ("outputs", cfg.output_dir), ("reports", cfg.report_dir),
                        ("figures", cfg.figure_dir), ("duckdb", cfg.duckdb_path)):
        print(f"  {label:<9} {path}")
    return 0


def _cmd_query(cfg, sql: str) -> int:
    import duckdb

    if not cfg.duckdb_path.exists():
        print(f"no DuckDB file at {cfg.duckdb_path}; run `clean` first.", file=sys.stderr)
        return 2
    # Re-register the views first: the file may have been created by an
    # earlier run whose Parquet has since been rebuilt.
    store.register_duckdb(cfg.duckdb_path, cfg.curated_dir, cfg.raw_dir, cfg.run_id)
    con = duckdb.connect(str(cfg.duckdb_path), read_only=True)
    try:
        print(con.sql(sql))
    finally:
        con.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"config file not found: {args.config}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "config":
            return _cmd_config(cfg)

        if args.command == "query":
            return _cmd_query(cfg, args.sql)

        cfg.ensure_dirs()

        if args.command == "ingest":
            pipeline.stage_ingest(cfg, refresh=args.refresh, dry_run=args.dry_run)
            return 0

        if args.command == "bench":
            from . import bench as bench_mod

            result = bench_mod.run_bench(
                cfg, network=not args.no_network, refresh=args.refresh,
                trials=args.trials, json_path=args.json,
            )
            print(bench_mod.render(result))
            if args.json:
                print(f"\njson -> {args.json}")
            return 0

        if args.command == "clean":
            pipeline.stage_clean(cfg)
            return 0

        if args.command == "analyze":
            from . import store as _store

            panel = _store.read_curated(cfg.curated_dir)
            pipeline.stage_analyze(cfg, panel)
            return 0

        if args.command == "run":
            pipeline.run(cfg, refresh=args.refresh)
            print("\ndone.")
            return 0

    except ProviderError as exc:
        print(f"\ndata source error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
