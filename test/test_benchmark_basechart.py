"""The base-chart benchmark works offline and reports missing installations."""

import subprocess
import sys
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "benchmark_basechart.py"
spec = importlib.util.spec_from_file_location("benchmark_basechart", SCRIPT)
benchmark_basechart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark_basechart)


def test_synthetic_benchmark():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--synthetic", "-n", "2"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "n=2 min=" in result.stdout
    assert "mode=pan" in result.stdout
    assert "median=" in result.stdout and "mean=" in result.stdout


def test_missing_gshhs(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--gshhs", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "GSHHS data not installed" in result.stderr


@pytest.mark.parametrize("mode", ["--pan", "--static"])
def test_viewports_per_iteration(monkeypatch, mode):
    jobs = []
    renderers = []

    class Renderer:
        def __init__(self, callback):
            renderers.append(self)

        def _render(self, job):
            jobs.append(job)

    monkeypatch.setattr(benchmark_basechart, "BaseChartRenderer", Renderer)
    assert (
        benchmark_basechart.main(
            ["--synthetic", "-n", "4", mode, "--center-lon", "179.99"]
        )
        == 0
    )
    assert len(renderers) == 1
    assert len({id(job) for job in jobs}) == 4
    viewports = [job.viewport for job in jobs]
    if mode == "--pan":
        assert len({id(viewport) for viewport in viewports}) == 4
        assert len({viewport._center_lon for viewport in viewports}) == 4
        assert viewports[1]._center_lon < 0
    else:
        assert len({id(viewport) for viewport in viewports}) == 1
