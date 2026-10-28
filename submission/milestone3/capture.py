"""Regenerate the Milestone 3 evidence pack from real runs.

Run from the repository root:

    .venv\\Scripts\\python.exe submission/milestone3/capture.py

Expects the validation panel to already exist:

    python -m marketengine --config config/validation.yml run
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
CFG = ["--config", "config/validation.yml"]
REPORTS = ROOT / "reports" / "validation"
OUTPUTS = ROOT / "data" / "outputs" / "validation"


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

    capture("01_config.txt", [str(PY), "-m", "marketengine", *CFG, "config"])
    capture("02_tests.txt", [str(PY), "-m", "pytest", "-q", "--tb=line"])
    capture("03_validate.txt", [str(PY), "-m", "marketengine", *CFG, "validate"])
    capture("03b_search.txt", [str(PY), "-m", "marketengine", *CFG, "search"])

    for src_name, dest in (
        ("validation.md", "04_validation.md"),
        ("backtest.md", "05_backtest_fullsample.md"),
        ("search.md", "05b_search.md"),
    ):
        src = REPORTS / src_name
        if src.exists():
            shutil.copyfile(src, OUT / dest)
            print(f"  wrote {dest}")

    for name in (
        "validation_train_equity.png",
        "validation_test_equity.png",
        "validation_train_test_cagr.png",
        "validation_test_costs.png",
    ):
        src = REPORTS / "figures" / name
        if src.exists():
            shutil.copyfile(src, OUT / "figures" / name)
            print(f"  wrote figures/{name}")

    for src_name, dest in (
        ("validation_comparison.csv", "06_comparison.csv"),
        ("validation_overfit_flags.csv", "07_overfit_flags.csv"),
        ("validation_cost_grid.csv", "08_cost_grid.csv"),
        ("validation_split.csv", "09_split.csv"),
        ("validation_train_summary.csv", "10_train_summary.csv"),
        ("validation_test_summary.csv", "11_test_summary.csv"),
        ("search_trials.csv", "12_search_trials.csv"),
        ("search_summary.csv", "13_search_summary.csv"),
    ):
        src = OUTPUTS / src_name
        if src.exists():
            shutil.copyfile(src, OUT / dest)
            print(f"  wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
