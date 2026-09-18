# Roadmap

Where this goes after Milestone 1: a system that picks and holds positions
over **one day to about one month**, sized and rebalanced daily, on daily
bars. Not intraday, not day trading. That horizon decides almost every
design choice below, so it is worth being explicit about what it implies:

- **Costs dominate, and the shorter the hold, the more.** Round-trip cost
  drag is roughly `(2s / 10,000) × (252 / H)` annualised, where `s` is
  one-way cost in basis points and `H` is the holding period in sessions.
  At 5bp per side and a 5-day hold that is about 5% a year of headwind. At
  a 20-day hold it is about 1.3%. A strategy that does not survive that
  arithmetic does not survive, and no amount of signal work fixes it —
  which is why the cost model (phase 7) is not deferred to the end.
- **Daily bars can settle entry and exit, and nothing finer.** A signal
  computed on session `t`'s close can first be filled at `t+1`'s open. No
  intraday stop-losses, ever: a daily bar does not order the low against
  the high, so "was the stop hit before the target" is unanswerable and any
  backtest that answers it is making the answer up.
- **Freshness is a correctness property, not a performance one.** See
  phase 4.

Each phase below has **exit criteria**: the things that must be
demonstrably true before starting the next one. They are written so that
failing them is unambiguous. A phase is not done because the code exists;
it is done because the criteria pass and there is a test or an artefact
showing it.

---

## Phase 1 — Milestone 1: the data layer  ✅ DONE

The reusable daily price/volume layer, the analytics on top of it, and the
evidence that it works.

**Built:** configurable universe from YAML; two providers behind one schema
(yfinance, Alpaca); incremental idempotent ingest with a restatement
overlap; quarantine → align → flag cleaning with a per-row `flags` column,
a per-symbol quality report and a per-bar event log; returns, cumulative
returns, rolling volatility, correlations and a full benchmark-relative
block; Parquet + DuckDB views; five figures and a generated report; a
`bench` subcommand measuring stage timings, fetch latency percentiles and
data freshness.

**Exit criteria**

| # | Criterion | Status |
|---|---|---|
| 1.1 | Universe, window, benchmark and every threshold come from YAML; no ticker or date literal anywhere in `marketengine/` | pass |
| 1.2 | A second provider runs the same pipeline with one line changed, and its merge logic is unit-tested against stub responses | pass — `config/alpaca.yml`, `tests/test_alpaca.py` |
| 1.3 | Re-running on the same day makes zero network calls; re-running tomorrow fetches one session | pass — verified, `_requests.json` front-coverage |
| 1.4 | Every analytics function has a closed-form test, not a snapshot of its own output | pass — 121 tests, 0 network |
| 1.5 | Every suspicious bar is named on the row and logged with its value; the only rows removed are structurally impossible ones | pass — `flags` column + `quality_events.csv` |
| 1.6 | Output traceable to the exact config bytes that produced it | pass — SHA-256 in `run_manifest.json` |
| 1.7 | Submission evidence generated from real terminal runs, not transcribed | pass — `submission/milestone1/` |

---

## Phase 2 — Point-in-time data model

The change that makes everything after it trustworthy, and the one that is
painful to retrofit later. Today a row says *what* the price was. It needs
to also say *when we learned it* and *which price basis it is*.

**Work**

