"""Present-to-present frame budget for the static ENC vertex frame.

Measures ``frameSwapped`` intervals (no grab, no readback) for:
  pan_1x      300 translation-only frames at the published cell (~106k vertices)
  pan_4x      300 translation-only frames at ~4x vertices
  zoom        6 zoom steps, 25 frames each, no data change
  floor       item hidden, control

``ENC_LEGACY_CPU_PROJECT=1`` measures the same phases on the original
per-frame float64 re-projection, for A/B comparison.

Every GUI run is bounded by ``signal.setitimer(SIGALRM)`` on the main thread.
"""

import json
import math
import os
import signal
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.pop("QSG_RENDER_LOOP", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
TMP = Path("/private/var/folders/w7/hb5wv1x1715821rqd1_3xf4r0000gn/T/opencode")
SNAPSHOT = Path.home() / "Library/Caches/sailworks-bench/snapshot"
LAT, LON, ZOOM = 48.1125, -122.7375, 14.236
WIDTH, HEIGHT, DPR = 710, 743, 2
STAMP = datetime(2026, 11, 15, 18, tzinfo=timezone.utc)
PALETTE = "dark"
DENSITY = os.environ.get("ENC_DENSITY", "very_dense")
ALARM_S = 300
N_FRAMES = 300
ZOOM_STEPS = 6
ZOOM_FRAMES = 25


def ms(value):
    return None if value is None else round(value * 1000.0, 3)


def pctl(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarise(values):
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": ms(pctl(values, 0.50)),
        "p95": ms(pctl(values, 0.95)),
        "max": ms(max(values)),
        "mean": ms(statistics.fmean(values)),
    }


def prepare_data_dir():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "startup_bench", ROOT / "tools/benchmark_startup_rendering.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    fixture.verify_snapshot(SNAPSHOT)
    root = Path(tempfile.mkdtemp(prefix="enc-sm-", dir=str(TMP)))
    data_dir = Path(fixture.prepare_sandbox(SNAPSHOT, root))
    settings_path = data_dir / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["chartENCVisible"] = True
    settings_path.write_text(json.dumps(settings, indent=1))
    return data_dir


def make_viewport(lat, lon, zoom):
    from sailworks.core.mapviewport import MapViewport
    from sailworks.qt.gribviewport import ViewportCamera
    from sailworks.qt.gribviewportchart import ChartCamera
    from sailworks.rendering.overlayrenderer import MARGIN

    camera = ViewportCamera(
        center_lat=lat,
        center_lon=lon,
        zoom=zoom,
        width=WIDTH,
        height=HEIGHT,
        time=STAMP,
        density=DENSITY,
        over_land=False,
    )
    return camera, MapViewport(ChartCamera(camera, DPR), MARGIN, lambda: None)


class TiledScene:
    """``k`` copies of a scene's positions, offset in scene units."""

    parts = ()
    cells = ()

    def __init__(self, positions, k, step):
        import numpy as np

        chunks = [positions + (i * step).reshape(1, 2) for i in range(k)]
        self.positions = chunks[0] if k == 1 else np.vstack(chunks)

    @property
    def vertex_count(self):
        return int(self.positions.shape[0])


def main():
    import numpy as np
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QGuiApplication, QSurfaceFormat
    from PySide6.QtQuick import QQuickWindow, QSGRendererInterface

    from sailworks.qt import encgpu
    from sailworks.qt.encgpu import EncMeshItem
    from sailworks.qt.encscenelayer import (
        STATE_READY,
        EncSceneLayer,
        projection_for,
        scene_report,
    )
    from sailworks.qt.gribviewport import enc_scene_catalog

    out = {
        "legacy": bool(encgpu.LEGACY_CPU_PROJECT),
        "view": {
            "w": WIDTH,
            "h": HEIGHT,
            "dpr": DPR,
            "lat": LAT,
            "lon": LON,
            "zoom": ZOOM,
        },
        "phases": {},
        "pixel_ratio": None,
        "errors": [],
    }

    def fail(message):
        out["errors"].append(message)
        print("FAIL:", message, flush=True)
        raise SystemExit(1)

    data_dir = prepare_data_dir()
    published = {"n": 0, "scene": None}

    def on_publish(payload):
        scene = payload[1] if isinstance(payload, tuple) else payload
        if hasattr(scene, "scene"):
            scene = scene.scene
        published["n"] += 1
        published["scene"] = scene

    if sys.platform == "darwin":
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Metal)
    fmt = QSurfaceFormat()
    fmt.setSamples(4)
    fmt.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QGuiApplication.instance() or QGuiApplication([])

    window = QQuickWindow()
    window.resize(QSize(WIDTH, HEIGHT))
    window.setColor(QColor.fromRgbF(0.1, 0.2, 0.3))
    item = EncMeshItem()
    item.setParentItem(window.contentItem())
    item.setWidth(WIDTH)
    item.setHeight(HEIGHT)
    item.setVisible(True)

    swaps = []
    window.frameSwapped.connect(lambda: swaps.append(time.perf_counter()))
    window.update()
    window.show()

    def pump():
        app.processEvents()

    def wait_swap(timeout=10.0, label=""):
        previous = len(swaps)
        deadline = time.perf_counter() + timeout
        while len(swaps) == previous:
            pump()
            if len(swaps) == previous:
                window.update()
            if time.perf_counter() > deadline:
                fail(f"no frameSwapped [{label}]")
            time.sleep(0.0001)

    wait_swap(label="initial")
    out["pixel_ratio"] = window.devicePixelRatio()

    layer = EncSceneLayer(
        data_dir,
        PALETTE,
        worker_idle=lambda: True,
        on_publish=on_publish,
        dpr=DPR,
        catalog_factory=enc_scene_catalog,
    )

    def gpu_assembly(camera):
        before = published["n"]
        started = time.perf_counter()
        layer.request(camera)
        deadline = time.perf_counter() + 40
        while True:
            pump()
            if published["n"] != before:
                state, report = scene_report(published["scene"])
                if state == STATE_READY:
                    return time.perf_counter() - started, report, published["scene"]
                if state == "unavailable":
                    fail(f"scene unavailable: {report.get('reason')}")
                before = published["n"]
            if time.perf_counter() > deadline:
                fail(f"no ready scene (state={layer.state} err={layer.error})")
            time.sleep(0.0002)

    camera, viewport = make_viewport(LAT, LON, ZOOM)
    build_s, report, scene = gpu_assembly(camera)
    base_proj = projection_for(viewport)
    out["build_ms"] = ms(build_s)
    out["scene"] = {
        "cells": report.get("cells"),
        "vertices": report.get("vertices"),
        "triangles": report.get("triangles"),
    }

    positions = np.ascontiguousarray(np.asarray(scene.positions, dtype=np.float64))
    probe = np.asarray(base_proj(np.array([[0.0, 0.0], [1.0, 0.0]])), dtype=np.float64)
    per_px_x = 1.0 / (abs(probe[1, 0] - probe[0, 0]) or 1.0)
    probe_y = np.asarray(
        base_proj(np.array([[0.0, 0.0], [0.0, 1.0]])), dtype=np.float64
    )
    per_px_y = 1.0 / (abs(probe_y[1, 1] - probe_y[0, 1]) or 1.0)
    step = np.array([3.0 * per_px_x, 3.0 * per_px_y])

    cpu = {"set_projection": [], "apply_colored_mesh": [], "frame": []}
    real_apply = encgpu.apply_colored_mesh
    record = {"on": False}

    def timed_apply(*args, **kwargs):
        started = time.perf_counter()
        try:
            return real_apply(*args, **kwargs)
        finally:
            if record["on"]:
                cpu["apply_colored_mesh"].append(time.perf_counter() - started)

    encgpu.apply_colored_mesh = timed_apply
    real_frame = item._update_static

    def timed_frame():
        started = time.perf_counter()
        try:
            return real_frame()
        finally:
            if record["on"]:
                cpu["frame"].append(time.perf_counter() - started)

    item._update_static = timed_frame
    real_set_proj = item.set_projection

    def timed_set_proj(projection):
        started = time.perf_counter()
        try:
            return real_set_proj(projection)
        finally:
            if record["on"]:
                cpu["set_projection"].append(time.perf_counter() - started)

    item.set_projection = timed_set_proj

    def run(name, mesh, projections, amp_px, visible=True):
        item.setVisible(visible)
        record["on"] = False
        item.set_projection(projections(0))
        if visible:
            item.set_view_scene(mesh)
        window.update()
        wait_swap(label=f"{name}-warm-a")
        wait_swap(label=f"{name}-warm-b")
        for key in cpu:
            cpu[key] = []
        uploads_before = item.upload_count

        intervals = []
        previous = swaps[-1]
        started_index = len(swaps)
        record["on"] = True
        phase_start = time.perf_counter()
        for index in range(len(projections.frames)):
            if visible:
                angle = 2.0 * math.pi * index / 90.0
                shift = amp_px * per_px_x * math.sin(angle)
                base = projections(index)
                projection = lambda points, s=shift, b=base: np.asarray(
                    b(points), dtype=np.float64
                ) + np.array([s, 0.0], dtype=np.float64)
                item.set_projection(projection)
                item.set_view_scene(mesh)
            else:
                window.setColor(QColor.fromRgbF(0.10 + 0.001 * (index % 3), 0.2, 0.3))
            window.update()
            deadline = time.perf_counter() + 6.0
            target = started_index + index + 1
            while len(swaps) < target:
                pump()
                if time.perf_counter() > deadline:
                    record["on"] = False
                    fail(f"{name}: stalled at frame {index}")
                time.sleep(0.0001)
            now = swaps[-1]
            intervals.append(now - previous)
            previous = now
        record["on"] = False
        out["phases"][name] = {
            "visible": visible,
            "frames": len(intervals),
            "interval": summarise(intervals),
            "mesh_vertices": int(getattr(mesh, "vertex_count", 0)) if visible else 0,
            "uploads_during_phase": item.upload_count - uploads_before,
            "matrix_updates": item.matrix_update_count,
            "cpu_ms_per_frame": {
                key: ms(statistics.fmean(values)) if values else None
                for key, values in cpu.items()
            },
        }
        print(name, json.dumps(out["phases"][name]), flush=True)
        return intervals

    class Fixed:
        """A projection that does not move: one frame of 'no change'."""

        def __init__(self, projection):
            self.projection = projection
            self.frames = range(N_FRAMES)

        def __call__(self, index):
            return self.projection

    class Pan(Fixed):
        pass

    class ZoomSteps:
        """Six zoom steps, no data change: only the projection moves."""

        def __init__(self, base_zoom, lat, lon):
            self.projections = []
            for index in range(ZOOM_STEPS):
                zoom = base_zoom + 0.4 * (1 if index % 2 else -1) * (index // 2 + 1)
                _, viewport = make_viewport(lat, lon, zoom)
                self.projections.extend([projection_for(viewport)] * ZOOM_FRAMES)
            self.frames = range(len(self.projections))

        def __call__(self, index):
            return self.projections[index]

    one = TiledScene(positions, 1, step)
    four = TiledScene(positions, 4, step)
    out["tiled_vertices"] = int(four.vertex_count)

    run("floor_item_hidden", one, Fixed(base_proj), 8.0, visible=False)
    base_iv = run("pan_1x", one, Pan(base_proj), 8.0)
    four_iv = run("pan_4x", four, Pan(base_proj), 8.0)
    run("zoom_steps", one, ZoomSteps(ZOOM, LAT, LON), 8.0)

    base_mean = statistics.fmean(base_iv)
    four_mean = statistics.fmean(four_iv)
    out["vertex_ratio"] = round(
        out["tiled_vertices"] / max(1, out["phases"]["pan_1x"]["mesh_vertices"]),
        2,
    )
    out["interval_ratio_4x_over_1x"] = (
        round(four_mean / base_mean, 2) if base_mean else None
    )

    path = TMP / (
        "enc_static_matrix_legacy.json" if out["legacy"] else "enc_static_matrix.json"
    )
    path.write_text(json.dumps(out, indent=2))
    print("RESULT_PATH", path, flush=True)
    print(
        "SUMMARY",
        json.dumps(
            {
                "legacy": out["legacy"],
                "pixel_ratio": out["pixel_ratio"],
                "scene": out["scene"],
                "tiled_vertices": out["tiled_vertices"],
                "vertex_ratio": out["vertex_ratio"],
                "interval_ratio": out["interval_ratio_4x_over_1x"],
                "pan_1x": out["phases"]["pan_1x"]["interval"],
                "pan_4x": out["phases"]["pan_4x"]["interval"],
                "zoom": out["phases"]["zoom_steps"]["interval"],
                "floor": out["phases"]["floor_item_hidden"]["interval"],
                "cpu_1x": out["phases"]["pan_1x"]["cpu_ms_per_frame"],
            }
        ),
        flush=True,
    )


def _alarm(_signal, _frame):
    print("ALARM: budget exceeded", flush=True)
    os._exit(3)


if __name__ == "__main__":
    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, ALARM_S)
    try:
        main()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
