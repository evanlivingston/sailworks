"""The base-chart benchmark works offline and reports missing installations."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "benchmark_basechart.py"


def test_synthetic_benchmark():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--synthetic", "-n", "2"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "n=2 min=" in result.stdout
    assert "median=" in result.stdout and "mean=" in result.stdout


def test_missing_gshhs(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--gshhs", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "GSHHS data not installed" in result.stderr
