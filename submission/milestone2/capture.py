"""Regenerate the Milestone 2 evidence pack from real runs.

Run from the repository root, with the project virtualenv:

    .venv\\Scripts\\python.exe submission/milestone2/capture.py

The curated panel has to exist already (`python -m marketengine run`
once). This script does not download anything. It records pytest, the
resolved config, and `backtest`, then copies the report and the figures
into this directory. Nothing here is transcribed.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PY = Path(sys.executable)


def capture(filename: str, args: list[str]) -> None:
    shown = " ".join(args)
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    text = "\n".join([
        f"$ {shown}",
        "",
        proc.stdout,
        proc.stderr,
        f"[exit code: {proc.returncode}]",
        "",
    ])
    (OUT / filename).write_text(text, encoding="utf-8")
    print(f"  wrote {OUT.name}/{filename}  (exit {proc.returncode})")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "figures").mkdir(exist_ok=True)
    print(f"capturing evidence into {OUT.relative_to(ROOT)} ...")

    env = "\n".join([
        "$ python -c platform, version; pip freeze",
        "",
        platform.platform(),
        f"python {sys.version}",
        "",
        "--- installed packages (frozen) ---",
        subprocess.run([str(PY), "-m", "pip", "freeze"], cwd=ROOT,
                       capture_output=True, text=True).stdout,
    ])
    (OUT / "00_environment.txt").write_text(env, encoding="utf-8")
    print("  wrote 00_environment.txt")

    capture("01_config.txt", [str(PY), "-m", "marketengine", "config"])
    capture("02_tests.txt", [str(PY), "-m", "pytest", "-q", "--tb=line"])
    capture("03_backtest.txt", [str(PY), "-m", "marketengine", "backtest"])

    report = ROOT / "reports" / "baseline" / "backtest.md"
    if report.exists():
        shutil.copyfile(report, OUT / "04_backtest.md")
        print("  wrote 04_backtest.md")
    figures = ROOT / "reports" / "baseline" / "figures"
    for name in ("backtest_equity.png", "backtest_drawdown.png", "backtest_costs.png"):
        src = figures / name
        if src.exists():
            shutil.copyfile(src, OUT / "figures" / name)
            print(f"  wrote figures/{name}")

    summary = ROOT / "data" / "outputs" / "baseline" / "backtest_summary.csv"
    grid = ROOT / "data" / "outputs" / "baseline" / "backtest_cost_grid.csv"
    corr = ROOT / "data" / "outputs" / "baseline" / "backtest_score_corr.csv"
    for src, dest in (
        (summary, "05_summary.csv"),
        (grid, "06_cost_grid.csv"),
        (corr, "07_score_corr.csv"),
    ):
        if src.exists():
            shutil.copyfile(src, OUT / dest)
            print(f"  wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
