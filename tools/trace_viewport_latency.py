"""After-rendering latency trace for the standalone desktop viewport.

This separates input dispatch, camera/selection work, scene submission and the
Qt Quick ``afterRendering`` callback. It is a render-signal trace, not an
input-to-photon or presentation-FPS claim.

Each scenario runs in its own native process so the scene graph and shader are
not shared between measurements.
"""

import argparse
import importlib.util
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rendering_fixture", ROOT / "tools/benchmark_startup_rendering.py"
)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)

SCENARIOS = ("pan", "trackpad_pan", "wheel_zoom", "zoom_out")
SCHEMA = 1


def summary(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}

    def percentile(fraction):
        index = (len(ordered) - 1) * fraction
        lower = math.floor(index)
        upper = math.ceil(index)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    return {
        "count": len(ordered),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": ordered[-1],
    }


def environment(app, widget):
    import cairo
    import numpy as np
    import PySide6

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "host": platform.node(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "qt": PySide6.__version__,
        "numpy": np.__version__,
        "cairo": cairo.cairo_version_string(),
        "qt_platform": app.platformName(),
        "dpr": widget.devicePixelRatioF(),
        "refresh_rate_hz": app.primaryScreen().refreshRate(),
        "timezone": list(time.tzname),
    }


def worker(config):
    from datetime import datetime

    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

    from sailworks.core.storage import DATA_DIR
    from sailworks.qt.gribviewport import (
        GribViewportModel,
        ViewportCamera,
        _viewport_widget_class,
        saved_selection,
    )
    from sailworks.rendering.windrenderer import WindLandMask

    app = QApplication([])
    data = Path(DATA_DIR)
    paths, settings = saved_selection(data)
    model = GribViewportModel(
        paths,
        land_mask=WindLandMask(data),
        resident_time_window=True,
        grid=True,
    )
    widget = _viewport_widget_class()(
        model,
        gpu_weather_fill=True,
        chart_layer=True,
        enc_chart_layer=True,
        chart_without_weather=True,
        chart_data_dir=data,
        native_weather_fields=True,
    )
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget)
    container.resize(1000, 700)
    container.show()

    def pump(milliseconds=0):
        app.processEvents()
        if milliseconds > 0:
            loop = QEventLoop()
            QTimer.singleShot(milliseconds, loop.quit)
            loop.exec()

    def wait(predicate, label, timeout=60):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            pump()
            if predicate():
                return
            time.sleep(0.005)
        raise TimeoutError(label)

    camera = ViewportCamera(
        center_lat=48.38353666189077,
        center_lon=-124.38396048600592,
        zoom=10.038935750954801,
        width=widget.width(),
        height=widget.height(),
        time=datetime.fromisoformat(config["forecast_time"]),
        density=settings.get("gribBarbDensity", "dense"),
        over_land=settings.get("gribArrowOnGround", False),
    )
    widget.set_camera(camera)
    wait(lambda: widget._state == "ready", "viewport ready")

    stages = {"set_camera_ms": [], "model_update_ms": [], "publish_ms": []}
    last = {name: 0.0 for name in stages}

    def instrument(name, owner, method):
        original = getattr(owner, method)

        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                last[name] = elapsed
                stages[name].append(elapsed)

        setattr(owner, method, wrapped)

    instrument("set_camera_ms", widget, "set_camera")
    instrument("model_update_ms", model, "update")
    instrument("publish_ms", widget, "_publish")

    state = {"revision": -1, "synchronizing": None}
    dispatched, rendered = {}, {}
    quick = widget.quickWindow()
    quick.beforeSynchronizing.connect(
        lambda: state.__setitem__("synchronizing", state["revision"]),
        Qt.ConnectionType.DirectConnection,
    )

    def rendered_callback():
        revision = state["synchronizing"]
        if revision is not None and revision not in rendered:
            rendered[revision] = time.perf_counter()

    quick.afterRendering.connect(rendered_callback, Qt.ConnectionType.DirectConnection)

    frames = config["frames"]
    interval = config["interval_ms"] / 1000.0
    handler_ms, dispatch_late_ms = [], []
    per_input = {name: [] for name in stages}
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    drain = QTimer()
    drain.setSingleShot(True)
    drain.timeout.connect(loop.quit)
    scenario = config["scenario"]
    center_x, center_y = widget.width() / 2, widget.height() / 2

    def dispatch():
        revision = state["revision"] + 1
        state["revision"] = revision
        intended = origin + revision * interval
        dispatched[revision] = time.perf_counter()
        dispatch_late_ms.append((dispatched[revision] - intended) * 1000)
        for name in stages:
            last[name] = 0.0
        started = time.perf_counter()
        if scenario == "pan":
            widget.pan_pixels(-8, 0)
        elif scenario == "trackpad_pan":
            widget.pan_pixels(-4, 0)
        elif scenario == "wheel_zoom":
            widget.zoom_at(
                0.02 if revision < frames // 2 else -0.02, center_x, center_y
            )
        elif scenario == "zoom_out":
            widget.zoom_at(-0.02, center_x, center_y)
        handler_ms.append((time.perf_counter() - started) * 1000)
        for name in stages:
            per_input[name].append(last[name])
        if revision + 1 == frames:
            drain.start(1000)
        else:
            timer.start(
                max(
                    0,
                    round(
                        (origin + (revision + 1) * interval - time.perf_counter())
                        * 1000
                    ),
                )
            )

    for _ in range(config["warmup_frames"]):
        pump(20)
    origin = time.perf_counter()
    timer.timeout.connect(dispatch)
    timer.start(0)
    loop.exec()
    pump(100)

    latency = [
        (rendered[revision] - dispatched[revision]) * 1000
        for revision in dispatched
        if revision in rendered
    ]
    return {
        "scenario": scenario,
        "environment": environment(app, widget),
        "inputs": len(dispatched),
        "observed_after_rendering": len(latency),
        "samples": {
            "input_to_after_rendering_ms": latency,
            "dispatch_late_ms": dispatch_late_ms,
            "handler_ms": handler_ms,
            **per_input,
        },
        "summary": {
            "input_to_after_rendering_ms": summary(latency),
            "dispatch_late_ms": summary(dispatch_late_ms),
            "handler_ms": summary(handler_ms),
            **{name: summary(values) for name, values in per_input.items()},
        },
    }


