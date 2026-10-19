"""Replay the installed Qt startup state in disposable, offline data copies."""

import argparse
import ctypes
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

SPEC = importlib.util.spec_from_file_location(
    "rendering_protocol", Path(__file__).with_name("benchmark_rendering.py")
)
protocol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(protocol)
NATIVE_SPEC = importlib.util.spec_from_file_location(
    "native_rendering_probe", Path(__file__).with_name("native_rendering_probe.py")
)
native_protocol = importlib.util.module_from_spec(NATIVE_SPEC)
NATIVE_SPEC.loader.exec_module(native_protocol)
ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = (
    "stationary",
    "pan",
    "reverse_pan",
    "zoom_out",
    "scrub_return",
    "pan_pause_pan",
    "trackpad_pan",
    "wheel_zoom",
    "wheel_zoom_reverse",
    "pan_zoom_reverse",
)
DIRECTORIES = ("gshhs", "grib", "enc", "marine", "polars", "chs-tiles")
FILES = (
    "settings.json",
    "workspace-qt.json",
    "grib-manager.json",
    "polar-manager.json",
    "track.json",
    "routing.json",
    "poi.json",
    "log.json",
    "seamarks.pbf",
    "s57_symbols.json",
)
TOKEN = "@APPDATA@"


def default_data_directory():
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/gweatherrouting"
    if sys.platform == "linux":
        return Path(os.environ.get("XDG_DATA_HOME", Path.home())) / ".gweatherrouting"
    raise ValueError("Startup snapshots currently support macOS and Linux")


def clone_file(source, destination):
    """Private copy-on-write on APFS, ordinary copy elsewhere; never hardlink."""
    if Path(source).is_symlink():
        raise ValueError(f"Snapshot inputs must not be symlinks: {source}")
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
            return str(destination)
    return shutil.copy2(source, destination)