- Add to raw and curated: `source` (vendor), `source_timestamp` (the
  vendor's own stamp), `ingested_at_utc` (when this process wrote it), and
  `price_basis` ∈ {`raw`, `split_adjusted`, `total_return`}.
- Make the unique key `(security_id, session_date, price_basis)` — the
  same session legitimately has three different prices and today they are
  three columns pretending to be one row.
- A **security master**: `security_id` stable across ticker changes, with
  `first_trade_date` / `last_trade_date`. FB → META must not look like two
  securities, and META's history must not look like it starts in 2021.
- `session_date` explicitly in `America/New_York`, stored tz-naive, with
  the timezone recorded in the schema rather than in a comment.
- A **quarantine table** on disk, not just event rows, so a rejected bar
  can be re-examined and re-admitted without a re-download.

**Why it matters at this horizon:** the two-price rule. Economic returns
need total-return prices. Price *patterns* (a 20-day high, a breakout) need
split-adjusted but **not** dividend-adjusted prices, or a dividend looks
like a downside break. Execution needs **raw as-traded** prices, because
that is the number that was on the screen and what a $10,000 order buys.
Conflating any two of these is a silent error that survives every test that
does not know the distinction exists.

**Exit criteria**

- 2.1 A ticker change is representable: one `security_id`, two ticker rows with
  disjoint date ranges, and a test that a rename does not split the history.
- 2.2 Every curated row carries `source`, `source_timestamp`, `ingested_at_utc`
  and `price_basis`, and reading two providers' output concatenates without
  a dtype or timezone conflict.
- 2.3 A test asserts a breakout computed on total-return prices differs from one
  computed on split-adjusted prices for a known dividend payer, and that the
  feature layer uses the split-adjusted one.
- 2.4 A quarantined bar round-trips: written, listed, re-admitted, with no
  network call.
- 2.5 `SELECT count(*) FROM curated WHERE price_basis IS NULL` returns 0.

---

## Phase 3 — Integrity hardening

The remaining checks from the data-engineering literature, and the
universe-construction bias that no amount of cleaning fixes.

**Work**

- Corporate actions stored as **events** (split ratio, dividend amount,
  ex-date), not just implied by a ratio between `close` and `adj_close`.
  Then a `CORPORATE_ACTION` flag on the affected session, and an
  independent check that the observed price ratio matches the stated split
  ratio — a mis-applied split is the single most common vendor error and it
  looks exactly like a 50% one-day loss.
- `AFTER_SECURITY_END` flag, and a hard rule that no bar past
  `last_trade_date` is ever admitted.
- **Point-in-time universe**: membership with `valid_from` / `valid_to`, so
  a backtest on "today's ten names" can be distinguished from a backtest on
  "the ten names as of 2015". The current universe is hand-picked survivors
  and every result on it is optimistic by an amount nothing in the output
  reveals.
- Liquidity screen: 20-day median dollar volume, and a minimum price, both
  evaluated **as of the decision date** and not over the whole sample.

**Exit criteria**

- 3.1 Splits and dividends stored as dated events; a test that a known 4:1 split
  (AAPL 2020-08-31) is present with the right ratio and flagged.
- 3.2 A synthetic mis-applied split is caught by the ratio cross-check, not by
  the extreme-return flag.
- 3.3 Universe membership is queryable as of a date; the same query on two dates
  returns different sets.
- 3.4 The liquidity screen uses only data available before the decision date,
  proven by a test that shifts the input forward and sees the decision change.

---

## Phase 4 — Freshness and the latency budget

The user-facing requirement: *accurate prices with minimal delay from
live*. Phase 1 measures the delay; this phase acts on it and sets the
contract.

**The honest constraint first.** A daily close is published minutes to
hours after 16:00 New York. There is no version of a daily-bar pipeline
that is "close to live". What is achievable, and what actually matters at a
one-day-to-one-month horizon:

- Know precisely how stale the newest bar is (done — `bench`).
- Never compute a signal on an unclosed session's partial bar (detected —
  `provisional_last_session`; this phase makes it a hard refusal).
- Get the previous close into the panel fast enough to act at the next
  open, which is a ~17-hour window, not a millisecond one.
- Add a **separate** low-latency path for the *execution* decision: the
  last trade / quote at order time, from Alpaca's real-time endpoint. That
  is a different data path with different guarantees, and it must not be
  mixed into the daily panel.

**Work**

- Hard refusal (not a warning) to emit trading signals from a panel whose
  last session is provisional, unless explicitly overridden.
