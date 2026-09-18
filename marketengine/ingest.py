"""
Ingestion.

Turns `provider` + `universe` + `window` from the config into one raw
Parquet file per symbol under `data/raw/<provider>/`.

Three properties matter here, and each is deliberate:

  - **Incremental.** What is already on disk is not re-downloaded. A
    re-run on the same day is a no-op against the network; a re-run
    tomorrow fetches one session. Re-running a finished pipeline is
    something you do constantly while building analytics on top of it, and
    it should cost nothing.
  - **Overlapping.** When new sessions are needed, the request reaches
    `RESTATEMENT_LOOKBACK` days back into data we already have. Vendors
    restate recent bars (late prints, a split adjustment applied a day
    after the fact); without the overlap the restated session would stay
    wrong forever, because we would never ask for it again.
  - **Partial-failure tolerant.** One delisted or misspelled ticker must
    not take down a ten-symbol run. A failed batch is retried symbol by
    symbol and the failures come back as report rows, not as an exception.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from . import store
from .config import Config
from .providers import ProviderError, get_provider
from .providers.base import PriceProvider, empty_panel

# How far back into already-stored data a top-up request reaches. Five
# calendar days covers a normal vendor restatement window without making
# the "nothing new" case expensive.
RESTATEMENT_LOOKBACK = timedelta(days=5)


def _requested_end(cfg: Config) -> date:
    """The end date to plan against.

    `window.end: null` means "latest available", which for planning
    purposes is today. Today may not be a trading day; the provider
    returns nothing past the last close, which is harmless.
    """
    return cfg.window.end or date.today()


def plan(cfg: Config, *, refresh: bool = False) -> dict[str, tuple[date, date] | None]:
    """Decide, per symbol, which date range still has to be downloaded.

    `None` means the symbol needs nothing. Split out from `ingest` so the
    decision is testable with no network access, and so `--dry-run` can
    print it.
    """
    want_start, want_end = cfg.window.start, _requested_end(cfg)
    requested = store.read_requests(cfg.raw_dir)
    out: dict[str, tuple[date, date] | None] = {}

    for sym in cfg.universe.all_symbols:
        if refresh:
            out[sym] = (want_start, want_end)
            continue

        coverage = store.raw_coverage(cfg.raw_dir, sym)
        if coverage is None:
            out[sym] = (want_start, want_end)
            continue

        _, have_last = coverage

        # Is the front of the window already covered? Answered from the
        # recorded REQUEST, not from the earliest stored bar. The earliest
        # bar is later than `window.start` whenever the start lands on a
        # holiday or the symbol listed mid-window, and neither is a gap —
        # comparing against it would re-download the full history forever.
        #
        # A gap at the front cannot be topped up from the end, so when the
        # front really is missing, re-request the whole window: one call,
        # and it also repairs any hole in the middle left behind by an
        # earlier partial failure.
        front_requested = requested.get(sym)
        if front_requested is None or front_requested > want_start.isoformat():
            out[sym] = (want_start, want_end)
            continue

        if have_last.date() >= want_end:
            out[sym] = None
            continue

        fetch_from = max(want_start, (have_last - RESTATEMENT_LOOKBACK).date())
        out[sym] = (fetch_from, want_end)

    return out


def _fetch_batch(
    provider: PriceProvider,
    symbols: list[str],
    start: date,
    end: date,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Fetch `symbols` together; on failure, fall back to one call each.

    Batching is worth doing — yfinance parallelises a multi-symbol request
    and Alpaca bills per request — but a batch is all-or-nothing, and one
    bad symbol in it would lose the other nine. So: try the batch, and if
    it raises, retry individually to find out which symbol is actually the
    problem.
    """
    errors: dict[str, str] = {}
    try:
        return provider.fetch(list(symbols), start, end), errors
    except ProviderError as exc:
        if len(symbols) == 1:
            errors[symbols[0]] = str(exc).splitlines()[0]
            return empty_panel(), errors
        print(f"  ! batch of {len(symbols)} failed ({str(exc).splitlines()[0]}); "
              f"retrying symbol by symbol")

    frames = []
    for sym in symbols:
        try:
            frames.append(provider.fetch([sym], start, end))
        except ProviderError as exc:
            errors[sym] = str(exc).splitlines()[0]
    panel = pd.concat(frames, ignore_index=True) if frames else empty_panel()
    return panel, errors


