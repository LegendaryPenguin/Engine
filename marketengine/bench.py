"""
Latency, throughput, and freshness measurement.

Two different questions get conflated as "is it fast", and they need
different answers:

  * **Throughput** — how long the pipeline takes end to end. This matters
    for iteration speed and for nothing else. Eleven symbols and eleven
    years is a few seconds of CPU; it is not a risk.
  * **Freshness** — how old the newest bar is relative to the session that
    should exist by now. This matters for correctness. A pipeline that
    runs in 300ms on yesterday's data is worse than useless for a
    one-day-to-one-month holding period, because the position it sizes
    today is sized off a price that has already moved.

So this module reports both, and reports freshness in *sessions* rather
than in seconds, because a daily-bar pipeline is either current or it is
not — there is no meaningful "200ms behind" for a bar that is stamped once
a day after the close.

What this deliberately does **not** claim: daily bars are not a live feed.
The floor on "delay from live pricing" for a daily close is the vendor's
own end-of-day publication lag, which is minutes to hours after 16:00 New
York and is not something this pipeline can improve on. Measuring it
honestly is the point — a number that says "your close is 18 hours old" is
what tells you a same-day intraday decision needs a different data path,
which is a later phase and not a tuning problem.

Percentiles, not averages. A mean fetch latency hides the one request in
twenty that takes four seconds, and that request is the one that decides
whether the daily job finishes before the market opens.
"""

from __future__ import annotations

import math
import platform
import statistics
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import analytics, clean as clean_mod, ingest as ingest_mod, store
from .config import Config
from .providers import ProviderError, get_provider

#: The clock. `perf_counter` and not `time.time`: it is monotonic, so an NTP
#: correction mid-measurement cannot produce a negative duration.
_clock = time.perf_counter


@dataclass
class Stage:
    """One measured step."""
    name: str
    seconds: float
    rows: int = 0

    @property
    def rows_per_second(self) -> float:
        return self.rows / self.seconds if self.seconds > 0 and self.rows else float("nan")


@dataclass
class Result:
    """Everything one `bench` invocation measured."""
    stages: list[Stage] = field(default_factory=list)
    fetch_latency_ms: dict[str, float] = field(default_factory=dict)
    freshness: pd.DataFrame | None = None
    environment: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return sum(s.seconds for s in self.stages)


