"""MarketEngine — a reproducible market data, research, and trading engine.

Milestone 1 is the data layer. Milestone 2 adds the first strategies and
a basic backtest on top of that panel, without folding the two together:
signals are pure functions of prices, and the simulator is the only place
a score becomes a position. Later milestones extend both.

    providers/  vendor -> one canonical schema (yfinance, alpaca)
    ingest      incremental download to raw Parquet
    clean       calendar alignment, gap policy, data-quality report
    store       Parquet + DuckDB, the only module that knows paths
    analytics   pure functions: returns, risk, correlation, vs benchmark
    features    pure functions: trailing return, volatility, distance to high
    strategies  pure functions: scores per (date, symbol), no positions
    backtest    next-open simulation, costs, comparison to benchmarks
    validate    chronological train/test, overfit flags, bias report
    plots       figures
    report      run manifest + Markdown summary
    pipeline    ingest, clean, analyze, report, backtest, validate
"""

__version__ = "0.3.0"
