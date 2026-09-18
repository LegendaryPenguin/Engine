"""
Typed configuration.

The YAML is parsed into frozen dataclasses once, at the edge of the
program, and every other module takes the dataclass. That means a typo in
the config fails immediately with a named field rather than surfacing
three modules later as a `KeyError` inside a rolling window.

The SHA-256 of the raw file text is carried on the object and written
into the run manifest, so an output directory can always be traced back
to the exact bytes of the config that produced it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

VALID_PROVIDERS = ("yfinance", "alpaca")
VALID_ALIGN = ("benchmark", "intersect", "union")


@dataclass(frozen=True)
class UniverseConfig:
    tickers: tuple[str, ...]
    benchmark: str

    @property
    def all_symbols(self) -> tuple[str, ...]:
        """Universe plus benchmark, de-duplicated, order preserved.

        The benchmark is downloaded and cleaned exactly like everything
        else — it is a price series, not a special case — but it must not
        appear twice if the user also lists it in `tickers`.
        """
        seen: dict[str, None] = {}
        for sym in (*self.tickers, self.benchmark):
            seen.setdefault(sym, None)
        return tuple(seen)


@dataclass(frozen=True)
class WindowConfig:
    start: date
    end: date | None  # None = most recent available close


@dataclass(frozen=True)
class CalendarConfig:
    align: str
    max_ffill_days: int


@dataclass(frozen=True)
class AnalyticsConfig:
    rolling_vol_windows: tuple[int, ...]
    corr_window: int
    risk_free_annual: float
    trading_days_per_year: int
    min_history_days: int
    extreme_return_threshold: float
    extreme_return_vol_units: float
    quality_vol_window: int
    stale_price_sessions: int

    @property
    def risk_free_daily(self) -> float:
        """Geometric de-annualisation, not `annual / 252`.

        The difference is small at 2% but it is free to be correct, and
        the Sharpe ratio is a graded number.
        """
        return (1.0 + self.risk_free_annual) ** (1.0 / self.trading_days_per_year) - 1.0


@dataclass(frozen=True)
class StorageConfig:
    root: Path
    duckdb_file: str


@dataclass(frozen=True)
class OutputConfig:
    figures: bool
    dpi: int


@dataclass(frozen=True)
class Config:
    run_id: str
    provider: str
    universe: UniverseConfig
    window: WindowConfig
    calendar: CalendarConfig
    analytics: AnalyticsConfig
    storage: StorageConfig
    outputs: OutputConfig
    source_path: Path
    source_sha256: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    # ---- where things go -------------------------------------------
    # Every path is derived, never passed around by hand, so `ingest`
    # and `analyze` cannot disagree about where the data lives.

    @property
    def raw_dir(self) -> Path:
        """One Parquet per symbol, exactly as the provider returned it.

        Keyed by provider as well as run, because the whole point of
        having two providers is being able to compare them.
        """
        return self.storage.root / "raw" / self.provider

    @property
    def curated_dir(self) -> Path:
        return self.storage.root / "curated" / self.run_id

    @property
    def output_dir(self) -> Path:
        return self.storage.root / "outputs" / self.run_id

    @property
    def report_dir(self) -> Path:
        return Path("reports") / self.run_id

    @property
    def figure_dir(self) -> Path:
        return self.report_dir / "figures"

    @property
    def duckdb_path(self) -> Path:
        return self.storage.root / self.storage.duckdb_file

    def ensure_dirs(self) -> None:
        for d in (self.raw_dir, self.curated_dir, self.output_dir,
                  self.report_dir, self.figure_dir):
            d.mkdir(parents=True, exist_ok=True)


def _as_date(value: Any, field_name: str) -> date | None:
    """Accept a real YAML date, an ISO string, or null.

    PyYAML already turns an unquoted `2015-01-01` into a `date`, but a
    quoted one stays a string, and both spellings are natural to write.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field_name}: {value!r} is not an ISO date (YYYY-MM-DD)") from exc
    raise ValueError(f"{field_name}: expected a date, got {type(value).__name__}")


def _require(section: dict[str, Any], key: str, where: str) -> Any:
    if key not in section:
        raise ValueError(f"config is missing `{where}.{key}`")
    return section[key]