- A freshness SLA in the config: max sessions behind benchmark, max age;
  `bench --check` exits non-zero on breach, so it can gate a cron job.
- A latency budget for the daily update, measured and asserted: fetch →
  clean → features → signals, per symbol and total.
- A real-time quote adapter (Alpaca) used only at order time, tagged with
  its own `source` and never written into the daily panel.
- Retry with backoff and a per-provider circuit breaker; record the retry
  count so a slow day is visible after the fact.

**Exit criteria**

- 4.1 `bench --check` exits non-zero when any symbol is a session behind, and
  zero when all are current. Tested with synthetic stale data.
- 4.2 Signal generation raises on a provisional last session; a test proves it.
- 4.3 Daily incremental update for the full universe completes inside a stated
  wall-clock budget, measured by `bench` on real requests, with p95 reported.
- 4.4 The real-time quote path is a separate module, returns a timestamped
  quote, and is never a source for the daily panel — enforced by a test that
  asserts no curated row has the realtime `source`.

---

## Phase 5 — Feature layer

Pure functions from the panel to features, with the same discipline
`analytics.py` already has: no I/O, closed-form tests.

**Work**

- Multi-horizon returns (1, 5, 20, 60 sessions), from total-return prices.
- EWMA and rolling realised volatility; realised-vol ratios for a
  regime proxy.
- Trailing dollar volume and turnover, from the split-adjusted basis.
- Distance to trailing high/low over 20 and 60 sessions, from the
  **split-adjusted** basis — the two-price rule from phase 2 in practice.
- **Cross-sectional percentile ranks** rather than z-scores. Daily returns
  are fat-tailed; a z-score lets one 20-sigma day dominate the whole
  cross-section, whereas a percentile rank is bounded by construction and
  does not need a winsorisation parameter that itself needs justifying.
- Every feature written with an explicit `as_of` date and computed only
  from rows with `session_date <= as_of`.

**Exit criteria**

- 5.1 Every feature has a closed-form test on a hand-built series.
- 5.2 A **leakage test**: recomputing every feature on data truncated at date
  `d` reproduces the values it had at `d` in the full-sample run, bit for bit,
  for every `d` in a sample of dates. This is the criterion that matters; it
  is the one that catches the mistakes.
- 5.3 No feature uses `center=True`, an unshifted `rolling` on the target, or a
  full-sample mean or standard deviation. Enforced by a test, not by review.
- 5.4 Features for the full universe compute in under a second from the curated
  panel, so a parameter sweep is cheap.

---

## Phase 6 — Strategy layer as score tables

Three deliberately simple, well-documented short-horizon strategies. Each
one emits a **score per (date, symbol)** and nothing else — no positions,
no orders. Keeping signal separate from sizing is what makes them
comparable and testable.

1. **20-day momentum**, cross-sectionally ranked, skipping the most recent
   session to avoid the short-term reversal that contaminates it.
2. **5-day standardised reversal**: short-horizon mean reversion, scaled by
   trailing volatility so a 5% move on TLT and a 5% move on NVDA are not
   treated as the same event.
3. **20-day breakout**: distance to the trailing 20-session high on
   split-adjusted prices, a trend-following counterweight to (2).

**Exit criteria**

- 6.1 Each strategy is a pure function `(features, as_of) -> scores`, with a
  test on a hand-built cross-section where the intended ranking is obvious.
- 6.2 Every score for date `t` uses only information available at `t`'s close;
  covered by the phase 5 leakage test extended to scores.
- 6.3 Correlation between the three score series is reported. If they are 0.9
  correlated they are one strategy and the roadmap says so out loud.
- 6.4 Neither strategy has a parameter that was chosen by looking at the
  test-period result. Parameters are stated up front, in the config, with the
  reason.

---

## Phase 7 — Portfolio construction and the execution simulator

Where most backtests quietly become fiction.

**Work**

- Signal at `t`'s close → earliest fill at `t+1`'s **open**, at the **raw
  as-traded** price. Not the adjusted close, not `t`'s close.