def ingest(cfg: Config, *, refresh: bool = False, dry_run: bool = False) -> pd.DataFrame:
    """Download whatever is missing and return a per-symbol report.

    The report is a DataFrame rather than log lines because it is written
    to `outputs/<run_id>/ingest_report.csv` and read by the run manifest.
    Columns: symbol, action, fetch_start, fetch_end, rows_fetched,
    stored_first, stored_last, stored_rows, error.
    """
    todo = plan(cfg, refresh=refresh)
    needed = {s: r for s, r in todo.items() if r is not None}

    if dry_run:
        for sym, rng in todo.items():
            print(f"  {sym:<6} {'up to date' if rng is None else f'fetch {rng[0]} -> {rng[1]}'}")
        return _report(cfg, todo, {}, {})

    rows_fetched: dict[str, int] = {}
    errors: dict[str, str] = {}

    if needed:
        provider = get_provider(cfg.provider)
        print(f"  provider: {cfg.provider} | {len(needed)} symbol(s) to fetch, "
              f"{len(todo) - len(needed)} already current")

        # Group by identical date range so symbols that need the same
        # window go out in one request.
        groups: dict[tuple[date, date], list[str]] = {}
        for sym, rng in needed.items():
            groups.setdefault(rng, []).append(sym)

        for (start, end), syms in sorted(groups.items()):
            panel, group_errors = _fetch_batch(provider, sorted(syms), start, end)
            errors.update(group_errors)
            for sym in sorted(syms):
                part = panel.loc[panel["symbol"] == sym]
                rows_fetched[sym] = int(len(part))
                if not part.empty:
                    store.write_raw(cfg.raw_dir, sym, part)
                    # Only on success: remembering a failed request as
                    # covered would make the gap permanent.
                    store.record_request(cfg.raw_dir, sym, start)
                elif sym not in errors:
                    # No exception, no rows: the vendor simply has nothing
                    # for this symbol in this window. Named explicitly so
                    # it is distinguishable from a hard failure.
                    errors[sym] = "provider returned no rows for this window"
    else:
        print("  everything requested is already on disk (use --refresh to re-download)")

    return _report(cfg, todo, rows_fetched, errors)


def _report(
    cfg: Config,
    todo: dict[str, tuple[date, date] | None],
    rows_fetched: dict[str, int],
    errors: dict[str, str],
) -> pd.DataFrame:
    """Build the per-symbol ingest report from what is now on disk."""
    records = []
    for sym in cfg.universe.all_symbols:
        rng = todo.get(sym)
        coverage = store.raw_coverage(cfg.raw_dir, sym)
        stored_rows = 0
        if coverage is not None:
            stored_rows = int(len(pd.read_parquet(store.raw_path(cfg.raw_dir, sym), columns=["date"])))
        records.append({
            "symbol": sym,
            "role": "benchmark" if sym == cfg.universe.benchmark else "universe",
            "action": "skipped (current)" if rng is None else "fetched",
            "fetch_start": rng[0].isoformat() if rng else "",
            "fetch_end": rng[1].isoformat() if rng else "",
            "rows_fetched": rows_fetched.get(sym, 0),
            "stored_first": coverage[0].date().isoformat() if coverage else "",
            "stored_last": coverage[1].date().isoformat() if coverage else "",
            "stored_rows": stored_rows,
            "error": errors.get(sym, ""),
        })
    return pd.DataFrame.from_records(records)
