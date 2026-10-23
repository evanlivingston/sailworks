"""CPU/Metal retained-map feasibility experiment; see docs/history/gpu-rendering-experiment.md."""

import argparse
import importlib.util
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


def load_tool(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


startup = load_tool("benchmark_startup_rendering")
ROOT = Path(__file__).resolve().parents[1]
MODES = ("cpu_current", "cpu_batched", "gpu_mesh")
SCENARIOS = ("stationary", "pan", "zoom")


def disable_external_access():
    def blocked(*_args, **_kwargs):
        raise RuntimeError("Network disabled in GPU experiment")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    from gweatherrouting.core.marine.credentials import SystemCredentialStore

    SystemCredentialStore.load = lambda _self: None


def save_fixture(directory, metadata, base, navigation, flat):
    import numpy as np

    directory.mkdir(parents=True, exist_ok=False)
    if not base.save(str(directory / "base.png")) or not navigation.save(
        str(directory / "navigation.png")
    ):
        raise RuntimeError("Unable to export retained imagery")
    np.savez_compressed(
        directory / "wind.npz",
        directions=flat[0],
        speeds=flat[1],
        latitudes=flat[2],
        longitudes=flat[3],
    )
    metadata["files"] = startup.inventory(directory)
    metadata["fixture_sha256"] = startup.digest(metadata)
    (directory / "scene.json").write_text(json.dumps(metadata, indent=2) + "\n")


def verify_fixture(directory):
    metadata = json.loads((directory / "scene.json").read_text())
    if metadata["fixture_version"] != 1:
        raise ValueError("Unsupported GPU fixture")
    expected = metadata.pop("fixture_sha256")
    if startup.digest(metadata) != expected:
        raise ValueError("Fixture metadata checksum mismatch")
    for entry in metadata["files"]:
        if startup.protocol.file_hash(directory / entry["path"]) != entry["sha256"]:
            raise ValueError("Fixture imagery or wind data changed")
    metadata["fixture_sha256"] = expected
    return metadata


def export_worker(config):
    disable_external_access()
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.window import PreviewWindow
    from gweatherrouting.qt.theme import apply_theme
    from datetime import datetime

    app = QApplication([])
    apply_theme(app)
    controller = SessionController()
    controller.timer.stop()
    controller.time_control.set_time(datetime.fromisoformat(config["forecast_time"]))
    chs = controller.charts._chs_renderer

    def cached_fetch(key):
        cached = chs.source.cached(key)
        if cached is None:
            raise ValueError("Tile absent from captured cache")
        return cached

    chs.source.fetch = cached_fetch
    chs.set_enabled(controller.settings.canadianCharts)
    window = PreviewWindow(controller)
    canvas = (
        window.map
        if window.views.tabText(window.views.currentIndex()) == "Charts"
        else window.log_map
    )
    renderers = (
        canvas.wind,
        canvas.marine._renderer,
        canvas.isochrones,
        controller.charts._enc_renderer,
    )
    try:
        window.show()
        canvas.grab()
        deadline = time.perf_counter() + 180
        quiet = None
        while time.perf_counter() < deadline:
            app.processEvents()
            busy = (
                any(r._running or r._pending for r in renderers)
                or chs._inflight
                or chs._pending
                or chs._ticks
                or canvas.weather_coverage.running
                or canvas.weather_coverage.pending
            )
            if busy:
                quiet = None
            elif quiet is None:
                quiet = time.perf_counter()
            elif time.perf_counter() - quiet > 0.2:
                break
            time.sleep(0.001)
        else:
            raise TimeoutError("Startup capture did not settle")
        canvas.grab()
        state = canvas.wind._states.get(canvas)
        if not canvas.wind_visible or not state or not state.frame:
            raise ValueError("Saved map must have a completed visible wind frame")
        job, _surface = state.frame
        flat = getattr(job, "barb_flat", None)
        if flat is None or not len(flat[0]) or controller.chart_errors:
            raise ValueError("Missing wind samples or failed startup charts")
        metadata = {
            "fixture_version": 1,
            "source": "saved-startup",
            "snapshot_sha256": config["snapshot_sha256"],
            "forecast_time": config["forecast_time"],
            "width": canvas.width(),
            "height": canvas.height(),
            "dpr": canvas.devicePixelRatioF(),
            "latitude": canvas._center_lat,
            "longitude": canvas._center_lon,
            "zoom": canvas._zoom,
            "density": controller.settings.gribBarbDensity,
            "opacity": float(job.palette[1]),
            "sea_color": list(canvas._sea_color().getRgbF()),
            "retained_wind_samples": len(flat[0]),
            "excluded": [
                "data loading during replay",
                "dynamic route-plan/track markers",
                "dashboard",
                "coverage notice",
                "dock UI",
            ],
        }
        for name, cache in (
            ("base", canvas._base_cache),
            ("navigation", canvas._nav_cache),
        ):
            if cache is None:
                raise ValueError("Expected retained layer image")
            metadata[name] = dict(latitude=cache[1], longitude=cache[2], zoom=cache[3])
        save_fixture(
            Path(config["fixture"]),
            metadata,
            canvas._base_cache[4],
            canvas._nav_cache[4],
            flat,
        )
    finally:
        window.close()
        app.processEvents()


def synthetic_worker(config):
    disable_external_access()
    import numpy as np
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPen
    from PySide6.QtWidgets import QApplication

    scene = load_tool("gpu_rendering_scene")
    app = QApplication([])
    dpr = config["dpr"]
    metadata = dict(
        fixture_version=1,
        source="controlled-glyphs",
        width=360,
        height=260,
        dpr=dpr,
        latitude=48.4,
        longitude=-123.4,
        zoom=10.0,
        density="dense",
        opacity=0.85,
        sea_color=[0.12, 0.18, 0.23, 1.0],
    )
    metadata["base"] = metadata["navigation"] = dict(
        latitude=48.4, longitude=-123.4, zoom=10.0
    )
    base = QImage(
        (360 + 384) * dpr, (260 + 384) * dpr, QImage.Format.Format_ARGB32_Premultiplied
    )
    base.setDevicePixelRatio(dpr)
    base.fill(QColor("#273a48"))
    painter = QPainter(base)
    painter.setPen(QPen(QColor("#657b83"), 1))
    for x in range(0, 744, 32):
        painter.drawLine(x, 0, x, 644)
    painter.end()
    navigation = QImage(base.size(), base.format())
    navigation.setDevicePixelRatio(dpr)
    navigation.fill(Qt.GlobalColor.transparent)
    projection = scene.Camera(metadata).projection
    positions = [
        projection.convert_screen_to_geographic(x, y).get_degrees()
        for y in np.linspace(35, 225, 6)
        for x in np.linspace(35, 325, 8)
    ]
    count = len(positions)
    flat = (
        np.resize(np.arange(0, 360, 30, dtype=float), count),
        np.resize(np.array([0, 1, 3, 5, 8, 12, 20, 27, 35.0]), count),
        np.array([p[0] for p in positions]),
        np.array([p[1] for p in positions]),
    )
    metadata["retained_wind_samples"] = count
    save_fixture(Path(config["fixture"]), metadata, base, navigation, flat)
    app.processEvents()


def render_worker(config):
    disable_external_access()
    import resource
    import numpy as np
    from PySide6 import __version__ as qt_version
    from PySide6.QtGui import QColor, QGuiApplication, QImage, QSurfaceFormat
    from PySide6.QtQuick import QQuickView, QQuickWindow, QSGRendererInterface

    scene_module = load_tool("gpu_rendering_scene")
    if sys.platform == "darwin":
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Metal)
    surface = QSurfaceFormat()
    surface.setSamples(4)
    surface.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(surface)
    app = QGuiApplication([])
    fixture = Path(config["fixture"])
    metadata = verify_fixture(fixture)
    base, navigation = QImage(str(fixture / "base.png")), QImage(
        str(fixture / "navigation.png")
    )
    for image in (base, navigation):
        image.setDevicePixelRatio(metadata["dpr"])
    with np.load(fixture / "wind.npz", allow_pickle=False) as data:
        flat = tuple(
            data[key].copy()
            for key in ("directions", "speeds", "latitudes", "longitudes")
        )
    payloads = scene_module.Payloads(metadata, flat, base, navigation)
    view = QQuickView()
    view.resize(metadata["width"], metadata["height"])
    view.setColor(QColor.fromRgbF(*metadata["sea_color"]))
    renderer = scene_module.make_scene(view, metadata, base, navigation, config["mode"])
    swaps = []
    view.frameSwapped.connect(lambda: swaps.append(time.perf_counter()))
    view.show()

    def submit(payload, overlay_only=False):
        started = time.perf_counter()
        renderer.frame(payload, overlay_only)
        prepared = time.perf_counter()
        previous = len(swaps)
        view.update()
        deadline = time.perf_counter() + 10
        while len(swaps) == previous:
            app.processEvents()
            if time.perf_counter() > deadline:
                raise TimeoutError("No Qt Quick frameSwapped callback")
            time.sleep(0.0001)
        return (prepared - started) * 1000, (swaps[-1] - started) * 1000, swaps[-1]

    payload = payloads.frame("stationary", 0, 1)
    first = submit(payload)
    api = view.rendererInterface().graphicsApi()
    if api in (
        QSGRendererInterface.GraphicsApi.Software,
        QSGRendererInterface.GraphicsApi.Null,
        QSGRendererInterface.GraphicsApi.Unknown,
    ):
        raise RuntimeError(f"Not a hardware scene graph: {api}")
    if sys.platform == "darwin" and api != QSGRendererInterface.GraphicsApi.Metal:
        raise RuntimeError(f"Expected Metal, got {api}")
    if view.devicePixelRatio() != metadata["dpr"]:
        raise ValueError("Native DPR differs from captured fixture")
    for _ in range(5):
        submit(payload)
    counters_before = dict(renderer.counters)
    samples = {
        key: []
        for key in (
            "payload_ms",
            "prepare_ms",
            "request_to_swap_ms",
            "swap_interval_ms",
        )
    }
    signatures, counts, primitives = [], [], []
    last_swap = None
    for index in range(config["frames"]):
        started = time.perf_counter()
        payload = payloads.frame(config["scenario"], index, config["frames"])
        payload_ms = (time.perf_counter() - started) * 1000
        prepared_ms, swap_ms, swapped = submit(payload)
        samples["payload_ms"].append(payload_ms)
        samples["prepare_ms"].append(prepared_ms)
        samples["request_to_swap_ms"].append(swap_ms + payload_ms)
        if last_swap is not None:
            samples["swap_interval_ms"].append((swapped - last_swap) * 1000)
        last_swap = swapped
        signatures.append(scene_module.geometry_digest(payload["geometry"]))
        counts.append(payload["selected_samples"])
        primitives.append(
            [
                sum(len(groups[k]) for groups in payload["geometry"].values())
                for k in range(3)
            ]
        )
    counters = {
        key: value - counters_before[key] for key, value in renderer.counters.items()
    }
    quality = []
    artifact_dir = Path(config["artifacts"])
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for index in sorted(
        {0, config["frames"] // 4, config["frames"] // 2, config["frames"] - 1}
    ):
        payload = payloads.frame(config["scenario"], index, config["frames"])
        reference = scene_module.cpu_image(
            metadata, base, navigation, payload, "cpu_current", True
        )
        view.setColor(QColor(0, 0, 0, 0))
        submit(payload, True)
        started = time.perf_counter()
        captured = view.grabWindow()
        readback_ms = (time.perf_counter() - started) * 1000
        if captured.isNull():
            raise RuntimeError("GPU readback failed")
        metrics = scene_module.pixel_metrics(
            scene_module.image_array(reference), scene_module.image_array(captured)
        )
        metrics.update(frame=index, blocking_readback_ms=readback_ms)
        full_reference = scene_module.cpu_image(
            metadata, base, navigation, payload, "cpu_current"
        )
        view.setColor(QColor.fromRgbF(*metadata["sea_color"]))
        submit(payload)
        full_candidate = view.grabWindow()
        composite_error = np.abs(
            scene_module.image_array(full_reference).astype(np.int16)
            - scene_module.image_array(full_candidate).astype(np.int16)
        )
        metrics["composite_rgba_mae"] = float(composite_error.mean())
        metrics["composite_rgba_p99_error"] = float(np.percentile(composite_error, 99))
        metrics["composite_within_experimental_tolerance"] = (
            metrics["composite_rgba_mae"] <= 1.0
            and metrics["composite_rgba_p99_error"] <= 8.0
        )
        quality.append(metrics)
        if index == 0 or not (
            metrics["within_experimental_tolerance"]
            and metrics["composite_within_experimental_tolerance"]
        ):
            prefix = "" if index == 0 else f"frame-{index}-"
            reference.save(str(artifact_dir / f"{prefix}reference-overlay.png"))
            captured.save(str(artifact_dir / f"{prefix}candidate-overlay.png"))
            full_reference.save(str(artifact_dir / f"{prefix}reference-composite.png"))
            full_candidate.save(str(artifact_dir / f"{prefix}candidate-composite.png"))
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
        1 if sys.platform == "darwin" else 1024
    )
    result = {
        "mode": config["mode"],
        "scenario": config["scenario"],
        "image_sampling_policy": "reference-unit-scale-nearest-scaled-linear-v1",
        "environment": dict(
            platform=platform.platform(),
            host=platform.node(),
            python=platform.python_version(),
            qt=qt_version,
            qpa=app.platformName(),
            graphics_api=api.name,
            render_loop=os.environ.get("QSG_RENDER_LOOP"),
            dpr=view.devicePixelRatio(),
            samples=view.format().samples(),
        ),
        "first_request_to_swap_ms": first[1],
        "samples": samples,
        "summary": {
            key: startup.protocol.summary(values) for key, values in samples.items()
        },
        "selected_samples_per_frame": counts,
        "primitives_per_frame": primitives,
        "geometry_sha256_per_frame": signatures,
        "measured_uploads": counters,
        "initial_uploads": counters_before,
        "known_base_navigation_texture_bytes": base.width() * base.height() * 4
        + navigation.width() * navigation.height() * 4,
        "gpu_stroke_fill_layers": len(renderer.layers),
        "process_peak_rss_bytes": peak,
        "quality": quality,
        "quality_gate_passed": all(
            q["within_experimental_tolerance"]
            and q["composite_within_experimental_tolerance"]
            for q in quality
        ),
        "calm_ring_max_chord_error_logical_px": 2.5 * (1 - math.cos(math.pi / 64)),
        "gpu_memory_usage": "not measured; texture/vertex bytes are explicit resource sizes, not total VRAM",
    }
    view.hide()
    app.processEvents()
    return result


