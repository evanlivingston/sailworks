"""The chart-reuse check's pure workload and verdict helpers."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "benchmark_chart_reuse.py"
spec = importlib.util.spec_from_file_location("benchmark_chart_reuse", SCRIPT)
chart_reuse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chart_reuse)


def _camera():
    from sailworks.qt.gribviewport import ViewportCamera

    return ViewportCamera(
        48.45,
        -124.29,
        9.38,
        1000,
        680,
        datetime(2026, 10, 27, tzinfo=timezone.utc),
        "very_dense",
        False,
    )


def test_panned_camera_moves_by_whole_world_pixels():
    camera = _camera()
    shifted = chart_reuse.panned_camera(camera, 200.0)
    width = 256 * 2**camera.zoom
    assert shifted.center_lon == camera.center_lon + 200.0 * 360.0 / width
    assert shifted.zoom == camera.zoom
    assert shifted.width == camera.width
    assert shifted.time == camera.time


def test_forecast_time_uses_the_opened_run_not_the_app_clock():
    fallback = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)
    paths = (
        "/data/gfs_20261026_12z_120h_aaaa.grib2",
        "/data/hrrr_20261026_17z_18h_bbbb.grib2",
    )
    assert chart_reuse.forecast_time(paths, fallback) == datetime(
        2026, 10, 27, tzinfo=timezone.utc
    )
    assert chart_reuse.forecast_time(("/data/plain.grib2",), fallback) == fallback


def _run(**overrides):
    values = {
        "runs": [
            {
                "label": "warm_full",
                "ms": 1000.0,
                "mode": "full",
                "composed_pixels": 1000,
                "timings": {"isobars_ms": 1.0},
            },
            {
                "label": "pan_200",
                "ms": 600.0,
                "mode": "regions",
                "composed_pixels": 100,
                "timings": {"isobars_ms": 1.0},
            },
            {
                "label": "time_only",
                "ms": 10.0,
                "mode": "reused",
                "composed_pixels": 0,
                "timings": {
                    "isobars_ms": 1.0,
                    "columns_ms": 1.0,
                    "copy_ms": 1.0,
                },
            },
            {
                "label": "reversal",
                "ms": 1.0,
                "mode": "reused",
                "composed_pixels": 0,
                "timings": {"isobars_ms": 1.0},
            },
        ],
        "workload": {"isobars": True},
        "counters": {"isobars": 2},
        "identity": {"match": True},
        "static": {"frames": 3},
    }
    values.update(overrides)
    return values


def test_the_declared_bounds_pass_on_a_representative_run():
    run = chart_reuse.verify(_run())
    assert run["passed"] is True
    assert run["pan_speedup"] == 0.4
    assert all(run["checks"].values())


def test_a_slow_or_incomplete_pan_fails_the_check():
    runs = _run()["runs"]
    runs[1] = dict(runs[1], ms=900.0, composed_pixels=900)
    run = chart_reuse.verify(_run(runs=runs))
    assert run["passed"] is False
    assert run["checks"]["pan_composed_fraction"] is False
    assert run["checks"]["pan_speedup"] is False


def test_a_time_change_that_rebuilds_chart_pixels_fails_the_check():
    runs = _run()["runs"]
    runs[2] = dict(runs[2], composed_pixels=10, mode="full")
    run = chart_reuse.verify(_run(runs=runs))
    assert run["checks"]["time_only_reuses_static"] is False
    assert run["passed"] is False


def test_report_accessors_are_bounded_and_absent_safe():
    run = _run()
    assert chart_reuse._run_ms(run, "pan_200") == 600.0
    assert chart_reuse._run_mode(run, "reversal") == "reused"
    assert chart_reuse._run_ms(run, "missing") is None
