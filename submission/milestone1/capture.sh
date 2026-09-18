#!/usr/bin/env bash
# Regenerate every file in this directory from real runs.
#
#   ./submission/milestone1/capture.sh
#
# Run from the repository root. Nothing here is transcribed or edited by
# hand: each file is the captured stdout+stderr of the command named in its
# header, so the evidence is reproducible rather than asserted. Re-running
# this overwrites all of it.
#
# `set -u` but deliberately NOT `set -e`: a failing command's output is
# itself evidence, and aborting the capture on the first non-zero exit would
# throw away the rest of the pack. Each step records its own exit code.

set -u

PY=./.venv/bin/python
PYTEST=./.venv/bin/pytest
OUT="submission/milestone1"

if [ ! -x "$PY" ]; then
    echo "expected a virtualenv at $PY — see README.md" >&2
    exit 2
fi

mkdir -p "$OUT/figures"

# Run a command, echo it into the file first so the file is self-describing,
# then record its exit code. Wall clock comes from `time -p` on the whole
# thing, because the interesting number for a submission is what the user
# waited for, not what a profiler attributes to Python.
capture() {
    local file="$1"; shift
    {
        echo "\$ $*"
        echo
        { time -p "$@" 2>&1 ; } 2>&1
        echo
        echo "[exit code: $?]"
    } > "$OUT/$file"
    echo "  wrote $OUT/$file"
}

echo "capturing evidence into $OUT/ ..."

# ---- 00 environment -------------------------------------------------
{
    echo '$ uname -srm && python --version && pip freeze'
    echo
    uname -srm
    "$PY" --version
    echo
    echo "--- installed packages (frozen) ---"
    "$PY" -m pip freeze
} > "$OUT/00_environment.txt"
echo "  wrote $OUT/00_environment.txt"

# ---- 01 the resolved config -----------------------------------------
capture 01_config.txt "$PY" -m marketengine config

# ---- 02 tests -------------------------------------------------------
# No network: proof the logic is tested independently of the vendor.
capture 02_tests.txt "$PYTEST" -q

# ---- 03 what ingest WOULD do ----------------------------------------
capture 03_ingest_dry_run.txt "$PY" -m marketengine ingest --dry-run

# ---- 04 cold run: full download -------------------------------------
# --refresh re-requests the entire window, so this is the honest cold-start
# number including every network round trip.
capture 04_run_cold.txt "$PY" -m marketengine run --refresh

# ---- 05 warm run: must be a no-op on the network --------------------
capture 05_run_warm.txt "$PY" -m marketengine run

# ---- 06 latency, throughput, freshness ------------------------------
# Warm: the steady-state daily path, where ingest is a no-op. This is the
# number that matters for "how long does the daily update take".
capture 06_latency_warm.txt "$PY" -m marketengine bench \
    --trials 7 --json "$OUT/06_latency_warm.json"

# Cold: --refresh re-downloads the whole eleven-year window, so the `ingest`
# stage timing here is a genuine 11-symbol download rather than a no-op.
capture 06_latency_cold.txt "$PY" -m marketengine bench \
    --refresh --trials 7 --json "$OUT/06_latency_cold.json"

# ---- 07 SQL over the curated panel ----------------------------------
capture 07_query.txt "$PY" -m marketengine query "
SELECT symbol,
       count(*)                          AS bars,
       min(date)                         AS first_bar,
       max(date)                         AS last_bar,
       sum(CASE WHEN filled THEN 1 ELSE 0 END)   AS forward_filled,
       sum(CASE WHEN flags <> '' THEN 1 ELSE 0 END) AS flagged_bars
FROM prices_baseline
GROUP BY symbol
ORDER BY symbol"

# ---- 08 data quality ------------------------------------------------
{
    echo '$ cat data/outputs/baseline/data_quality.csv  (rendered)'
    echo
    "$PY" - <<'PYEOF'
import pandas as pd
pd.set_option("display.width", 200, "display.max_columns", 60)
q = pd.read_csv("data/outputs/baseline/data_quality.csv")
print("--- per-symbol quality report ---")
print(q.to_string(index=False))
print()
flags = [c for c in q.columns if c.startswith("flag_")]
print("--- flag totals across the universe ---")
print(q[flags].sum().to_string())
print()
e = pd.read_csv("data/outputs/baseline/quality_events.csv")
print(f"--- quality event log: {len(e)} event(s) ---")
print(e.to_string(index=False) if len(e) else "(no events)")
PYEOF
} > "$OUT/08_data_quality.txt"
echo "  wrote $OUT/08_data_quality.txt"

# ---- 09 what was produced -------------------------------------------
{
    echo '$ ls -lh the generated outputs'
    echo
    echo "--- data/raw/yfinance ---";       ls -lh data/raw/yfinance
    echo; echo "--- data/curated/baseline ---"; ls -lh data/curated/baseline
    echo; echo "--- data/outputs/baseline ---"; ls -lh data/outputs/baseline
    echo; echo "--- reports/baseline ---";      ls -lh reports/baseline
    echo; echo "--- reports/baseline/figures ---"; ls -lh reports/baseline/figures
    echo; echo "--- total on-disk size ---";  du -sh data reports
} > "$OUT/09_outputs_listing.txt"
echo "  wrote $OUT/09_outputs_listing.txt"

# ---- 10 the run manifest --------------------------------------------
cp reports/baseline/run_manifest.json "$OUT/10_run_manifest.json"
echo "  wrote $OUT/10_run_manifest.json"

# ---- figures --------------------------------------------------------
cp reports/baseline/figures/*.png "$OUT/figures/"
cp reports/baseline/summary.md "$OUT/11_summary.md"
echo "  wrote $OUT/11_summary.md and $OUT/figures/*.png"

echo
echo "done. $OUT/README.md explains each artefact; $OUT/RUBRIC.md maps them"
echo "to the rubric. Neither is generated — they are written by hand and"
echo "reference the numbers above, so re-check them if a number moved."