- Cost model: commission plus slippage in basis points per side, applied to
  every fill, plus a **sensitivity sweep at 0, 5, 10 and 25 bp**. The
  sweep is the deliverable, not the single number: a strategy whose edge
  disappears at 10bp is a strategy that does not exist, and the only way to
  see that is to plot it.
- Equal-weight to start, with a maximum position weight and a cap on
  participation as a fraction of trailing average dollar volume. A backtest
  that buys 40% of a day's volume is measuring nothing.
- Explicit rebalance schedule and turnover accounting; report realised
  average holding period and check it against the intended 1–20 sessions.
- **No intraday stops.** If a stop is wanted it is evaluated on closes
  only, and the docs say why: a daily bar cannot order the low against the
  high.

**Exit criteria**

- 7.1 A test proves no fill ever uses a price from the session the signal was
  computed on.
- 7.2 A test proves every fill uses the raw as-traded basis, not an adjusted one.
- 7.3 The cost sweep is produced automatically and the breakeven cost level
  (the bp at which excess return hits zero) is reported as a headline number.
- 7.4 Realised turnover and average holding period are reported and fall inside
  the intended band, or the strategy is relabelled.
- 7.5 Position and participation caps are enforced, with a test that a signal
  demanding more is clipped rather than silently honoured.

---

## Phase 8 — Validation

Nothing here is allowed to touch the test period until the method is fixed.

**Work**

- **Chronological** 80/20 split. Never a random shuffle: shuffling daily
  data puts tomorrow in the training set and yesterday in the test set,
  which inflates everything and is undetectable from the output.
- All development, all parameter choices, all debugging on the training
  period. The test period is run once.
- Metric table: annualised return, annualised volatility, Sharpe, max
  drawdown, hit rate, average holding period, turnover, breakeven cost —
  train and test side by side, with the gap between them as the headline.
- Empirical quantiles for tail risk, not a normal approximation. A Gaussian
  VaR on daily equity returns understates the tail by a factor that shows
  up exactly when it matters.
- A benchmark comparison that includes the honest null: equal-weight
  buy-and-hold of the same universe, after the same costs.

**Exit criteria**

- 8.1 The split is chronological and the boundary date is recorded in the run
  manifest. A test fails if any test-period row appears in training.
- 8.2 Train and test metrics are reported together. A test-period Sharpe more
  than ~40% below train is reported as overfitting, in the output, not omitted.
- 8.3 The strategy is compared to equal-weight buy-and-hold after identical
  costs, and if it loses, the report says so on the first page.
- 8.4 The test period was run once, and the git history shows it.

---

## Phase 9 — Daily operation on paper

Paper trading, which is where the assumptions get audited by reality.

**Work**

- A scheduled daily job: ingest → clean → features → scores → target
  weights → orders, against Alpaca paper. Aborts loudly on any phase-4 SLA
  breach rather than trading on stale data.
- Reconciliation: expected fill price vs actual, expected position vs
  actual, every day, written to a table. This is the number that reveals
  whether the phase-7 cost model was honest.
- Divergence alerting: paper P&L vs the backtest's prediction for the same
  period. They will differ; the size and the sign of the difference is the
  finding.
- A one-page daily digest: positions, trades, the reconciliation delta, any
  quality flags on the bars that drove today's decisions.

**Exit criteria**

- 9.1 The job runs unattended for 20 consecutive sessions with no manual
  intervention.
- 9.2 Every order traces to a stored score, which traces to stored features,
  which trace to stored bars with a `source` and an `ingested_at_utc`.
- 9.3 Realised slippage is measured and compared to the assumed cost. If
  realised is worse, the backtest's cost assumption is raised to match and
  every headline number is regenerated.
- 9.4 Zero trades executed on stale or provisional data, proven from the logs.
- 9.5 No real money until 9.1–9.4 hold and the phase-8 test-period result
  survives the realised cost from 9.3.