def child(config, root, timeout=240):
    config_path, result_path = root / "config.json", root / "result.json"
    config_path.write_text(json.dumps(config))
    env = dict(
        os.environ,
        HOME=str(root / "home"),
        XDG_DATA_HOME=str(root / "xdg-data"),
        XDG_CONFIG_HOME=str(root / "xdg-config"),
        XDG_CACHE_HOME=str(root / "xdg-cache"),
        PYTHONPATH=str(ROOT),
        PYTHONHASHSEED="0",
        QSG_RENDER_LOOP="basic",
        QSG_INFO="1",
        QT_SCALE_FACTOR="1",
    )
    env.pop("QT_QPA_PLATFORM", None)
    env.pop("QT_QUICK_BACKEND", None)
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
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    (root / "worker.log").write_text(completed.stdout + completed.stderr)
    if completed.returncode:
        raise RuntimeError(
            f"Worker exited {completed.returncode}: {completed.stderr[-6000:]}"
        )
    return json.loads(result_path.read_text())


def run(args):
    metadata = verify_fixture(args.fixture)
    report = dict(
        experiment_version=1,
        fixture=metadata,
        revision=startup.protocol.revision(),
        harness_sha256=startup.digest(
            [
                startup.protocol.file_hash(Path(__file__)),
                startup.protocol.file_hash(
                    Path(__file__).with_name("gpu_rendering_scene.py")
                ),
            ]
        ),
        frames=args.frames,
        repeats=args.repeats,
        runs=[],
    )
    modes, scenarios = args.modes.split(","), args.scenarios.split(",")
    if any(m not in MODES for m in modes) or any(s not in SCENARIOS for s in scenarios):
        raise ValueError("Unknown mode or scenario")
    if len(set(modes)) != len(modes) or len(set(scenarios)) != len(scenarios):
        raise ValueError("Modes and scenarios must be unique")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.repeats):
        order = modes[repeat % len(modes) :] + modes[: repeat % len(modes)]
        for scenario in scenarios:
            for mode in order:
                artifacts = (
                    args.artifacts / f"{metadata['source']}-{mode}-{scenario}-{repeat}"
                )
                with tempfile.TemporaryDirectory(prefix="gwr-gpu-") as directory:
                    result = child(
                        dict(
                            action="render",
                            fixture=str(args.fixture.resolve()),
                            mode=mode,
                            scenario=scenario,
                            frames=args.frames,
                            artifacts=str(artifacts.resolve()),
                        ),
                        Path(directory),
                    )
                    (artifacts / "worker.log").write_text(
                        (Path(directory) / "worker.log").read_text()
                    )
                    result["backend_diagnostics"] = [
                        line
                        for line in (Path(directory) / "worker.log")
                        .read_text()
                        .splitlines()
                        if any(
                            term in line
                            for term in (
                                "Metal device:",
                                "MSAA sample count",
                                "basic render loop",
                            )
                        )
                    ]
                result["repeat"] = repeat
                report["runs"].append(result)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.with_suffix(".partial.json").write_text(
                    json.dumps(report, indent=2) + "\n"
                )
                print(
                    f"{mode}/{scenario} [{repeat + 1}/{args.repeats}]: prepare p50 {result['summary']['prepare_ms']['p50_ms']:.2f} ms; swap interval p99 {result['summary']['swap_interval_ms'].get('p99_ms',0):.2f} ms; quality={result['quality_gate_passed']}",
                    file=sys.stderr,
                )
    for scenario in scenarios:
        reference = None
        for result in (r for r in report["runs"] if r["scenario"] == scenario):
            content = (
                result["geometry_sha256_per_frame"],
                result["selected_samples_per_frame"],
                result["primitives_per_frame"],
            )
            if reference is not None and content != reference:
                raise ValueError(
                    "Modes/repetitions rendered different selected wind geometry"
                )
            reference = content
    report["identical_selected_geometry"] = True
    report["complete"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--capture", type=Path, metavar="STARTUP_SNAPSHOT")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--dpr", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            config = json.loads(args.worker.read_text())
            if config["action"] == "render":
                result = render_worker(config)
            elif config["action"] == "export":
                export_worker(config)
                result = dict(exported=True)
            elif config["action"] == "synthetic":
                synthetic_worker(config)
                result = dict(exported=True)
            else:
                raise ValueError("Unknown worker action")
        else:
            if args.fixture is None:
                parser.error("--fixture is required")
            if args.capture or args.synthetic:
                if args.capture and args.synthetic:
                    parser.error("Choose capture or synthetic, not both")
                if args.fixture.exists():
                    parser.error("Fixture destination must be new")
                with tempfile.TemporaryDirectory(prefix="gwr-gpu-export-") as directory:
                    root = Path(directory)
                    config = dict(
                        action="synthetic" if args.synthetic else "export",
                        fixture=str(args.fixture.resolve()),
                        dpr=args.dpr,
                    )
                    if args.capture:
                        manifest = startup.verify_snapshot(args.capture)
                        startup.prepare_sandbox(args.capture, root)
                        config.update(
                            forecast_time=manifest["forecast_time"],
                            snapshot_sha256=manifest["content_sha256"],
                        )
                    child(config, root)
                print(f"Exported private retained fixture: {args.fixture}")
                return
            if args.output is None or args.artifacts is None:
                parser.error("--output and private --artifacts directory are required")
            if args.frames < 2 or args.repeats < 1:
                parser.error("frames >=2 and repeats >=1 are required")
            result = run(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"GPU experiment failed: {exc}\n")


if __name__ == "__main__":
    main()