def replace_root(value, old, new):
    if isinstance(value, dict):
        return {key: replace_root(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_root(item, old, new) for item in value]
    if isinstance(value, str) and (value == old or value.startswith(old + "/")):
        return new + value[len(old) :]
    return value


def inventory(directory):
    return [
        {
            "path": str(path.relative_to(directory)),
            "bytes": path.stat().st_size,
            "sha256": protocol.file_hash(path),
        }
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def array_content_hash(arrays):
    """Hash typed array boundaries as well as bytes for wind/mask parity."""
    if arrays is None:
        return None
    import numpy as np

    content = hashlib.sha256()
    for array in arrays:
        contiguous = np.ascontiguousarray(array)
        content.update(repr((contiguous.dtype.str, contiguous.shape)).encode())
        content.update(contiguous.tobytes())
    return content.hexdigest()


def configure_wind_workload(settings, config):
    """Explicit replay overrides; only called in the private worker sandbox."""
    if config.get("wind_style") is not None:
        settings.gribWindStyle = config["wind_style"]
    if config.get("wind_over_land") is not None:
        settings.gribArrowOnGround = config["wind_over_land"] == "show"


def numerical_weather_status(canvas):
    """Actual numerical-symbol state, not merely a hardware backend label."""
    gpu = getattr(canvas, "_gpu", None)
    stager = getattr(gpu, "weather", None)
    scene = getattr(gpu, "scene", None)
    frame = getattr(scene, "frame", None)
    numerical = getattr(frame, "numerical_wind", None)
    item = getattr(scene, "weather", None)
    ready = item is not None and bool(item.property("ready"))
    visible = item is not None and item.isVisible()
    cpu_batches = len(frame.wind) if frame is not None else 0
    raster = frame is not None and frame.raster_wind is not None
    active = bool(
        getattr(gpu, "active", False)
        and numerical is not None
        and ready
        and visible
        and not cpu_batches
        and not raster
    )
    atlas = numerical[0] if numerical is not None else None
    provider = getattr(scene, "weather_provider", None)
    return dict(
        enabled=stager is not None,
        active=active,
        shader_ready=bool(ready),
        symbols=atlas.count if atlas is not None else 0,
        atlas_bytes=atlas.nbytes if atlas is not None else 0,
        interval=(
            [atlas.staged.before, atlas.staged.after] if atlas is not None else None
        ),
        image_provider_requests=getattr(provider, "requests", 0),
        cpu_symbol_batches=cpu_batches,
        cpu_raster_symbols=bool(raster),
        band_cache=stager.band_cache_statistics() if stager is not None else {},
        field_store=stager.field_store_statistics() if stager is not None else {},
    )


def capture(source, destination):
    source_alias = str(source.absolute())
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError("Snapshot must be a new directory outside application data")
    settings_path = source / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    for kind in ("vectorCharts", "rasterCharts"):
        for chart in settings.get(kind, []):
            path = Path(chart["path"])
            if not path.is_absolute() or not path.resolve().is_relative_to(source):
                raise ValueError(
                    f"Chart outside managed storage needs an explicit fixture: {path}"
                )
            if path.resolve().relative_to(source).parts[0] not in DIRECTORIES:
                raise ValueError(
                    f"Chart is outside snapshotted data directories: {path}"
                )
    # Record all source signatures before copying; reject concurrent app saves.
    selected = [
        source / name for name in (*FILES, *DIRECTORIES) if (source / name).exists()
    ]
    inputs = []
    for path in selected:
        if path.is_symlink():
            raise ValueError(f"Snapshot inputs must not be symlinks: {path}")
        inputs.extend(path.rglob("*") if path.is_dir() else [path])
    signatures = {p: p.stat() for p in inputs if p.is_file()}
    if any(p.is_symlink() for p in inputs):
        raise ValueError("Snapshot trees must not contain symlinks")
    directory_entries = {
        p: tuple(sorted(x.name for x in p.iterdir()))
        for p in inputs + selected
        if p.is_dir()
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="gwr-capture-", dir=destination.parent
    ) as temporary:
        snapshot = Path(temporary) / "snapshot"
        data = snapshot / "data"
        data.mkdir(parents=True)
        for path in selected:
            target = data / path.name
            if path.is_dir():
                shutil.copytree(path, target, copy_function=clone_file)
            else:
                clone_file(path, target)
        for path, before in signatures.items():
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError(
                    f"Application data changed during capture; retry: {path.name}"
                )
        for path, entries in directory_entries.items():
            if entries != tuple(sorted(x.name for x in path.iterdir())):
                raise ValueError("Application data changed during capture; retry")
        for name in FILES:
            if name.endswith(".json") and (data / name).exists():
                path = data / name
                content = replace_root(
                    json.loads(path.read_text()), source_alias, TOKEN
                )
                path.write_text(json.dumps(replace_root(content, str(source), TOKEN)))
        entries = inventory(data)
        manifest = {
            "snapshot_version": 1,
            "forecast_time": datetime.now()
            .replace(minute=0, second=0, microsecond=0)
            .isoformat(),
            "timezone": list(time.tzname),
            "files": entries,
            "content_sha256": digest(entries),
            "excluded": [
                "instrument connections",
                "credentials",
                "recordings",
                "temporary downloads",
            ],
        }
        (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        snapshot.replace(destination)
    return manifest


def verify_snapshot(path):
    manifest = json.loads((path / "manifest.json").read_text())
    if manifest["snapshot_version"] != 1:
        raise ValueError("Unsupported snapshot version")
    entries = inventory(path / "data")
    if entries != manifest["files"] or digest(entries) != manifest["content_sha256"]:
        raise ValueError("Startup snapshot has changed; capture a new baseline")
    return manifest


def prepare_sandbox(snapshot, root):
    if sys.platform == "darwin":
        data = root / "home/Library/Application Support/gweatherrouting"
    elif sys.platform == "linux":
        data = root / "xdg-data/.gweatherrouting"
    else:
        raise ValueError("Startup replay currently supports macOS and Linux")
    shutil.copytree(snapshot / "data", data, copy_function=clone_file)
    for name in FILES:
        path = data / name
        if name.endswith(".json") and path.exists():
            path.write_text(
                json.dumps(replace_root(json.loads(path.read_text()), TOKEN, str(data)))
            )
    return data


def worker(config):
    import logging
    import platform
    import resource
    from types import SimpleNamespace

    # Fail closed before importing the application. Cached CHS data is handled
    # locally below, never by relaxing this network guard.
    def no_network(*_args, **_kwargs):
        raise RuntimeError("Network is disabled in startup rendering benchmarks")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    import cairo
    import numpy as np
    import PySide6
    from osgeo import gdal
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.window import PreviewWindow
    from gweatherrouting.qt.theme import apply_theme

    # The system keychain is shared even with an isolated HOME. Treat account
    # state as unavailable; no account is needed to render captured local data.
    SystemCredentialStore.load = lambda _self: None

    warnings = []

    class WarningCapture(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())

    handler = WarningCapture(logging.WARNING)
    logger = logging.getLogger("gweatherrouting")
    logger.addHandler(handler)
    app = QApplication([])
    app.setApplicationName("GWeatherRouting")
    apply_theme(app)
    began = time.perf_counter()
    schedule_began = time.monotonic()
    controller = SessionController(
        offline=True, map_renderer=config.get("map_renderer")
    )
    controller.timer.stop()
    configure_wind_workload(controller.settings, config)
    controller.time_control.set_time(datetime.fromisoformat(config["forecast_time"]))
    controller_ms = (time.perf_counter() - began) * 1000
    chs = controller.charts._chs_renderer
    tile_misses = set()

    def cached_fetch(key):
        cached = chs.source.cached(key)
        if cached is None:
            tile_misses.add(key)
            raise ValueError("CHS tile not present in captured cache")
        return cached

    chs.source.fetch = cached_fetch
    chs.set_enabled(controller.settings.canadianCharts)
    window = PreviewWindow(controller)
    window_ms = (time.perf_counter() - began) * 1000 - controller_ms
    if window.views.tabText(window.views.currentIndex()) == "Charts":
        canvas = window.map
    elif window.views.tabText(window.views.currentIndex()) == "Logs":
        canvas = window.log_map
    else:
        window.close()
        raise ValueError(
            "Saved startup view is not a map; refusing to substitute a different view"
        )
    renderers = {
        "base_charts": canvas._base_chart_renderer,
        "enc": controller.charts._enc_renderer,
        "wind": canvas.wind,
        "marine": canvas.marine._renderer,
        "isochrones": canvas.isochrones,
    }
    jobs = []
    records = {}
    phase = "startup"
    stages = {
        name: []
        for name in (
            "_base_image",
            "_navigation_image",
            "_paint_wind_barbs",
            "_paint_track_overlay",
            "_dashboard_image",
        )
    }
    gpu_probes = []
    weather_events, prepared_views = [], []
    post_gesture_began = None
    first_prepared_view_ms = None
    zoom_input_began = None
    expected_zoom = None
    first_prepared_zoom_ms = None
    if config.get("synchronous_base_charts"):
        canvas._base_chart_renderer.draw_base = lambda *args, **kwargs: False
    if config.get("uncached_weather_passes"):
        canvas.wind._reuse_job_sources = False
    canvas.wind._prefetch_enabled = bool(config.get("weather_prefetch"))
    canvas._adaptive_pan_prefetch = not config.get("fixed_pan_lookahead", False)
    canvas._zoom_prefetch_enabled = bool(
        config.get("weather_prefetch")
    ) and not config.get("disable_zoom_prefetch", False)
    for panel in window.marine_panels.values():
        panel._uncached_coverage_fields = bool(config.get("uncached_coverage_fields"))
    if config.get("disable_weather_field_cache"):
        canvas.wind._field_cache_enabled = False
    if config.get("weather_source_limit") is not None:
        canvas.wind._source_retention_limit = int(config["weather_source_limit"])
    if config.get("legacy_weather_scheduling"):
        canvas.wind._preserve_covered_jobs = False
        canvas.wind._coalesce_active = False
    elif config.get("preserve_covered_weather_jobs"):
        canvas.wind._preserve_covered_jobs = True

    def trace_weather(event, job):
        record = dict(
            event=event,
            request_id=job.request_id,
            elapsed_ms=(time.monotonic() - schedule_began) * 1000,
            reason=job.cancel_reason,
        )
        if event == "request":
            record.update(
                viewport=dict(
                    latitude=job.viewport._center_lat,
                    longitude=job.viewport._center_lon,
                    zoom=job.viewport._zoom,
                    width=job.viewport.width,
                    height=job.viewport.height,
                ),
                content_key=digest(
                    [
                        repr(job.palette),
                        str(job.factor),
                        repr(
                            [
                                (source.name, Path(source.path).name)
                                for (source,) in job.charts
                            ]
                        ),
                    ]
                ),
            )
        weather_events.append(record)

    canvas.wind._schedule_event = trace_weather

    def observe_prepared_view():
        nonlocal first_prepared_view_ms, first_prepared_zoom_ms
        status = canvas.wind.prepared_view_status(canvas)
        if (
            zoom_input_began is not None
            and first_prepared_zoom_ms is None
            and canvas._zoom == expected_zoom
            and not canvas._zoom_animating
            and status["current"]
        ):
            first_prepared_zoom_ms = (time.monotonic() - zoom_input_began) * 1000
        state = canvas.wind._states.get(canvas)
        frame_id = state.frame[0].request_id if state and state.frame else None
        view = (canvas._center_lat, canvas._center_lon, canvas._zoom)
        signature = (phase, frame_id, view, status["preview"], status["current"])
        if not prepared_views or prepared_views[-1]["signature"] != signature:
            prepared_views.append(
                dict(
                    signature=signature,
                    phase=phase,
                    frame_id=frame_id,
                    elapsed_ms=(time.monotonic() - schedule_began) * 1000,
                    **status,
                )
            )
        if (
            post_gesture_began is not None
            and first_prepared_view_ms is None
            and status["current"]
        ):
            first_prepared_view_ms = (time.monotonic() - post_gesture_began) * 1000

    def observe_renderer(name, renderer):
        original_render, original_completed = renderer._render, renderer._completed

        def render(job):
            profile = None
            if name == "wind" and config.get("profile_weather"):
                import cProfile

                profile = cProfile.Profile()
            record = {
                "layer": name,
                "phase": phase,
                "duration_ms": None,
                "completed": False,
                "speculative": bool(job.speculative),
                "request_id": job.request_id,
                "forecast_time": job.palette[0].isoformat() if name == "wind" else None,
                "created_ms": (job.created_at - schedule_began) * 1000,
                "queue_wait_ms": (
                    (job.started_at - job.created_at) * 1000
                    if job.started_at is not None
                    else None
                ),
                "viewport": {
                    "latitude": job.viewport._center_lat,
                    "longitude": job.viewport._center_lon,
                    "zoom": job.viewport._zoom,
                },
            }
            records[id(job)] = record
            jobs.append(record)
            started = time.perf_counter()
            try:
                if profile is not None:
                    profile.enable()
                surface = original_render(job)
                if name == "wind" and surface is not None and not job.cancel.is_set():
                    flat = getattr(job, "barb_flat", None)
                    digest = hashlib.sha256()
                    if flat is not None:
                        for array in flat:
                            digest.update(np.ascontiguousarray(array).tobytes())
                    record["weather_content"] = {
                        "surface_sha256": hashlib.sha256(
                            surface.get_data()
                        ).hexdigest(),
                        "surface_size": [surface.get_width(), surface.get_height()],
                        "barb_sha256": digest.hexdigest(),
                        "barb_count": len(flat[0]) if flat is not None else 0,
                        "barb_land_sha256": array_content_hash(
                            (job.barb_land,)
                            if getattr(job, "barb_land", None) is not None
                            else None
                        ),
                        "streamlines_sha256": array_content_hash(
                            getattr(job, "streamlines", None)
                        ),
                    }
                return surface
            finally:
                if name == "wind":
                    record["weather_cache"] = dict(getattr(job, "weather_metrics", {}))
                if profile is not None:
                    profile.disable()
                    import pstats

                    entries = pstats.Stats(profile).stats
                    record["profile"] = [
                        dict(
                            file=Path(key[0]).name,
                            line=key[1],
                            function=key[2],
                            calls=value[1],
                            self_ms=value[2] * 1000,
                            cumulative_ms=value[3] * 1000,
                        )
                        for key, value in sorted(
                            entries.items(), key=lambda pair: pair[1][3], reverse=True
                        )[:40]
                    ]
                record["duration_ms"] = (time.perf_counter() - started) * 1000

        def completed(view, job, surface, error):
            result = original_completed(view, job, surface, error)
            record = records.pop(id(job), None)
            if record is not None:
                state = renderer._states.get(view)
                record.update(
                    completed=True,
                    cancelled=job.cancel.is_set(),
                    error=error,
                    published=bool(state and state.frame and state.frame[0] is job),
                    cancel_reason=job.cancel_reason,
                    published_ms=(
                        (job.published_at - schedule_began) * 1000
                        if job.published_at is not None
                        else None
                    ),
                    cancelled_ms=(
                        (job.cancelled_at - schedule_began) * 1000
                        if job.cancelled_at is not None
                        else None
                    ),
                )
                if name == "wind":
                    record["prepared_current_on_publish"] = bool(
                        record["published"]
                        and renderer.prepared_view_status(view)["current"]
                    )
                    observe_prepared_view()
            return result

        renderer._render, renderer._completed = render, completed

    for name, renderer in renderers.items():
        observe_renderer(name, renderer)

    def instrument(name, original):
        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                stages[name].append((time.perf_counter() - started) * 1000)

        return wrapped

    for name in stages:
        setattr(canvas, name, instrument(name, getattr(canvas, name)))
    for name, owner, method in (
        ("chart_draw", canvas.charts, "do_draw"),
        ("static_chart_draw", canvas.charts, "_draw_base"),
        ("wind_composite", canvas.wind, "draw"),
        ("marine_composite", canvas.marine, "do_draw"),
    ):
        stages[name] = []
        setattr(owner, method, instrument(name, getattr(owner, method)))

    def grab():
        started = time.perf_counter()
        gpu = canvas._gpu
        if gpu is not None and gpu.active:
            gpu.timer.stop()
            gpu._present()
            prepared = time.perf_counter()
            if gpu.active:
                image = gpu.widget.grabFramebuffer()
                if image.isNull():
                    app.processEvents()
                    image = gpu.widget.grabFramebuffer()
                if image.isNull():
                    raise RuntimeError("Embedded GPU frame readback failed")
                gpu_probes.append(
                    {
                        "prepare_ms": (prepared - started) * 1000,
                        "readback_ms": (time.perf_counter() - prepared) * 1000,
                    }
                )
        canvas.grab()
        return (time.perf_counter() - started) * 1000

    def renderer_busy(renderer):
        if renderer is canvas.wind:
            return renderer._active_job is not None or bool(renderer._pending)
        return renderer._running or bool(renderer._pending)

    def settle():
        canvas.wind.cancel_speculation()
        deadline = time.perf_counter() + config["settle_timeout_s"]
        quiet_since = None
        while time.perf_counter() < deadline:
            app.processEvents()
            observe_prepared_view()
            coverage = canvas.weather_coverage
            busy = any(renderer_busy(r) for r in renderers.values()) or bool(
                chs._inflight
                or chs._pending
                or chs._ticks
                or coverage.running
                or coverage.pending
            )
            if busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.perf_counter()
            elif time.perf_counter() - quiet_since >= 0.2:
                if any(j.get("error") for j in jobs):
                    raise RuntimeError(
                        "Overlay rendering failed; see worker diagnostics"
                    )
                return
            time.sleep(0.001)
        active = [name for name, r in renderers.items() if r._running or r._pending]
        raise TimeoutError(f"Startup overlays did not settle: {active}")

    samples = {
        key: [] for key in ("grab_ms", "queued_callback_ms", "start_interval_ms")
    }
    rebuilds, surfaces = [], []
    try:
        window.show()
        cold_ms = grab()
        first_ms = (time.perf_counter() - began) * 1000
        settle()
        grab()
        after_settle = config.get("_after_startup_settled")
        if after_settle:
            after_settle(canvas)
            grab()
            settle()
            grab()
        settled_ms = (time.perf_counter() - began) * 1000
        if controller.chart_errors:
            raise RuntimeError("Saved chart layers failed to open")
        if config.get("map_renderer") == "gpu" and (
            canvas._gpu is None or not canvas._gpu.active or not canvas._gpu.initialized
        ):
            raise RuntimeError(
                "GPU benchmark fell back: " + controller.renderer_status()
            )
        initial_view = {
            "latitude": canvas._center_lat,
            "longitude": canvas._center_lon,
            "zoom": canvas._zoom,
        }
        startup = {
            "view": window.views.tabText(window.views.currentIndex()),
            "map": initial_view,
            "wind": canvas.wind_visible,
            "dashboard": canvas.dashboard_visible,
            "isobars": controller.settings.gribIsobarsVisible,
            "palette": controller.settings.chartPalette,
            "barb_density": controller.settings.gribBarbDensity,
            "wind_style": controller.settings.get("gribWindStyle", "barbs"),
            "wind_over_land": controller.settings.gribArrowOnGround,
            "isobar_interval": controller.settings.gribIsobarInterval,
            "marine_visible": canvas.marine.visible,
            "currents_visible": canvas.marine.currents_visible,
            "chs_enabled": chs.enabled,
            "charts": len(controller.charts.charts),
            "enabled_charts": sum(c.enabled for c in controller.charts.charts),
            "forecasts": len(controller.core.grib_manager.gribs),
            "routes": len(controller.core.routingManager),
            "route_points": sum(len(r) for r in controller.core.routingManager),
            "tracks": len(controller.core.trackManager),
            "track_points": sum(len(r) for r in controller.core.trackManager),
            "marine_fields": len(canvas.marine.sources.fields),
            "tide_datasets": len(canvas.marine.sources.levels.datasets),
            "current_station_datasets": len(canvas.marine.sources.currents.datasets),
        }
        wind_state = canvas.wind._states.get(canvas)
        flat = (
            getattr(wind_state.frame[0], "barb_flat", None)
            if wind_state and wind_state.frame
            else None
        )
        startup["retained_wind_samples"] = len(flat[0]) if flat is not None else 0
        phase = "warmup"
        for _ in range(config["warmup_frames"]):
            grab()
            app.processEvents()
        for values in stages.values():
            values.clear()
        warm_job_offset = len(jobs)
        gpu_probe_offset = len(gpu_probes)
        preparation_service = config.get("_preparation_service")
        if preparation_service:
            return preparation_service(canvas, app, settle, grab)
        before_native = config.get("_before_native_probe")
        native_fidelity = before_native(canvas, app) if before_native else None
        phase = "replay"
        ui_profile = None
        if config.get("profile_ui"):
            import cProfile

            ui_profile = cProfile.Profile()
            ui_profile.enable()
        scenario = config["scenario"]
        initial_time = canvas.time_control.time
        if scenario in ("pan", "reverse_pan", "zoom_out", "pan_pause_pan"):
            canvas.drag = SimpleNamespace()
        if scenario == "zoom_out":
            canvas._zoom_animating = True
            canvas._zoom_anchor = (
                canvas._center_lat,
                canvas._center_lon,
                canvas.width() / 2,
                canvas.height() / 2,
            )
        last_start = None
        prepared_samples = []
        frame_traces = []
        native_probe = None
        if config.get("measurement_mode") == "native-render":
            native_probe = native_protocol.replay(canvas, config)
        for index in range(0 if native_probe is not None else config["frames"]):
            started = time.perf_counter()
            stage_offsets = {key: len(values) for key, values in stages.items()}
            probe_offset = len(gpu_probes)
            if last_start is not None:
                samples["start_interval_ms"].append((started - last_start) * 1000)
            last_start = started
            mixed_zoom = scenario == "pan_zoom_reverse" and index in (
                config["frames"] // 3,
                config["frames"] // 2,
            )
            if scenario in ("wheel_zoom", "wheel_zoom_reverse") or mixed_zoom:
                if (
                    mixed_zoom
                    or index == 0
                    or (scenario == "wheel_zoom_reverse" and index == 8)
                ):
                    from PySide6.QtCore import QPoint, QPointF, Qt
                    from PySide6.QtGui import QWheelEvent

                    point = QPointF(canvas.width() * 0.3, canvas.height() * 0.4)
                    event = QWheelEvent(
                        point,
                        point,
                        QPoint(),
                        QPoint(0, 480 if index in (0, config["frames"] // 3) else -480),
                        Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase,
                        False,
                    )
                    zoom_input_began = time.monotonic()
                    first_prepared_zoom_ms = None
                    app.sendEvent(canvas, event)
                    expected_zoom = (
                        canvas._zoom_target
                        if canvas._zoom_target is not None
                        else canvas._zoom
                    )
            elif (
                scenario == "pan_zoom_reverse"
                and config["frames"] // 3 <= index < 2 * config["frames"] // 3
            ):
                pass
            elif scenario in ("trackpad_pan", "pan_zoom_reverse"):
                from PySide6.QtCore import QPoint, QPointF, Qt
                from PySide6.QtGui import QWheelEvent

                canvas._trackpad_scroll_pans = True
                point = QPointF(canvas.width() / 2, canvas.height() / 2)
                event = QWheelEvent(
                    point,
                    point,
                    QPoint(
                        config.get("trackpad_step_px", 16)
                        * (
                            1
                            if scenario == "pan_zoom_reverse"
                            and index >= 2 * config["frames"] // 3
                            else -1
                        ),
                        0,
                    ),
                    QPoint(),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                    (
                        Qt.ScrollPhase.ScrollBegin
                        if index == 0
                        or (
                            scenario == "pan_zoom_reverse"
                            and index == 2 * config["frames"] // 3
                        )
                        else Qt.ScrollPhase.ScrollUpdate
                    ),
                    False,
                )
                app.sendEvent(canvas, event)
            elif scenario == "scrub_return":
                if index < 3:
                    minutes = 30 if index == 1 else 15
                    canvas.time_control.set_time(
                        initial_time + timedelta(minutes=minutes)
                    )
            elif scenario == "zoom_out":
                canvas._zoom = max(
                    1, initial_view["zoom"] - (index + 1) / config["frames"]
                )
                canvas._recenter_on_anchor()
            elif scenario == "pan_pause_pan":
                leg = config["frames"] // 3
                if index < leg:
                    canvas._pan_pixels(-8, 0)
                elif index == leg:
                    settle()
                    if config.get("weather_prefetch"):
                        leg2 = config["frames"] - 2 * leg
                        if leg2 * 8 < 192:
                            raise ValueError(
                                "pan_pause_pan needs a final leg of 192px; raise --frames"
                            )
                        canvas._release_velocity = (-8.0 * leg2, 0.0)
                        canvas._refresh_after_gesture()
                        paused = time.perf_counter()
                        while time.perf_counter() - paused < 60:
                            app.processEvents()
                            state = canvas.wind._states.get(canvas)
                            if state is not None and state.prefetched:
                                break
                            time.sleep(0.005)
                        else:
                            raise RuntimeError("Prefetch did not complete during pause")
                elif index < 2 * leg:
                    pass
                else:
                    canvas._pan_pixels(-8, 0)
            elif scenario != "stationary":
                direction = (
                    -1
                    if scenario == "reverse_pan" and index >= config["frames"] // 2
                    else 1
                )
                canvas._pan_pixels(-8 * direction, 0)
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
            observe_prepared_view()
            prepared_samples.append(canvas.wind.prepared_view_status(canvas)["current"])
            frame_traces.append(
                dict(
                    index=index,
                    grab_ms=samples["grab_ms"][-1],
                    stages_ms={
                        key: sum(values[stage_offsets[key] :])
                        for key, values in stages.items()
                    },
                    gpu_probes=gpu_probes[probe_offset:],
                    numerical_weather=numerical_weather_status(canvas),
                )
            )
            rebuilds.append({key: len(stages[key]) - before[key] for key in before})
            surfaces.append(protocol.tracked_surface_bytes(canvas))
            until = started + config["interval_ms"] / 1000
            while time.perf_counter() < until:
                app.processEvents()
                time.sleep(min(0.001, max(0, until - time.perf_counter())))
        replay_stage_counts = {key: len(values) for key, values in stages.items()}
        if ui_profile is not None:
            ui_profile.disable()
        ui_profile_text = None
        if ui_profile is not None:
            import io
            import pstats

            stream = io.StringIO()
            pstats.Stats(ui_profile, stream=stream).sort_stats(
                "cumulative"
            ).print_stats(80)
            ui_profile_text = stream.getvalue()
        if scenario in ("trackpad_pan", "pan_zoom_reverse") or (
            native_probe is not None and scenario == "reverse_pan"
        ):
            canvas._finish_trackpad_pan()
        replay_gpu_probe_end = len(gpu_probes)
        post_job_offset = len(jobs)
        phase = "settle"
        post_gesture_began = time.monotonic()
        observe_prepared_view()
        canvas.drag = None
        canvas._zoom_animating = False
        canvas._zoom_anchor = None
        canvas._refresh_after_gesture()
        started = time.perf_counter()
        grab()
        settle()
        grab()
        post_ms = (time.perf_counter() - started) * 1000
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        screen = app.primaryScreen().availableGeometry()
        return {
            "scenario": scenario,
            "native_render_probe": native_probe,
            "native_fidelity": native_fidelity,
            "startup": startup,
            "actual_viewport": [canvas.width(), canvas.height()],
            "window_size": [window.width(), window.height()],
            "controller_init_ms": controller_ms,
            "window_init_ms": window_ms,
            "startup_to_first_grab_ms": first_ms,
            "startup_to_worker_idle_ms": settled_ms,
            "cold_first_grab_ms": cold_ms,
            "post_gesture_worker_idle_ms": post_ms,
            "post_gesture_first_prepared_view_ms": first_prepared_view_ms,
            "weather_source_retention": canvas.wind._source_retention_limit,
            "weather_prefetch": dict(canvas.wind.prefetch_metrics),
            "pan_lookahead": (
                "fixed" if config.get("fixed_pan_lookahead") else "adaptive"
            ),
            "pan_preparation": dict(
                seconds=canvas.wind._pan_preparation.seconds,
                samples=canvas.wind._pan_preparation.samples,
                lookahead_seconds=canvas.wind.pan_lookahead(canvas),
            ),
            "replay_prepared_samples": prepared_samples,
            "frame_traces": frame_traces,
            "ui_profile": ui_profile_text,
            "uncached_coverage_fields": bool(config.get("uncached_coverage_fields")),
            "first_prepared_zoom_ms": first_prepared_zoom_ms,
            "zoom_prefetch_enabled": canvas._zoom_prefetch_enabled,
            "weather_schedule_events": weather_events,
            "weather_prepared_views": prepared_views,
            "weather_scheduling": (
                "legacy"
                if config.get("legacy_weather_scheduling")
                else (
                    "covered-active-trial"
                    if config.get("preserve_covered_weather_jobs")
                    else "coalesced"
                )
            ),
            "samples": samples,
            "summary": {
                key: protocol.summary(values) for key, values in samples.items()
            },
            "stages_ms": stages,
            "replay_stage_counts": replay_stage_counts,
            "rebuilds_per_frame": rebuilds,
            "tracked_surface_bytes_per_frame": surfaces,
            "process_peak_rss_bytes": peak * (1 if sys.platform == "darwin" else 1024),
            "overlay_jobs": jobs,
            "warm_job_offset": warm_job_offset,
            "post_job_offset": post_job_offset,
            "cached_chs_missing_tiles": len(tile_misses),
            "warnings": warnings,
            "renderer_requested": controller.map_renderer,
            "renderer_status": controller.renderer_status(),
            "numerical_weather": numerical_weather_status(canvas),
            "gpu_wind_renderer": canvas._gpu_wind_mode,
            "base_charts_mode": (
                "synchronous" if config.get("synchronous_base_charts") else "worker"
            ),
            "base_charts_diagnostics": canvas._base_chart_renderer.diagnostics,
            "weather_reuse_job_sources": canvas.wind._reuse_job_sources,
            "gpu_probes": gpu_probes[gpu_probe_offset:replay_gpu_probe_end],
            "gpu_counters": (
                dict(canvas._gpu.scene.counters)
                if canvas._gpu is not None and canvas._gpu.active
                else None
            ),
            "gpu_resource_estimate": (
                canvas._gpu.estimate
                if canvas._gpu is not None and canvas._gpu.active
                else None
            ),
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
                "qt_style": app.style().objectName(),
                "dpr": canvas.devicePixelRatioF(),
                "available_screen": [screen.width(), screen.height()],
                "refresh_rate_hz": app.primaryScreen().refreshRate(),
                "timezone": list(time.tzname),
            },
        }
    finally:
        window.close()
        app.processEvents()
        logger.removeHandler(handler)


def run(args):
    snapshot = args.snapshot.resolve(strict=True)
    manifest = verify_snapshot(snapshot)
    scenarios = args.scenarios.split(",")
    if any(s not in SCENARIOS for s in scenarios) or len(set(scenarios)) != len(
        scenarios
    ):
        raise ValueError(f"Choose unique scenarios from {SCENARIOS}")
    if args.measurement_mode == "native-render":
        if args.offscreen or args.map_renderer != "gpu":
            raise ValueError("native-render requires native Qt and --map-renderer gpu")
        if any(s not in native_protocol.SCENARIOS for s in scenarios):
            raise ValueError(f"native-render scenarios: {native_protocol.SCENARIOS}")
    workload = {
        "profile": "installed-startup-v1",
        "snapshot_sha256": manifest["content_sha256"],
        "snapshot_files": len(manifest["files"]),
        "snapshot_bytes": sum(f["bytes"] for f in manifest["files"]),
        "forecast_time": manifest["forecast_time"],
        "scenarios": scenarios,
        "network": "cached-only",
        "credentials": "disabled",
        "trajectory_version": 1,
        "weather_profiled": args.profile_weather,
        "ui_profiled": args.profile_ui,
        "weather_prefetch": args.weather_prefetch,
        "wind_style": args.wind_style,
        "wind_over_land": args.wind_over_land,
        "trackpad_step_px": args.trackpad_step_px,
        "mode": (
            "native-render-v1"
            if args.measurement_mode == "native-render"
            else "offscreen" if args.offscreen else "native-grab"
        ),
        "measurement_mode": args.measurement_mode,
        "native_drain_timeout_s": args.native_drain_timeout_s,
        **{
            key: getattr(args, key)
            for key in (
                "frames",
                "repeats",
                "warmup_frames",
                "scale",
                "interval_ms",
                "settle_timeout_s",
            )
        },
    }
    provenance = protocol.revision()
    provenance["harness_sha256"] = digest(
        [
            protocol.file_hash(__file__),
            protocol.file_hash(Path(__file__).with_name("benchmark_rendering.py")),
            protocol.file_hash(Path(__file__).with_name("native_rendering_probe.py")),
        ]
    )
    report = {
        "schema_version": 2 if args.measurement_mode == "native-render" else 1,
        "revision": provenance,
        "workload": workload,
        "runs": [],
    }
    checkpoint = args.output.with_suffix(".partial.json")
    checkpoint.parent.mkdir(parents=True, exist_ok=True)

    def save_checkpoint():
        checkpoint.write_text(
            json.dumps({**report, "complete": False}, indent=2) + "\n"
        )

    for repeat in range(args.repeats):
        for scenario in scenarios:
            with tempfile.TemporaryDirectory(prefix="gwr-startup-") as directory:
                root = Path(directory)
                prepare_sandbox(snapshot, root)
                config = {
                    **workload,
                    "scenario": scenario,
                    "map_renderer": args.map_renderer,
                    "synchronous_base_charts": args.synchronous_base_charts,
                    "profile_weather": args.profile_weather,
                    "profile_ui": args.profile_ui,
                    "uncached_coverage_fields": args.uncached_coverage_fields,
                    "uncached_weather_passes": args.uncached_weather_passes,
                    "legacy_weather_scheduling": args.legacy_weather_scheduling,
                    "preserve_covered_weather_jobs": args.preserve_covered_weather_jobs,
                    "weather_source_limit": args.weather_source_limit,
                    "weather_prefetch": args.weather_prefetch,
                    "fixed_pan_lookahead": args.fixed_pan_lookahead,
                    "disable_zoom_prefetch": args.disable_zoom_prefetch,
                    "disable_weather_field_cache": args.disable_weather_field_cache,
                }
                config_path, output = root / "config.json", root / "result.json"
                config_path.write_text(json.dumps(config))
                env = dict(
                    os.environ,
                    HOME=str(root / "home"),
                    XDG_DATA_HOME=str(root / "xdg-data"),
                    XDG_CONFIG_HOME=str(root / "xdg-config"),
                    XDG_CACHE_HOME=str(root / "xdg-cache"),
                    PYTHONHASHSEED="0",
                    PYTHONPATH=str(ROOT),
                    QT_SCALE_FACTOR=str(args.scale),
                )
                if args.offscreen:
                    env["QT_QPA_PLATFORM"] = "offscreen"
                else:
                    env.pop("QT_QPA_PLATFORM", None)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(config_path),
                        "--output",
                        str(output),
                    ],
                    env=env,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=args.worker_timeout_s,
                )
                if result.returncode:
                    report["failure"] = {
                        "scenario": scenario,
                        "repeat": repeat,
                        "exit_code": result.returncode,
                    }
                    save_checkpoint()
                    diagnostics = args.output.with_suffix(".failure.log")
                    diagnostics.write_text(result.stdout + result.stderr)
                    raise RuntimeError(
                        f"Worker exited {result.returncode}; completed runs: {checkpoint}; diagnostics: {diagnostics}"
                    )
                measured = json.loads(output.read_text())
                environment = measured.pop("environment")
                if "environment" in report and report["environment"] != environment:
                    raise ValueError("Environment changed between runs")
                report["environment"] = environment
                measured["repeat"] = repeat
                report["runs"].append(measured)
                save_checkpoint()
                if args.measurement_mode == "native-render":
                    inputs = measured["native_render_probe"]["inputs"]
                    observed = sum(i["status"] == "observed" for i in inputs)
                    print(
                        f"{scenario}: {observed}/{len(inputs)} inputs observed at afterRendering (not presentation)",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"{scenario} [{repeat + 1}/{args.repeats}]: p99 {measured['summary']['grab_ms']['p99_ms']:.1f} ms; startup idle {measured['startup_to_worker_idle_ms']/1000:.1f}s",
                        file=sys.stderr,
                    )
    report["complete"] = True
    return report


def compare_weather_content(before, after):
    """Strict same-viewport weather identity gate, separate from frame timings."""

    def published(run):
        return [
            job
            for job in run.get("overlay_jobs", ())
            if job["layer"] == "wind" and job.get("published")
        ]

    all_jobs = [
        job
        for report in (before, after)
        for run in report["runs"]
        for job in published(run)
    ]
    if not any("weather_content" in job for job in all_jobs):
        return dict(status="not_captured", matched_frames=0)
    if any("weather_content" not in job for job in all_jobs):
        raise ValueError("Missing weather content hashes; refusing partial validation")

    def coverage(run):
        """Published frames plus prefetch promotions (publish events whose
        job record was stored, not completed-published)."""
        items = list(published(run))
        viewports = {}
        for event in run.get("weather_schedule_events", ()):
            if event["event"] == "request":
                viewports[event["request_id"]] = event["viewport"]
            elif event["event"] == "publish":
                request_id = event["request_id"]
                if any(job.get("request_id") == request_id for job in items):
                    continue
                record = next(
                    (
                        job
                        for job in run.get("overlay_jobs", ())
                        if job["layer"] == "wind"
                        and job.get("request_id") == request_id
                        and "weather_content" in job
                    ),
                    None,
                )
                if record is not None and request_id in viewports:
                    items.append(
                        dict(
                            record,
                            viewport=viewports[request_id],
                            published=True,
                        )
                    )
        return items

    candidates = {(run["scenario"], run["repeat"]): run for run in after["runs"]}
    matched = 0
    only_before, only_after = 0, 0
    for run in before["runs"]:
        peer = candidates.get((run["scenario"], run["repeat"]))
        if peer is None:
            raise ValueError("Missing weather comparison run")
        mine = coverage(run)
        theirs = coverage(peer)
        for job in mine:
            peers = [
                item
                for item in theirs
                if item["viewport"] == job["viewport"]
                and item.get("forecast_time") == job.get("forecast_time")
            ]
            if not peers:
                # Scheduling may legitimately change which intermediate
                # viewports get their own frame (e.g. prefetch promotion
                # replaces a mid-leg publication); shared viewports must
                # still agree exactly.
                only_before += 1
                continue
            if any(item["weather_content"] != job["weather_content"] for item in peers):
                raise ValueError("Weather content changed at a matched viewport")
            matched += 1
        for item in theirs:
            if not any(
                job["viewport"] == item["viewport"]
                and job.get("forecast_time") == item.get("forecast_time")
                for job in mine
            ):
                only_after += 1
    if not matched:
        raise ValueError("No matched weather frames")
    return dict(
        status="passed",
        matched_frames=matched,
        only_baseline_frames=only_before,
        only_candidate_frames=only_after,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--capture-current", type=Path, metavar="NEW_SNAPSHOT")
    group.add_argument("--snapshot", type=Path)
    group.add_argument("--compare", nargs=2, type=Path)
    group.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS[:4]))
    parser.add_argument("--frames", type=int, default=180)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup-frames", type=int, default=5)
    parser.add_argument("--scale", type=int, choices=(1, 2), default=1)
    parser.add_argument("--interval-ms", type=float, default=16.667)
    parser.add_argument("--settle-timeout-s", type=float, default=180)
    parser.add_argument("--worker-timeout-s", type=float, default=600)
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--map-renderer", choices=("cpu", "gpu"))
    parser.add_argument(
        "--measurement-mode", choices=("grab", "native-render"), default="grab"
    )
    parser.add_argument("--native-drain-timeout-s", type=float, default=2)
    parser.add_argument("--synchronous-base-charts", action="store_true")
    parser.add_argument("--profile-weather", action="store_true")
    parser.add_argument("--profile-ui", action="store_true")
    parser.add_argument("--wind-style", choices=("barbs", "streamlines"))
    parser.add_argument("--wind-over-land", choices=("show", "hide"))
    parser.add_argument("--uncached-coverage-fields", action="store_true")
    parser.add_argument("--uncached-weather-passes", action="store_true")
    parser.add_argument("--legacy-weather-scheduling", action="store_true")
    parser.add_argument("--preserve-covered-weather-jobs", action="store_true")
    parser.add_argument("--weather-source-limit", type=int, default=None)
    parser.add_argument("--disable-weather-field-cache", action="store_true")
    parser.add_argument("--weather-prefetch", action="store_true")
    parser.add_argument("--fixed-pan-lookahead", action="store_true")
    parser.add_argument("--disable-zoom-prefetch", action="store_true")
    parser.add_argument("--trackpad-step-px", type=int, default=16)
    args = parser.parse_args()
    try:
        if args.capture_current:
            manifest = capture(
                args.data_dir or default_data_directory(), args.capture_current
            )
            print(
                f"Captured {len(manifest['files'])} files ({sum(f['bytes'] for f in manifest['files']) / 1048576:.1f} MiB). Private snapshot: {args.capture_current}"
            )
            return
        if args.output is None:
            parser.error("--output is required for replay/comparison")
        if args.worker:
            report = worker(json.loads(args.worker.read_text()))
        elif args.compare:
            before, after = (json.loads(p.read_text()) for p in args.compare)
            report = protocol.compare_reports(before, after)
            report["weather_content"] = compare_weather_content(before, after)
        else:
            if min(args.frames, args.repeats) < 1 or args.warmup_frames < 0:
                parser.error("frames/repeats must be positive; warmup nonnegative")
            if not math.isfinite(args.interval_ms) or args.interval_ms < 0:
                parser.error("interval must be finite and nonnegative")
            if not all(
                math.isfinite(v) and v > 0
                for v in (
                    args.settle_timeout_s,
                    args.worker_timeout_s,
                    args.native_drain_timeout_s,
                )
            ):
                parser.error("timeouts must be finite and positive")
            report = run(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Startup benchmark failed: {exc}\n")


if __name__ == "__main__":
    main()
