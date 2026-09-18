"""MarketEngine — a reproducible market data and analytics pipeline.

Milestone 1 of EECS 692. The layering here is deliberate and is what
Milestones 2-5 build on without rewriting:

    providers/  vendor -> one canonical schema (yfinance, alpaca)
    ingest      incremental download to raw Parquet
    clean       calendar alignment, gap policy, data-quality report
    store       Parquet + DuckDB, the only module that knows paths
    analytics   pure functions: returns, risk, correlation, vs benchmark
    plots       figures
    report      run manifest + Markdown summary
    pipeline    the four stages, in order
"""

__version__ = "0.1.0"
