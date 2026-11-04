"""The base-chart benchmark works offline and reports missing installations."""

import subprocess
import sys
import importlib.util
import math
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


def test_tile_bounds_and_shift():
    view = benchmark_basechart.Viewport(47.6, -122.4, 8)
    tile = benchmark_basechart.selected_tile(view, 256)
    shifted = benchmark_basechart.selected_tile(view, 256, 1)
    x = math.floor(view._lon_to_x(view._center_lon) / 256) * 256
    y = math.floor(view._lat_to_y(view._center_lat) / 256) * 256
    top_left, bottom_right = tile.get_bbox()
    lat, lon = top_left.get_degrees()
    bottom, right = bottom_right.get_degrees()
    assert tile._lon_to_x(lon) == pytest.approx(x)
    assert tile._lat_to_y(lat) == pytest.approx(y)
    assert tile._lon_to_x(right) == pytest.approx(x + 256)
    assert tile._lat_to_y(bottom) == pytest.approx(y + 256)
    assert shifted._lon_to_x(shifted._center_lon) == pytest.approx(x + 384)


def test_grid_layout_non_multiple():
    cold = benchmark_basechart.grid_layout(912, 812, 256)
    warm = benchmark_basechart.grid_layout(912, 812, 256, 50)
    assert len(cold) == 16
    assert len(warm) == 20
    assert cold[-1] == (3, 3, 656, 768)
    assert warm[-1] == (4, 3, 862, 768)
    exposed = benchmark_basechart.grid_layout(1000, 812, 256, 50)
    assert len(exposed) == 20
    assert exposed[-1] == (4, 3, 950, 768)


@pytest.mark.parametrize("mode", ["--tile", "--grid"])
def test_synthetic_new_modes(mode):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--synthetic", "-n", "1", mode],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (
        "mode=tile" in result.stdout
        if mode == "--tile"
        else "mode=grid-warm-pan" in result.stdout
    )
