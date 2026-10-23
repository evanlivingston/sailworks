"""Isolated Qt rendering baselines. See docs/history/rendering-performance.md."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
SCENARIOS = (
    "empty_pan",
    "navigation_pan",
    "reverse_pan",
    "zoom_out",
    "wind_pan",
    "log_pan",
    "combined_pan",
)
DEFAULT_SCENE = {
    "version": 1,
    "center": [48.4, -123.4],
    "zoom": 9.0,
    "time": "2026-10-15T12:00:00",
    "navigation_points": 10000,
    "log_points": 100000,
    "wind_side": 316,
    "datasets": [],
}


def summary(values):
    """Linear-interpolated percentiles, with budget counts and raw samples elsewhere."""
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}

    def percentile(fraction):
        index = (len(ordered) - 1) * fraction
        lower = math.floor(index)
        upper = math.ceil(index)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    return {
        "count": len(values),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": ordered[-1],
        "over_16_7_ms": sum(v > 16.7 for v in values),
        "over_33_3_ms": sum(v > 33.3 for v in values),
        "over_50_ms": sum(v > 50 for v in values),
    }


def read_scene(path=None):
    scene = dict(DEFAULT_SCENE)
    if path:
        supplied = json.loads(Path(path).read_text())
        unknown = set(supplied) - set(scene)
        if unknown:
            raise ValueError(f"Unknown scene keys: {sorted(unknown)}")
        scene.update(supplied)
    if scene["version"] != 1:
        raise ValueError("Unsupported scene version")
    for key in ("navigation_points", "log_points", "wind_side"):
        if type(scene[key]) is not int or scene[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    lat, lon = scene["center"]
    if not (-85 <= lat <= 85 and -180 <= lon <= 180 and 1 <= scene["zoom"] <= 18):
        raise ValueError("Invalid scene center or zoom")
    from datetime import datetime

    instant = datetime.fromisoformat(scene["time"])
    if instant.tzinfo is not None:
        raise ValueError("Scene time must be naive; replay uses TZ=UTC")
    datasets = []
    for entry in scene["datasets"]:
        if entry["kind"] not in ("vector", "grib", "auxiliary"):
            raise ValueError("Dataset kind must be vector, grib, or auxiliary")
        file = Path(entry["path"]).expanduser()
        if not file.is_absolute():
            file = Path(path).resolve().parent / file
        file = file.resolve(strict=True)
        digest = file_hash(file)
        if digest != entry["sha256"]:
            raise ValueError(f"Dataset checksum mismatch: {file}")
        datasets.append(dict(kind=entry["kind"], path=str(file), sha256=digest))
    scene["datasets"] = datasets
    return scene


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scene_identity(scene):
    # Locations may differ across checkouts; content and order must not.
    identity = dict(scene)
    identity["datasets"] = [
        {k: entry[k] for k in ("kind", "sha256")} for entry in scene["datasets"]
    ]
    return identity


def revision():
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "tracked_diff_sha256": hashlib.sha256(
            git("diff", "HEAD", "--binary").encode()
        ).hexdigest(),
        "harness_sha256": file_hash(__file__),
    }


def compare_reports(before, after):
    if before["schema_version"] != SCHEMA or after["schema_version"] != SCHEMA:
        raise ValueError("Incompatible schema_version")
    for key in ("schema_version", "workload", "environment"):
        if before[key] != after[key]:
            raise ValueError(f"Incompatible {key}; refusing a misleading comparison")
    if before["revision"]["harness_sha256"] != after["revision"]["harness_sha256"]:
        raise ValueError("Incompatible measurement harness")
    rows = []
    for scenario in before["workload"]["scenarios"]:
        a = [r for r in before["runs"] if r["scenario"] == scenario]
        b = [r for r in after["runs"] if r["scenario"] == scenario]
        repeats = before["workload"]["repeats"]
        if len(a) != repeats or len(b) != repeats:
            raise ValueError(f"Missing or mismatched repetitions: {scenario}")
        if any(
            sorted(r["repeat"] for r in runs) != list(range(repeats)) for runs in (a, b)
        ):
            raise ValueError(f"Duplicate or missing repetition IDs: {scenario}")
        for run in a + b:
            if len(run["samples"]["grab_ms"]) != before["workload"]["frames"]:
                raise ValueError(f"Incomplete frame samples: {scenario}")
        old = summary([v for run in a for v in run["samples"]["grab_ms"]])
        new = summary([v for run in b for v in run["samples"]["grab_ms"]])
        rows.append(
            {
                "scenario": scenario,
                "before": old,
                "after": new,
                "queued_callback_before": summary(
                    [v for run in a for v in run["samples"]["queued_callback_ms"]]
                ),
                "queued_callback_after": summary(
                    [v for run in b for v in run["samples"]["queued_callback_ms"]]
                ),
                "resources_before": resource_summary(a),
                "resources_after": resource_summary(b),
                "p99_change_percent": (
                    100 * (new["p99_ms"] / old["p99_ms"] - 1) if old["p99_ms"] else None
                ),
            }
        )
    return {"schema_version": SCHEMA, "comparisons": rows}


def resource_summary(runs):
    return {
        "peak_rss_bytes": max(r["process_peak_rss_bytes"] for r in runs),
        "peak_tracked_surface_bytes": max(
            max(r["tracked_surface_bytes_per_frame"]) for r in runs
        ),
        "base_rebuilds": sum(
            frame["_base_image"] for run in runs for frame in run["rebuilds_per_frame"]
        ),
        "navigation_rebuilds": sum(
            frame["_navigation_image"]
            for run in runs
            for frame in run["rebuilds_per_frame"]
        ),
        "post_gesture_worker_idle": summary(
            [r["post_gesture_worker_idle_ms"] for r in runs]
        ),
    }


def tracked_surface_bytes(canvas):
    """Known retained image buffers only, not total memory or temporary copies."""
    seen = set()
    total = 0

    def add(image):
        nonlocal total
        if image is None:
            return
        if hasattr(image, "cacheKey"):
            key, size = ("qt", image.cacheKey()), image.sizeInBytes()
        else:
            key, size = ("cairo", id(image)), image.get_stride() * image.get_height()
        if key not in seen:
            seen.add(key)
            total += size

    for cache in (canvas._base_cache, canvas._nav_cache, canvas._dash_cache):
        if cache:
            add(cache[-1])
    cached = canvas.charts._chart_surfaces.get(canvas)
    if cached:
        add(cached[-1])
    for renderer in (
        canvas.wind,
        canvas.isochrones,
        canvas.marine._renderer,
        canvas.charts._enc_renderer,
    ):
        state = renderer._states.get(canvas)
        if state and state.frame:
            add(state.frame[1])
    for surface, _modified in canvas.charts._chs_renderer._tiles.values():
        add(surface)
    return total


def worker(config):
    """Runs only in a fresh process with a disposable HOME and blocked sockets."""
    import resource
    from datetime import datetime
    from types import SimpleNamespace

    import cairo
    import numpy as np
    import PySide6
    from osgeo import gdal
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    # Import application code only AFTER the parent has isolated its storage paths.
    from gweatherrouting.core.geo.track import Track
    from gweatherrouting.core.mapviewport import MapViewport
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.datacontroller import read_forecast
    from gweatherrouting.qt.mapcanvas import (
        TrackMap,
    )  # TODO #86: legacy CPU-rendering benchmark; retarget or replace before deleting mapcanvas.py
    from gweatherrouting.rendering.overlayrenderer import RenderJob, RenderState

    def no_network(*_args, **_kwargs):
        raise RuntimeError("Network is disabled in rendering benchmarks")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    scene, scenario = config["scene"], config["scenario"]
    app = QApplication([])
    controller = SessionController(load_charts=False)
    controller.timer.stop()
    controller.settings.canadianCharts = False
    controller.settings.gribArrowOnGround = True
    controller.settings.panInertia = False
    controller.settings.gribIsobarsVisible = bool(scene["datasets"])
    controller.time_control.set_time(datetime.fromisoformat(scene["time"]))
    canvas = TrackMap(
        controller
    )  # TODO #86: legacy CPU-rendering benchmark; retarget or replace before deleting mapcanvas.py
    canvas.resize(config["width"], config["height"])
    canvas._center_lat, canvas._center_lon = scene["center"]
    canvas._zoom = scene["zoom"]
    canvas.dashboard_visible = False
    # Coverage notices test a separate sampler and can offer downloads. Exclude
    # them explicitly; benchmark geometry/loading rather than notice estimation.
    canvas._check_weather_coverage = lambda: None
    canvas.marine.visible = False
    synthetic_wind = scenario in ("wind_pan", "combined_pan")
    if synthetic_wind and any(d["kind"] == "grib" for d in scene["datasets"]):
        controller.close()
        raise ValueError("Synthetic wind scenarios cannot also load real GRIBs")

    if scenario in ("navigation_pan", "reverse_pan", "zoom_out", "combined_pan"):
        n = scene["navigation_points"]
        lat, lon = scene["center"]
        controller.core.trackManager.append(
            Track(
                "Benchmark track",
                [(lat + 0.5 * i / n, lon + 0.8 * i / n, None) for i in range(n)],
            )
        )
    if scenario in ("log_pan", "combined_pan"):
        n = scene["log_points"]
        lat, lon = scene["center"]
        canvas.set_points(
            [
                SimpleNamespace(lat=lat + 0.5 * i / n, lon=lon + 0.8 * i / n)
                for i in range(n)
            ],
            (),
        )
    for entry in scene["datasets"]:
        if entry["kind"] == "vector":
            controller.charts.load_vector_layer(entry["path"])
        elif entry["kind"] == "grib":
            controller.core.grib_manager.gribs.append(read_forecast(entry["path"]))

    if synthetic_wind:
        side = scene["wind_side"]
        lat, lon = scene["center"]
        lons, lats = np.meshgrid(
            np.linspace(lon - 2, lon + 2, side),
            np.linspace(lat - 2, lat + 2, side),
        )
        style = canvas._wind_style_key()[:8]
        key = (style, canvas.get_scale_factor(), ())
        job = RenderJob(key, MapViewport(canvas), (), canvas.get_scale_factor(), style)
        job.barb_flat = (
            np.full(side * side, 225.0),
            np.full(side * side, 10.0),
            lats.ravel(),
            lons.ravel(),
        )
        # Supply a completed retained field. Background fill/sampling is NOT
        # measured in these two synthetic scenarios; live geometry is real.
        canvas.wind.draw = lambda *_args, **_kwargs: None
        canvas.wind._states[canvas] = RenderState(key=key, frame=(job, None))

    samples = {
        key: [] for key in ("grab_ms", "queued_callback_ms", "start_interval_ms")
    }
    stages = {
        key: []
        for key in (
            "_base_image",
            "_navigation_image",
            "_paint_wind_barbs",
            "_paint_track_overlay",
        )
    }
    rebuilds = []
    surface_bytes = []

    def instrument(name, original):
        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                stages[name].append((time.perf_counter() - started) * 1000)

        return wrapped

    def grab():
        started = time.perf_counter()
        canvas.grab()
        return (time.perf_counter() - started) * 1000

    def wait_workers():
        deadline = time.perf_counter() + config["settle_timeout_s"]
        while True:
            app.processEvents()
            renderers = (canvas.wind, canvas.charts._enc_renderer, canvas.isochrones)
            if not any(r._running or r._pending for r in renderers):
                return
            if time.perf_counter() >= deadline:
                raise TimeoutError("Overlay preparation did not settle")
            time.sleep(0.001)

    try:
        # First paint has application-cold caches, not cold OS/filesystem caches.
        cold_ms = grab()
        wait_workers()
        canvas.show()
        app.processEvents()
        grab()
        wait_workers()
        for _ in range(config["warmup_frames"]):
            grab()
            app.processEvents()
        for key in stages:
            setattr(canvas, key, instrument(key, getattr(canvas, key)))
        canvas.drag = SimpleNamespace()  # Defer worker publication like a gesture.
        canvas._zoom_animating = scenario == "zoom_out"
        if canvas._zoom_animating:
            canvas._zoom_anchor = (
                *scene["center"],
                canvas.width() / 2,
                canvas.height() / 2,
            )
        last_start = None
        for index in range(config["frames"]):
            started = time.perf_counter()
            if last_start is not None:
                samples["start_interval_ms"].append((started - last_start) * 1000)
            last_start = started
            if scenario == "zoom_out":
                canvas._zoom = max(1, scene["zoom"] - (index + 1) / config["frames"])
                canvas._recenter_on_anchor()
            else:
                direction = (
                    -1
                    if scenario == "reverse_pan" and index >= config["frames"] // 2
                    else 1
                )
                canvas._pan_pixels(-8 * direction, 0)
            # Queue before synchronous painting, then observe event-loop service.
            QTimer.singleShot(
                0,
                lambda posted=time.perf_counter(): samples["queued_callback_ms"].append(
                    (time.perf_counter() - posted) * 1000
                ),
            )
            before = {
                key: len(stages[key]) for key in ("_base_image", "_navigation_image")
            }
            samples["grab_ms"].append(grab())
            app.processEvents()
            rebuilds.append({key: len(stages[key]) - before[key] for key in before})
            surface_bytes.append(tracked_surface_bytes(canvas))
            remaining = config["interval_ms"] / 1000 - (time.perf_counter() - started)
            if remaining > 0:
                # Service Qt while pacing; this is NOT a native presentation clock.
                until = time.perf_counter() + remaining
                while time.perf_counter() < until:
                    app.processEvents()
                    time.sleep(min(0.001, max(0, until - time.perf_counter())))
        canvas.drag = None
        canvas._zoom_animating = False
        canvas._zoom_anchor = None
        canvas._refresh_after_gesture()
        started = time.perf_counter()
        grab()
        wait_workers()
        grab()
        settle_ms = (time.perf_counter() - started) * 1000
        high_water = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {
            "scenario": scenario,
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "host": platform.node(),
                "cpu_count": os.cpu_count(),
                "python": platform.python_version(),
                "qt": PySide6.__version__,
                "numpy": np.__version__,
                "cairo": cairo.cairo_version_string(),
                "gdal": gdal.VersionInfo("RELEASE_NAME"),
                "qt_platform": app.platformName(),
                "dpr": canvas.devicePixelRatioF(),
            },
            "actual_viewport": [canvas.width(), canvas.height()],
            "cold_first_grab_ms": cold_ms,
            "post_gesture_worker_idle_ms": settle_ms,
            "samples": samples,
            "summary": {key: summary(values) for key, values in samples.items()},
            "stages_ms": stages,
            "rebuilds_per_frame": rebuilds,
            "tracked_surface_bytes_per_frame": surface_bytes,
            "process_peak_rss_bytes": high_water
            * (1 if sys.platform == "darwin" else 1024),
        }
    finally:
        controller.close()
        canvas.close()
        app.processEvents()


def run(args):
    if sys.platform == "win32":
        raise ValueError(
            "Storage sandbox and RSS measurement currently support macOS/Linux"
        )
    scene = read_scene(args.manifest)
    scenarios = args.scenarios.split(",")
    if any(s not in SCENARIOS for s in scenarios) or len(set(scenarios)) != len(
        scenarios
    ):
        raise ValueError(f"Choose unique scenarios from {SCENARIOS}")
    if any(d["kind"] == "grib" for d in scene["datasets"]) and set(scenarios) & {
        "wind_pan",
        "combined_pan",
    }:
        raise ValueError(
            "Select non-synthetic-wind scenarios when supplying GRIB datasets"
        )
    workload = {
        "scene": scene_identity(scene),
        "scenarios": scenarios,
        **{
            key: getattr(args, key)
            for key in (
                "frames",
                "repeats",
                "warmup_frames",
                "width",
                "height",
                "scale",
                "interval_ms",
                "settle_timeout_s",
            )
        },
        "trajectory_version": 1,
        "mode": "offscreen" if args.offscreen else "native-grab",
    }
    report = {
        "schema_version": SCHEMA,
        "revision": revision(),
        "workload": workload,
        "runs": [],
    }
    for repeat in range(args.repeats):
        for scenario in scenarios:
            with tempfile.TemporaryDirectory(prefix="gwr-render-") as directory:
                root = Path(directory)
                config = {**workload, "scene": scene, "scenario": scenario}
                config_path, result_path = root / "config.json", root / "result.json"
                config_path.write_text(json.dumps(config))
                environment = dict(os.environ)
                environment.update(
                    HOME=str(root / "home"),
                    XDG_DATA_HOME=str(root / "data"),
                    XDG_CONFIG_HOME=str(root / "config"),
                    XDG_CACHE_HOME=str(root / "cache"),
                    TZ="UTC",
                    PYTHONHASHSEED="0",
                    QT_SCALE_FACTOR=str(args.scale),
                    QT_STYLE_OVERRIDE="Fusion",
                    PYTHONPATH=str(ROOT),
                )
                if args.offscreen:
                    environment["QT_QPA_PLATFORM"] = "offscreen"
                else:
                    environment.pop("QT_QPA_PLATFORM", None)
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(config_path),
                        "--output",
                        str(result_path),
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=args.worker_timeout_s,
                )
                if completed.returncode:
                    raise RuntimeError(completed.stdout + completed.stderr)
                result = json.loads(result_path.read_text())
                environment = result.pop("environment")
                if "environment" in report and report["environment"] != environment:
                    raise ValueError("Environment changed between runs")
                report["environment"] = environment
                result["repeat"] = repeat
                report["runs"].append(result)
                print(
                    f"{scenario} [{repeat + 1}/{args.repeats}]: p99 grab {result['summary']['grab_ms']['p99_ms']:.1f} ms",
                    file=sys.stderr,
                )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup-frames", type=int, default=5)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--scale", type=int, choices=(1, 2), default=1)
    parser.add_argument("--interval-ms", type=float, default=0)
    parser.add_argument("--settle-timeout-s", type=float, default=30)
    parser.add_argument("--worker-timeout-s", type=float, default=300)
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.compare:
            report = compare_reports(*(json.loads(p.read_text()) for p in args.compare))
        elif args.worker:
            report = worker(json.loads(args.worker.read_text()))
        else:
            if min(args.frames, args.repeats, args.width, args.height) < 1:
                parser.error("frames, repeats, width and height must be positive")
            if (
                args.warmup_frames < 0
                or not math.isfinite(args.interval_ms)
                or args.interval_ms < 0
            ):
                parser.error("warmup frames and interval must be nonnegative")
            if not all(
                math.isfinite(v) and v > 0
                for v in (args.settle_timeout_s, args.worker_timeout_s)
            ):
                parser.error("timeouts must be finite and positive")
            report = run(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Rendering benchmark failed: {exc}\n")


if __name__ == "__main__":
    main()