def load_config(path: str | Path) -> Config:
    """Read, validate, and hash a config file.

    Validation is eager and total: everything that could be wrong is
    checked here so that no downstream module has to defend itself.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level of the config must be a mapping")

    run_id = str(doc.get("run_id", "baseline")).strip()
    if not run_id:
        raise ValueError("config: `run_id` must not be empty (it names the output directories)")

    provider = str(_require(doc, "provider", "config")).strip().lower()
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"config: provider must be one of {VALID_PROVIDERS}, got {provider!r}")

    uni = _require(doc, "universe", "config")
    tickers_raw = _require(uni, "tickers", "universe")
    if not isinstance(tickers_raw, list) or not tickers_raw:
        raise ValueError("universe.tickers: expected a non-empty list")
    tickers = tuple(dict.fromkeys(str(t).strip().upper() for t in tickers_raw))
    benchmark = str(_require(uni, "benchmark", "universe")).strip().upper()
    if not benchmark:
        raise ValueError("universe.benchmark: must not be empty")

    win = doc.get("window") or {}
    start = _as_date(_require(win, "start", "window"), "window.start")
    end = _as_date(win.get("end"), "window.end")
    if end is not None and start is not None and end <= start:
        raise ValueError(f"window: end ({end}) must be after start ({start})")

    cal = doc.get("calendar") or {}
    align = str(cal.get("align", "benchmark")).strip().lower()
    if align not in VALID_ALIGN:
        raise ValueError(f"calendar.align must be one of {VALID_ALIGN}, got {align!r}")
    # Default 0: prices are NOT forward-filled. See clean.py assumption 3 —
    # a carried price is a fabricated 0% return followed by a fabricated
    # real one, which at a one-day-to-one-month holding period is an
    # artefact big enough to trade on.
    max_ffill = int(cal.get("max_ffill_days", 0))
    if max_ffill < 0:
        raise ValueError("calendar.max_ffill_days must be >= 0")

    an = doc.get("analytics") or {}
    vol_windows = tuple(int(w) for w in an.get("rolling_vol_windows", [21, 63, 252]))
    if not vol_windows or any(w < 2 for w in vol_windows):
        raise ValueError("analytics.rolling_vol_windows: every window must be >= 2")
    corr_window = int(an.get("corr_window", 63))
    if corr_window < 2:
        raise ValueError("analytics.corr_window must be >= 2")
    trading_days = int(an.get("trading_days_per_year", 252))
    if trading_days < 1:
        raise ValueError("analytics.trading_days_per_year must be >= 1")

    quality_vol_window = int(an.get("quality_vol_window", 20))
    if quality_vol_window < 5:
        raise ValueError("analytics.quality_vol_window must be >= 5 to estimate a std")
    vol_units = float(an.get("extreme_return_vol_units", 8.0))
    if vol_units <= 0:
        raise ValueError("analytics.extreme_return_vol_units must be > 0")
    stale_sessions = int(an.get("stale_price_sessions", 3))
    if stale_sessions < 2:
        raise ValueError("analytics.stale_price_sessions must be >= 2 (a run needs a repeat)")

    analytics = AnalyticsConfig(
        rolling_vol_windows=vol_windows,
        corr_window=corr_window,
        risk_free_annual=float(an.get("risk_free_annual", 0.0)),
        trading_days_per_year=trading_days,
        min_history_days=int(an.get("min_history_days", 250)),
        extreme_return_threshold=float(an.get("extreme_return_threshold", 0.50)),
        extreme_return_vol_units=vol_units,
        quality_vol_window=quality_vol_window,
        stale_price_sessions=stale_sessions,
    )

    sto = doc.get("storage") or {}
    storage = StorageConfig(
        root=Path(str(sto.get("root", "data"))),
        duckdb_file=str(sto.get("duckdb_file", "marketengine.duckdb")),
    )

    out = doc.get("outputs") or {}
    outputs = OutputConfig(figures=bool(out.get("figures", True)), dpi=int(out.get("dpi", 150)))

    return Config(
        run_id=run_id,
        provider=provider,
        universe=UniverseConfig(tickers=tickers, benchmark=benchmark),
        window=WindowConfig(start=start, end=end),
        calendar=CalendarConfig(align=align, max_ffill_days=max_ffill),
        analytics=analytics,
        storage=storage,
        outputs=outputs,
        source_path=path,
        source_sha256=sha,
        raw=doc,
    )