@contextmanager
def timed(result: Result, name: str):
    """Time a block and append it to `result` as a `Stage`.

    Yields the `Stage` so the body can set `rows` once it knows how many
    there were, which is the only way to get a throughput number without
    counting the rows twice.
    """
    stage = Stage(name=name, seconds=0.0)
    start = _clock()
    try:
        yield stage
    finally:
        stage.seconds = _clock() - start
        result.stages.append(stage)


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile.

    Not interpolated: with 10 samples an interpolated p95 is a weighted
    average of the 9th and 10th, which invents a number between two real
    observations. The 10th observation is a thing that actually happened.
    """
    if not values:
        return float("nan")
    ordered = sorted(values)
    # ceil(q*n), 1-indexed, clamped. `round()` would be wrong here: Python
    # rounds 4.5 down (banker's rounding), so a p50 over ten samples would
    # silently return the 5th value on one input size and the 6th on another.
    idx = min(max(math.ceil(q * len(ordered)) - 1, 0), len(ordered) - 1)
    return ordered[idx]


# --------------------------------------------------------------- freshness


def expected_last_session(now: datetime | None = None) -> date:
    """The most recent session that should have a published close by now.

    A deliberately crude calendar: the last weekday strictly before today,
    or today if the US close has passed. It does not know about holidays,
    so on the day after Thanksgiving it will expect a session that does not
    exist and report one session of staleness. That is the right direction
    to be wrong in — a freshness check that under-reports staleness is
    worthless, one that occasionally cries wolf gets looked at. The
    benchmark's own last bar is used as the real ground truth below; this
    is the independent cross-check that catches the case where the vendor
    is stale for *every* symbol at once, which comparing symbols to each
    other cannot detect.
    """
    now = now or datetime.now(timezone.utc)
    # 16:00 America/New_York is 20:00 or 21:00 UTC depending on DST. 22:00
    # UTC is comfortably after either, and after most vendors' EOD publish.
    cutoff_hour = 22
    day = now.date()
    if now.hour < cutoff_hour:
        day -= timedelta(days=1)
    while day.weekday() >= 5:  # Saturday, Sunday
        day -= timedelta(days=1)
    return day


def freshness(cfg: Config, now: datetime | None = None) -> pd.DataFrame:
    """Per-symbol: how old is the newest stored bar?

    Read from `data/raw/`, not from the curated panel, because the curated
    panel is only as fresh as the last `clean` run and the question here is
    about the data, not about the pipeline.
    """
    now = now or datetime.now(timezone.utc)
    expected = expected_last_session(now)
    bench = cfg.universe.benchmark

    coverage: dict[str, pd.Timestamp] = {}
    for sym in cfg.universe.all_symbols:
        cov = store.raw_coverage(cfg.raw_dir, sym)
        if cov is not None:
            coverage[sym] = cov[1]

    # The benchmark is the reference session: if SPY has a bar for a date,
    # the market was open that date. A symbol behind the benchmark is a
    # per-symbol problem; every symbol behind `expected` together is a
    # vendor problem.
    bench_last = coverage.get(bench)

    rows = []
    for sym in cfg.universe.all_symbols:
        last = coverage.get(sym)
        if last is None:
            rows.append({"symbol": sym, "last_bar": None, "age_days": None,
                         "sessions_behind_benchmark": None, "current": False,
                         "note": "no data on disk"})
            continue
        rows.append({
            "symbol": sym,
            "last_bar": last.date().isoformat(),
            "age_days": (now.date() - last.date()).days,
            # Filled in below, once the benchmark's session dates are loaded.
            "sessions_behind_benchmark": None,
            "current": bool(bench_last is not None and last >= bench_last),
            "note": "",
        })

    out = pd.DataFrame(rows)
    # Fill in the real session count, which needs the benchmark's own dates.
    bench_dates = None
    if bench_last is not None:
        bench_raw = store.read_raw(cfg.raw_dir, [bench])
        if not bench_raw.empty:
            bench_dates = pd.DatetimeIndex(sorted(set(bench_raw["date"])))
    if bench_dates is not None:
        def behind(sym_last: str | None) -> int | None:
            if sym_last is None:
                return None
            ts = pd.Timestamp(sym_last)
            return int((bench_dates > ts).sum())
        out["sessions_behind_benchmark"] = out["last_bar"].map(behind)

    out.attrs["expected_last_session"] = expected.isoformat()
    out.attrs["benchmark_last_session"] = (bench_last.date().isoformat()
                                           if bench_last is not None else None)

    # A bar NEWER than the last completed session is a bar for a session that
    # has not closed yet. Both vendors serve one: a partial daily bar whose
    # "close" is the last trade so far and whose volume is a fraction of the
    # final number. It is not wrong, it is not final, and a short-horizon
    # signal computed on it will change by the actual close — which is how a
    # backtest ends up trading on a price that never existed.
    provisional = bool(bench_last is not None and bench_last.date() > expected)
    out.attrs["provisional_last_session"] = provisional
    return out


# ----------------------------------------------------------- fetch latency


def fetch_latency(cfg: Config, *, trials: int = 5, symbol: str | None = None,
                  lookback_days: int = 7) -> tuple[dict[str, float], list[str]]:
    """Time `trials` small live requests and return percentiles in ms.

    Measures the shape of request the daily job actually makes at the
    margin: one symbol, the last few sessions. A full-history download is
    bandwidth-bound and says nothing about round-trip latency, which is
    what decides whether the incremental daily update takes 2 seconds or
    40.

    The first request is timed separately and reported as `cold_ms`: it
    carries TLS handshake, DNS, and for yfinance a cookie/crumb
    negotiation, and averaging it in makes the steady-state number look
    two or three times worse than it is.
    """
    notes: list[str] = []
    symbol = symbol or cfg.universe.benchmark
    end = date.today()
    start = end - timedelta(days=lookback_days)

    try:
        provider = get_provider(cfg.provider)
    except Exception as exc:  # noqa: BLE001 — credentials, import, anything
        return {}, [f"provider {cfg.provider!r} unavailable: {exc}"]

    samples: list[float] = []
    rows_seen = 0
    for i in range(max(trials, 1)):
        t0 = _clock()
        try:
            got = provider.fetch([symbol], start, end)
        except ProviderError as exc:
            notes.append(f"trial {i + 1} failed: {exc}")
            continue
        samples.append((_clock() - t0) * 1000.0)
        rows_seen = len(got)

    if not samples:
        return {}, notes or ["every latency trial failed"]

    steady = samples[1:] or samples
    out = {
        "trials": float(len(samples)),
        "symbol_count": 1.0,
        "rows_per_request": float(rows_seen),
        "cold_ms": round(samples[0], 1),
        "p50_ms": round(_percentile(steady, 0.50), 1),
        "p95_ms": round(_percentile(steady, 0.95), 1),
        "max_ms": round(max(steady), 1),
        "mean_ms": round(statistics.fmean(steady), 1),
    }
    if len(samples) < 3:
        notes.append("fewer than 3 successful trials: the percentiles are indicative only")
    return out, notes


# ----------------------------------------------------------- stage timings


def measure_pipeline(cfg: Config, *, network: bool = True,
                     refresh: bool = False) -> Result:
    """Time each stage of the pipeline on the real data.

    With `network=False` the ingest stage is skipped entirely and the
    measurement starts from Parquet on disk, which is the honest way to
    report compute cost separately from vendor cost — they have completely
    different variance and you cannot tune one by looking at the sum.
    """
    result = Result(environment=environment(cfg))
    cfg.ensure_dirs()

    if network:
        with timed(result, "ingest") as stage:
            report = ingest_mod.ingest(cfg, refresh=refresh)
            stage.rows = int(report["rows_fetched"].sum())
        if stage.rows == 0:
            result.notes.append(
                "ingest fetched 0 rows — everything was already on disk, so this "
                "is the warm no-op path, not a download benchmark. Use --refresh "
                "for a cold measurement."
            )
    else:
        result.notes.append("ingest skipped (--no-network): timings are compute only")

    with timed(result, "read_raw") as stage:
        raw = store.read_raw(cfg.raw_dir, list(cfg.universe.all_symbols))
        stage.rows = len(raw)
    if raw.empty:
        raise ValueError(f"no raw data under {cfg.raw_dir}; run ingest first")

    with timed(result, "clean") as stage:
        panel, quality, events = clean_mod.clean(cfg, raw)
        stage.rows = len(panel)

    with timed(result, "write_curated") as stage:
        store.write_curated(cfg.curated_dir, panel)
        stage.rows = len(panel)

    with timed(result, "analytics") as stage:
        prices = store.wide(panel, "adj_close")
        rets = analytics.simple_returns(prices)
        analytics.cumulative_returns(rets)
        analytics.correlation_matrix(rets, min_periods=cfg.analytics.min_history_days)
        for window in cfg.analytics.rolling_vol_windows:
            analytics.rolling_volatility(rets, window)
        analytics.summary_table(rets, cfg.universe.benchmark,
                                rf_daily=cfg.analytics.risk_free_daily,
                                periods=cfg.analytics.trading_days_per_year)
        stage.rows = int(rets.notna().to_numpy().sum())

    result.notes.append(
        f"{len(quality)} symbols, {len(events):,} quality events, "
        f"{len(panel):,} curated rows"
    )
    return result


def environment(cfg: Config) -> dict[str, str]:
    """What the numbers were measured on. Latency is meaningless without it."""
    import numpy

    env = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "pandas": pd.__version__,
        "numpy": numpy.__version__,
        "provider": cfg.provider,
        "config_sha256": cfg.source_sha256[:16],
        "symbols": str(len(cfg.universe.all_symbols)),
    }
    try:
        import duckdb
        env["duckdb"] = duckdb.__version__
    except ImportError:  # pragma: no cover
        pass
    return env


# ------------------------------------------------------------------ output


def render(result: Result) -> str:
    """The human-readable report. Also what gets pasted into a submission."""
    lines: list[str] = []
    w = lines.append

    w("environment")
    for k, v in result.environment.items():
        w(f"  {k:<16} {v}")
    w("")

    if result.stages:
        w("stage timings")
        w(f"  {'stage':<16}{'seconds':>10}{'rows':>12}{'rows/sec':>14}")
        for s in result.stages:
            rps = s.rows_per_second
            rps_txt = f"{rps:,.0f}" if rps == rps else "-"
            rows_txt = f"{s.rows:,}" if s.rows else "-"
            w(f"  {s.name:<16}{s.seconds:>10.3f}{rows_txt:>12}{rps_txt:>14}")
        w(f"  {'TOTAL':<16}{result.total_seconds:>10.3f}")
        w("")

    if result.fetch_latency_ms:
        w("provider fetch latency (one symbol, last 7 calendar days)")
        for k, v in result.fetch_latency_ms.items():
            w(f"  {k:<16} {v:,.1f}")
        w("")

    if result.freshness is not None:
        f = result.freshness
        w("data freshness")
        w(f"  expected last session   {f.attrs.get('expected_last_session')}")
        w(f"  benchmark last session  {f.attrs.get('benchmark_last_session')}")
        if f.attrs.get("provisional_last_session"):
            w("  WARNING: the newest bar is for a session that has not closed yet.")
            w("           Its close is the last trade so far and its volume is partial.")
            w("           Pin window.end, or drop the last bar, before trading on it.")
        w("")
        w(f"  {'symbol':<8}{'last_bar':>12}{'age_days':>10}"
          f"{'sessions_behind':>18}{'current':>9}")
        for _, row in f.iterrows():
            behind = row["sessions_behind_benchmark"]
            w(f"  {row['symbol']:<8}{str(row['last_bar'] or '-'):>12}"
              f"{str(row['age_days'] if row['age_days'] is not None else '-'):>10}"
              f"{str(behind if behind is not None else '-'):>18}"
              f"{'yes' if row['current'] else 'NO':>9}")
        stale = f.loc[~f["current"], "symbol"].tolist()
        w("")
        w("  every symbol is current with the benchmark's last session"
          if not stale else f"  BEHIND the benchmark: {', '.join(stale)}")
        w("")

    for note in result.notes:
        w(f"note: {note}")
    return "\n".join(lines)


def to_json(result: Result) -> dict:
    """Machine-readable form, for diffing two runs."""
    return {
        "environment": result.environment,
        "stages": [{"stage": s.name, "seconds": round(s.seconds, 4), "rows": s.rows,
                    "rows_per_second": (round(s.rows_per_second, 1)
                                        if s.rows_per_second == s.rows_per_second else None)}
                   for s in result.stages],
        "total_seconds": round(result.total_seconds, 4),
        "fetch_latency_ms": result.fetch_latency_ms,
        "freshness": {
            "provisional_last_session": (
                bool(result.freshness.attrs.get("provisional_last_session"))
                if result.freshness is not None else None),
            "expected_last_session": (result.freshness.attrs.get("expected_last_session")
                                      if result.freshness is not None else None),
            "benchmark_last_session": (result.freshness.attrs.get("benchmark_last_session")
                                       if result.freshness is not None else None),
            "per_symbol": (result.freshness.to_dict(orient="records")
                           if result.freshness is not None else []),
        },
        "notes": result.notes,
    }


def run_bench(cfg: Config, *, network: bool = True, refresh: bool = False,
              trials: int = 5, json_path: Path | None = None) -> Result:
    """Measure everything and return the result. The CLI's entry point."""
    result = measure_pipeline(cfg, network=network, refresh=refresh)
    if network:
        result.fetch_latency_ms, notes = fetch_latency(cfg, trials=trials)
        result.notes.extend(notes)
    else:
        result.notes.append("fetch latency skipped (--no-network)")
    result.freshness = freshness(cfg)

    if json_path is not None:
        import json

        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(to_json(result), indent=2, default=str))
    return result