def run(args):
    snapshot = args.snapshot.resolve(strict=True)
    manifest = fixture.verify_snapshot(snapshot)
    scenarios = args.scenarios.split(",")
    if any(name not in SCENARIOS for name in scenarios) or len(set(scenarios)) != len(
        scenarios
    ):
        raise ValueError(f"Choose unique scenarios from {SCENARIOS}")
    workload = {
        "snapshot_sha256": manifest["content_sha256"],
        "forecast_time": manifest["forecast_time"],
        "scenarios": scenarios,
        "frames": args.frames,
        "repeats": args.repeats,
        "warmup_frames": args.warmup_frames,
        "interval_ms": args.interval_ms,
        "mode": "afterRendering-trace-v1",
    }
    report = {"schema_version": SCHEMA, "workload": workload, "runs": []}
    for repeat in range(args.repeats):
        for scenario in scenarios:
            with tempfile.TemporaryDirectory(prefix="gwr-latency-") as directory:
                root = Path(directory)
                fixture.prepare_sandbox(snapshot, root)
                config = {
                    **workload,
                    "scenario": scenario,
                    "repeats": 1,
                }
                config_path, result_path = root / "config.json", root / "result.json"
                config_path.write_text(json.dumps(config))
                env = dict(
                    os.environ,
                    HOME=str(root / "home"),
                    XDG_DATA_HOME=str(root / "xdg-data"),
                    XDG_CONFIG_HOME=str(root / "xdg-config"),
                    XDG_CACHE_HOME=str(root / "xdg-cache"),
                    PYTHONHASHSEED="0",
                    PYTHONPATH=str(ROOT),
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(config_path),
                        "--output",
                        str(result_path),
                    ],
                    env=env,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=args.worker_timeout_s,
                )
                if result.returncode:
                    diagnostics = Path(
                        str(args.output).replace(".json", ".failure.log")
                    )
                    diagnostics.write_text(result.stdout + result.stderr)
                    raise RuntimeError(
                        f"Worker exited {result.returncode}; diagnostics: {diagnostics}"
                    )
                measured = json.loads(result_path.read_text())
                report.setdefault("environment", measured.pop("environment"))
                measured["repeat"] = repeat
                report["runs"].append(measured)
                print(
                    f"{scenario} [{repeat + 1}/{args.repeats}]: "
                    f"p95 {measured['summary']['input_to_after_rendering_ms']['p95_ms']:.1f} ms",
                    file=sys.stderr,
                )
    report["complete"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup-frames", type=int, default=5)
    parser.add_argument("--interval-ms", type=float, default=16.667)
    parser.add_argument("--worker-timeout-s", type=float, default=600)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        result = worker(json.loads(args.worker.read_text()))
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        return
    if args.snapshot is None or args.output is None:
        parser.error("--snapshot and --output are required")
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
