#!/usr/bin/env python3
"""Native ENC acceptance for the experimental viewport (issue #78).

The viewport's chart layer must present the application's own ENC: the same
cells, drawn by the application's unchanged ``ENCOverlayRenderer``, composed on
the layer's own worker and read back natively from the scene graph's
framebuffer at the GUI's device pixel ratio.

The reference is assembled here, in the application's own order: base chart,
then ENC, then pressure contours.  The base chart and the contours are the same
code on both sides and act as controls; the ENC is the variable.

Spans reported here are wall-clock proxies.  They are not presentation FPS and
not GPU time, and they are never thresholded.
"""

import argparse
import hashlib
import importlib.util
import json
import shutil
import socket
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Lock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXPECTED_SNAPSHOT_SHA = (
    "1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03"
)
DEFAULT_SCENARIOS = ("san-francisco", "puget", "multi-cell")
WIDGET_WIDTH = 710
WIDGET_HEIGHT = 743
BUDGET_S = 95.0
MAE_LIMIT = 1.0
P99_LIMIT = 8.0
RATIO_LOW = 0.95
RATIO_HIGH = 1.05
INK_EPSILON = 0.5
CHECK_NAMES = (
    "fixture_verified",
    "detailed_enc_present",
    "source_selection_exact",
    "multi_cell_coverage_exercised",
    "zero_alpha_preserved",
    "annotations_faithful",
    "native_metal_dpr2",
    "native_chart_nonblank",
    "stale_chart_hidden",
    "camera_weather_advance_while_chart_stalled",
    "latest_generation_only",
    "resources_bounded_and_released",
)

# Fidelity cameras: the detailed prior-comparable cells and a multi-cell view.
FIDELITY = {
    "san-francisco": dict(lat=37.8375, lon=-122.4375, zoom=14.236),
    "puget": dict(lat=48.1125, lon=-122.7375, zoom=14.236),
    "multi-cell": dict(lat=48.1500, lon=-124.8000, zoom=13.0),
}
# Diagnostics: scale visibility and coverage miss.  Metadata only.
NEGATIVE = {
    "san-francisco-zoom11": dict(lat=37.836, lon=-122.475, zoom=11.0),
    "san-francisco-zoom10": dict(lat=37.836, lon=-122.475, zoom=10.0),
    "coverage-miss": dict(lat=30.0, lon=-150.0, zoom=13.0),
}


def _load_tool(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha_of_entries(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def _pump(milliseconds):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def _wait(predicate, timeout_s, step_ms=50):
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if predicate():
            return True
        _pump(step_ms)
    return bool(predicate())


def _grab(widget):
    """The scene graph's framebuffer as premultiplied BGRA bytes (native)."""
    import numpy as np

    from PySide6.QtGui import QImage

    image = widget.grabFramebuffer().convertToFormat(
        QImage.Format.Format_ARGB32_Premultiplied
    )
    if image.isNull():
        raise RuntimeError("framebuffer readback failed")
    return (
        np.frombuffer(image.constBits(), np.uint8)
        .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
        .reshape(image.height(), image.width(), 4)
        .copy()
    )


def _surface_array(surface):
    """A cairo ARGB32 surface as BGRA bytes, the same layout as the framebuffer."""
    import numpy as np

    surface.flush()
    stride = surface.get_stride()
    height = surface.get_height()
    width = surface.get_width()
    data = np.frombuffer(bytes(surface.get_data()), np.uint8)
    return data.reshape(height, stride)[:, : width * 4].reshape(height, width, 4).copy()


def _array_surface(array):
    import cairo
    import numpy as np

    height, width = array.shape[:2]
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    target = np.frombuffer(surface.get_data(), np.uint8).reshape(
        height, surface.get_stride()
    )
    target[:, : width * 4] = np.ascontiguousarray(array).reshape(height, width * 4)
    surface.mark_dirty()
    return surface


def _ink(array):
    """Pixels with any channel differing from zero beyond the epsilon."""
    return (array.astype("int16") > INK_EPSILON).any(axis=2)


def _fidelity(native, ref_on, ref_off):
    """Metrics of the native ENC layer against the reference, premultiplied BGRA."""
    import numpy as np

    on = ref_on.astype("int16")
    off = ref_off.astype("int16")
    nat = native.astype("int16")
    diff = np.abs(nat - on)
    ref_mask = (np.abs(on - off) > INK_EPSILON).any(axis=2)
    native_mask = (np.abs(nat - off) > INK_EPSILON).any(axis=2)
    both = ref_mask & native_mask
    precision = float(both.sum() / max(native_mask.sum(), 1))
    recall = float(both.sum() / max(ref_mask.sum(), 1))
    ref_ink = np.abs(on - off).sum()
    native_ink = np.abs(nat - off).sum()
    coverage = float(native_ink / ref_ink) if ref_ink else 0.0
    ref_alpha = on[:, :, 3].sum()
    native_alpha = nat[:, :, 3].sum()
    alpha_coverage = float(native_alpha / ref_alpha) if ref_alpha else 0.0
    zero = on[:, :, 3] == 0
    interior = zero.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            interior &= np.roll(np.roll(zero, dy, axis=0), dx, axis=1)
    zero_violations = int((interior & (nat[:, :, 3] > 0)).sum())
    return {
        "mae_255": float(diff.mean()),
        "p99_255": float(np.percentile(diff, 99)),
        "nonblank": bool(ref_mask.any() and native_mask.any() and nat[:, :, 3].any()),
        "native_visible_pixels": int((nat[:, :, 3] > 0).sum()),
        "precision": precision,
        "recall": recall,
        "coverage": coverage,
        "alpha_coverage": alpha_coverage,
        "zero_alpha_violations": zero_violations,
        "ref_ink_pixels": int(ref_mask.sum()),
        "native_ink_pixels": int(native_mask.sum()),
    }


def _rounded(value):
    """Nested geometry as plain numbers, rounded for a stable digest."""
    if isinstance(value, (list, tuple)):
        return tuple(_rounded(item) for item in value)
    if isinstance(value, float):
        return round(value, 6)
    return value


class _AnnotationProbe:
    """Records the S-57 drawing calls in order: identity, not pixels.

    The probe wraps the drawer's own methods and forwards every call, so the
    drawing is unchanged.  Both the candidate's worker and the reference run
    through the same class, so the recorded sequences are directly comparable.
    """

    def __init__(self):
        self._lock = Lock()
        self._phase = None
        self._records = []

    def install(self):
        from gweatherrouting.rendering.vectordrawer.s57chartdrawer import (
            S57ChartDrawer,
        )

        probe = self

        def wrap(name):
            original = getattr(S57ChartDrawer, name)

            def recorded(self, gpsmap, cr, feature, *args):
                probe._record(name, feature)
                return original(self, gpsmap, cr, feature, *args)

            setattr(S57ChartDrawer, name, recorded)

        for name in (
            "_render_area",
            "_render_line",
            "_render_point",
            "_render_soundings",
        ):
            wrap(name)

    def _record(self, kind, feature):
        if self._phase is None:
            return
        geometry = _rounded(feature.geometry)
        digest = hashlib.blake2b(
            repr((kind, feature.obj_name, geometry)).encode(), digest_size=8
        ).hexdigest()
        with self._lock:
            self._records.append((kind, feature.obj_name, digest))

    def begin(self, phase):
        with self._lock:
            self._phase = phase
            self._records = []

    def end(self):
        with self._lock:
            self._phase = None
            records = list(self._records)
            self._records = []
        return records


def _cell_provenance(snapshot, data_dir, settings):
    """The cells the fixture holds: catalog scale, manifest digest and state."""
    import xml.etree.ElementTree as ElementTree

    catalog = {}
    catalog_path = snapshot / "data" / "enc" / "catalog.xml"
    if catalog_path.is_file():
        for node in ElementTree.parse(catalog_path).iter():
            name = node.get("name")
            if name and node.get("cscale"):
                catalog[name.upper()] = int(node.get("cscale"))
    manifest = json.loads((snapshot / "manifest.json").read_text())
    digests = {
        entry["path"]: entry["sha256"]
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and "path" in entry and "sha256" in entry
    }
    rows = []
    for entry in settings.get("vectorCharts") or []:
        path = str(entry.get("path", ""))
        if ".000" not in path:
            continue
        name = Path(path).stem.upper()
        relative = path.replace("@APPDATA@/", "data/")
        rows.append(
            {
                "cell": name,
                "enabled": bool(entry.get("enabled", True)),
                "catalog_cscale": catalog.get(name),
                "sha256": digests.get(relative),
                "installed": (
                    data_dir / "enc" / name / "ENC_ROOT" / name / f"{name}.000"
                ).is_file(),
            }
        )
    return rows


def _reference_descriptors(data_dir, settings):
    """Enabled ENC cells read with GDAL, independently of the viewport's source."""
    from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
    from osgeo import ogr

    descriptors = []
    for entry in settings.get("vectorCharts") or []:
        path = str(entry.get("path", ""))
        if ".000" not in path or not entry.get("enabled", True):
            continue
        resolved = path.replace("@APPDATA@", str(data_dir))
        dataset = ogr.Open(resolved)
        if dataset is None:
            continue
        chart = GDALVectorChart.__new__(GDALVectorChart)
        chart.vector_file = dataset
        chart._coverage = []
        chart._native_scale = 0
        chart._load_enc_metadata()
        envelopes = tuple(geometry.GetEnvelope() for geometry in chart._coverage)
        descriptors.append((resolved, chart._native_scale, envelopes))
    return tuple(descriptors)


def _reference_selection(descriptors, camera_scale, viewport_envelope):
    """The application's selection rules, applied in the harness's own loop."""
    from gweatherrouting.core.enccharts import chart_scale_rank, chart_scale_visible

    min_lon, max_lon, min_lat, max_lat = viewport_envelope
    selected = []
    for path, native_scale, envelopes in sorted(
        descriptors, key=lambda c: (chart_scale_rank(c[1], camera_scale), c[0])
    ):
        if not chart_scale_visible(native_scale, camera_scale):
            continue
        if envelopes and not any(
            a <= max_lon and b >= min_lon and c <= max_lat and d >= min_lat
            for a, b, c, d in envelopes
        ):
            continue
        selected.append(Path(path).stem.upper())
    return tuple(selected)


def _base_job(camera, dpr, palette, sources, cancel):
    from gweatherrouting.core.mapviewport import MapViewport
    from gweatherrouting.qt.gribviewportchart import ChartCamera
    from gweatherrouting.rendering.overlayrenderer import MARGIN, RenderJob

    canvas = ChartCamera(camera, dpr)
    viewport = MapViewport(canvas, MARGIN, lambda: None)
    viewport.render_cancelled = cancel.is_set
    return RenderJob(
        key=(palette, dpr, sources),
        viewport=viewport,
        charts=tuple((source,) for source in sources),
        factor=dpr,
        palette=palette,
        cancel=cancel,
    )


def _reference_composition(layer, camera, dpr, palette, data_dir, descriptors, probe):
    """Base, then ENC, then contours: the application's order, assembled here."""
    import cairo
    from gweatherrouting.qt.gribviewportchart import _ChartComposer, base_sources
    from gweatherrouting.rendering.encrenderer import ENCOverlayRenderer
    from gweatherrouting.core.scheduling import schedule_idle
    from gweatherrouting.rendering.overlayrenderer import MARGIN
    from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

    class AnnotationDrawer(S57ChartDrawer):
        def _render_area(self, *args):
            pass

        def _render_line(self, *args):
            pass

    class AnnotationRenderer(ENCOverlayRenderer):
        def _worker_chart(self, path, native_scale, palette):
            chart = super()._worker_chart(path, native_scale, palette)
            chart.drawer.__class__ = AnnotationDrawer
            return chart

    sources = base_sources(data_dir)
    cancel = Event()
    job = _base_job(camera, dpr, palette, sources, cancel)
    composer = _ChartComposer(palette, sources)
    base, _none = composer.render(job, None, None)
    off = _array_surface(_surface_array(base))
    off.set_device_scale(dpr, dpr)
    columns = layer._isobar_columns(job, camera, cancel)
    if columns:
        composer.painter().draw_isobars(job.viewport, cairo.Context(off), columns)
    enc_job = replace(job, charts=descriptors)
    oracle = ENCOverlayRenderer(schedule_idle)
    try:
        enc = oracle._render(enc_job)
    finally:
        oracle._charts.clear()
    records = probe.end()
    # Record annotation identity only from the full oracle pass above.
    annotation_renderer = AnnotationRenderer(schedule_idle)
    try:
        annotations = annotation_renderer._render(enc_job)
    finally:
        annotation_renderer._charts.clear()
    on = base
    if enc is not None:
        context = cairo.Context(on)
        context.set_source_surface(enc, 0, 0)
        context.paint()
        on.flush()
    if columns:
        composer.painter().draw_isobars(job.viewport, cairo.Context(on), columns)
    on.flush()
    offset = MARGIN * dpr
    width = camera.width * dpr
    height = camera.height * dpr

    def crop(array):
        return array[offset : offset + height, offset : offset + width]

    return (
        crop(_surface_array(on)),
        crop(_surface_array(off)),
        crop(_surface_array(annotations)),
        records,
    )


def _cell_masks(descriptors, camera, dpr, palette, data_dir):
    """One cell alone, drawn by the oracle: the per-cell ink used for multi-cell checks."""
    from gweatherrouting.rendering.encrenderer import ENCOverlayRenderer
    from gweatherrouting.core.scheduling import schedule_idle
    from gweatherrouting.qt.gribviewportchart import base_sources
    from gweatherrouting.rendering.overlayrenderer import MARGIN

    sources = base_sources(data_dir)
    job = _base_job(camera, dpr, palette, sources, Event())
    oracle = ENCOverlayRenderer(schedule_idle)
    masks = {}
    try:
        for descriptor in descriptors:
            surface = oracle._render(replace(job, charts=(descriptor,)))
            if surface is None:
                continue
            array = _surface_array(surface)
            offset = MARGIN * dpr
            array = array[
                offset : offset + camera.height * dpr,
                offset : offset + camera.width * dpr,
            ]
            masks[Path(descriptor[0]).stem.upper()] = _ink(array)
    finally:
        oracle._charts.clear()
    return masks


def _scenario_camera(base_camera, spec):
    return replace(
        base_camera,
        center_lat=spec["lat"],
        center_lon=spec["lon"],
        zoom=spec["zoom"],
    )


def run(snapshot, output, scenarios, repeats, frames, experience=False):
    started = time.perf_counter()
    deadline = started + BUDGET_S
    report = {
        "gate": "enc-viewport",
        "tool": "tools/check_enc_viewport.py",
        "checks": {name: False for name in CHECK_NAMES},
        "fidelity": [],
        "scenarios": {},
        "negative": {},
        "multi_cell": {},
        "identity": {},
        "behavior": {},
        "timings_s": {},
        "snapshot_sha256": "",
        "complete": False,
        "notes": [
            "Spans are wall-clock proxies, not presentation FPS or GPU time.",
            "Weather fill is off in the chart scene; barbs are hidden for the chart-only readback.",
        ],
    }
    fixture = _load_tool(
        "viewport_fixture", ROOT / "tools/benchmark_startup_rendering.py"
    )
    try:
        manifest = fixture.verify_snapshot(snapshot)
    except (ValueError, OSError) as exc:
        report["error"] = f"fixture: {exc}"
        return report
    report["snapshot_sha256"] = manifest["content_sha256"]
    report["checks"]["fixture_verified"] = (
        manifest["content_sha256"] == EXPECTED_SNAPSHOT_SHA
    )
    if not report["checks"]["fixture_verified"]:
        report["error"] = "content_sha256 differs from the recorded fixture"
        return report

    socket.socket.connect = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("Network is disabled in the ENC acceptance tool")
    )
    sandbox_root = Path(tempfile.mkdtemp(prefix="enc-viewport-"))
    try:
        data_dir = fixture.prepare_sandbox(snapshot, sandbox_root)
        _measure(
            report, snapshot, data_dir, scenarios, repeats, frames, deadline, experience
        )
    except Exception as exc:  # the report must still say what happened
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)
    report["timings_s"]["total"] = round(time.perf_counter() - started, 3)
    return report


def _measure(
    report, snapshot, data_dir, scenarios, repeats, frames, deadline, experience=False
):
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.qt.gribviewport import (
        GribViewportModel,
        ViewportCamera,
        _viewport_widget_class,
        saved_selection,
    )
    from gweatherrouting.qt.gribviewportchart import ChartCamera  # noqa: F401
    from gweatherrouting.qt.gribviewport import saved_chart_palette
    from gweatherrouting.qt.viewportenc import read_manifest  # noqa: F401
    from gweatherrouting.rendering.windrenderer import WindLandMask

    app = QApplication.instance() or QApplication([])
    paths, settings = saved_selection(data_dir)
    palette = saved_chart_palette(data_dir, settings)
    report["identity"]["cells"] = _cell_provenance(snapshot, data_dir, settings)
    descriptors = _reference_descriptors(data_dir, settings)
    report["identity"]["reference_cells"] = [
        {"cell": Path(p).stem.upper(), "native_scale": s} for p, s, _e in descriptors
    ]

    probe = _AnnotationProbe()
    probe.install()

    manifest_time = datetime.fromisoformat("2026-10-19T08:00:00")
    base_camera = ViewportCamera(
        center_lat=0.0,
        center_lon=0.0,
        zoom=12.0,
        width=WIDGET_WIDTH,
        height=WIDGET_HEIGHT,
        time=manifest_time,
        density="very_dense",
        over_land=False,
    )
    model = GribViewportModel(
        paths,
        land_mask=WindLandMask(data_dir),
        resident_time_window=True,
        grid=True,
    )
    widget = _viewport_widget_class()(
        model,
        gpu_weather_fill=experience,
        chart_layer=True,
        chart_data_dir=data_dir,
        enc_chart_layer=True,
        chart_palette=palette,
    )
    widget.resize(WIDGET_WIDTH, WIDGET_HEIGHT)
    widget.show()
    try:
        if experience:
            _experience(report, widget, base_camera, deadline)
            return
        _run_session(
            report,
            widget,
            model,
            base_camera,
            descriptors,
            palette,
            data_dir,
            probe,
            scenarios,
            repeats,
            frames,
            deadline,
        )
    finally:
        widget.close()
        model.close()
        report["complete"] = report.get("complete", False)
        app.processEvents()


def _run_session(  # noqa: C901 - one session keeps the widget and worker together
    report,
    widget,
    model,
    base_camera,
    descriptors,
    palette,
    data_dir,
    probe,
    scenarios,
    repeats,
    frames,
    deadline,
):
    import numpy as np
    from gweatherrouting.qt.gribviewport import ViewportCamera  # noqa: F401

    dpr = int(round(widget.devicePixelRatio()))
    report["identity"]["platform"] = _platform(widget)
    report["checks"]["native_metal_dpr2"] = bool(
        "metal" in report["identity"]["platform"]["scene_graph_backend"].lower()
        and int(round(dpr)) == 2
    )

    widget.set_camera(base_camera)
    ready_weather = _wait(lambda: widget._weather_ready_at is not None, 45)
    report["identity"]["weather_ready"] = bool(ready_weather)
    if not ready_weather:
        raise RuntimeError(
            "Weather did not become ready; chart readiness is not forced"
        )
    if widget._barbs is not None:
        widget._barbs.setVisible(False)
    if widget._status is not None:
        widget._status.setVisible(False)
    layer = widget._chart_model
    if layer is None:
        report["error"] = "chart layer unavailable"
        return

    def composed(camera):
        probe.begin("candidate")
        before = widget.counters["chart_publications"]
        widget.set_camera(camera)
        layer.request(camera, replace=True)
        ok = _wait(
            lambda: layer.state == "ready"
            and widget.counters["chart_publications"] > before
            and bool(widget._chart.property("ready"))
            and layer.enc_metadata.get("state")
            in ("ready", "coverage_miss", "unavailable", "disabled")
            and layer.enc_metadata.get("selected") is not None,
            20,
        )
        candidate_records = probe.end()
        candidate_records_by_camera.append(candidate_records)
        ok = ok and _wait(widget._weather.isVisible, 15)
        # Weather callbacks may restore the barbs while a chart is loading.
        # Isolate the chart for readback only after those callbacks settle.
        widget._barbs.setVisible(False)
        _pump(100)
        return ok

    candidate_records_by_camera = []
    spread = []
    orders = [
        list(scenarios) if index % 2 == 0 else list(reversed(scenarios))
        for index in range(max(1, repeats))
    ]
    report["scenario_orders"] = orders
    for name in [name for order in orders for name in order]:
        if name not in FIDELITY:
            continue
        if time.perf_counter() > deadline - 15:
            report["error"] = "budget exhausted before all scenarios"
            return
        spec = FIDELITY[name]
        camera = _scenario_camera(base_camera, spec)
        ok = composed(camera)
        meta = layer.enc_metadata
        if not ok:
            report["scenarios"][name] = {"ready": False, "state": meta.get("state")}
            continue
        expected_selection, reference_coverage = _coverage_selection(
            descriptors, camera, dpr, palette
        )
        candidate_selection = tuple(meta.get("selected") or ())
        report["identity"].setdefault(name, {})["expected"] = expected_selection
        report["identity"][name]["candidate"] = candidate_selection
        report["identity"][name]["coverage_matches"] = (
            list(meta.get("coverage", ())) == reference_coverage
        )
        if name == "multi-cell":
            report["multi_cell"] = {
                "cells": list(candidate_selection),
                "regions_match": report["identity"][name]["coverage_matches"],
            }
            report["checks"]["multi_cell_coverage_exercised"] = (
                len(candidate_selection) >= 2
                and report["identity"][name]["coverage_matches"]
            )
        probe.begin("reference")
        native_frames = []
        for _ in range(max(1, frames)):
            native_frames.append(_grab(widget))
        probe.end()
        native = native_frames[-1]
        stable = all(np.array_equal(native, frame) for frame in native_frames)
        probe.begin("reference")
        ref_on, ref_off, annotations, reference_records = _reference_composition(
            layer, camera, dpr, palette, data_dir, descriptors, probe
        )
        report["identity"][name]["annotations_match"] = bool(
            candidate_records_by_camera
            and candidate_records_by_camera[-1] == reference_records
            and reference_records
        )
        metrics = _fidelity(native, ref_on, ref_off)
        annotation_mask = annotations[:, :, 3] > 0
        matching = np.max(np.abs(native.astype("int16") - ref_on), axis=2) <= P99_LIMIT
        metrics["annotations"] = {
            "pixels": int(annotation_mask.sum()),
            "coverage": (
                float(matching[annotation_mask].mean())
                if annotation_mask.any()
                else 0.0
            ),
            "meaning": "fraction of reference annotation pixels matching within 8/255",
        }
        metrics.update(scenario=name, stable_readback=bool(stable))
        report["fidelity"].append(metrics)
        report["scenarios"][name] = {
            "state": meta.get("state"),
            "selected": list(candidate_selection),
            "expected": list(expected_selection),
        }
        spread.append(name)
    report["identity"]["selection_matches"] = all(
        report["identity"].get(name, {}).get("candidate")
        == report["identity"].get(name, {}).get("expected")
        and report["identity"][name]["coverage_matches"]
        for name in spread
    ) and bool(spread)
    report["checks"]["source_selection_exact"] = report["identity"]["selection_matches"]
    report["checks"]["detailed_enc_present"] = all(
        m["nonblank"]
        for m in report["fidelity"]
        if m["scenario"] in ("san-francisco", "puget")
    ) and any(m["scenario"] in ("san-francisco", "puget") for m in report["fidelity"])
    report["checks"]["native_chart_nonblank"] = bool(report["fidelity"]) and all(
        m["native_ink_pixels"] > 0 and m["native_visible_pixels"] > 0
        for m in report["fidelity"]
    )
    report["checks"]["zero_alpha_preserved"] = bool(report["fidelity"]) and all(
        m["zero_alpha_violations"] == 0 for m in report["fidelity"]
    )
    report["checks"]["annotations_faithful"] = bool(spread) and all(
        report["identity"][name].get("annotations_match") for name in spread
    )
    report["complete"] = bool(report["fidelity"]) and len(report["fidelity"]) == (
        len(scenarios) * max(1, repeats)
    )
    if report["complete"]:
        # Weather responsiveness needs a view covered by the saved forecasts;
        # SF also exercises chart display when weather is unavailable.
        weather_camera = _scenario_camera(base_camera, FIDELITY["puget"])
        if not composed(weather_camera) or not _wait(
            lambda: widget._state == "ready", 15
        ):
            report["error"] = "Weather-covered view did not settle for lifecycle probe"
            return
        _behavior(report, widget, layer, deadline)


def _coverage_selection(descriptors, camera, dpr, palette):
    """Independently compute the oracle's clipped, non-overlapping regions."""
    from osgeo import ogr
    from gweatherrouting.core.enccharts import chart_scale_rank, chart_scale_visible
    from gweatherrouting.rendering.chartlayer import ChartLayer
    from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
    from gweatherrouting.qt.viewportenc import region_identity

    job = _base_job(camera, dpr, palette, (), Event())
    viewport = job.viewport
    p1, p2 = viewport.get_bbox()
    bounding = ogr.CreateGeometryFromWkt(
        ChartLayer.get_bounding_wkt_of_coords(*p1.get_degrees(), *p2.get_degrees())
    )
    covered = None
    identities = []
    for path, scale, _envelopes in sorted(
        descriptors,
        key=lambda row: (chart_scale_rank(row[1], viewport.get_scale()), row[0]),
    ):
        if not chart_scale_visible(scale, viewport.get_scale()):
            continue
        dataset = ogr.Open(path)
        geometries = GDALVectorChart.read_coverage(dataset)
        region = None
        for geometry in geometries:
            region = geometry.Clone() if region is None else region.Union(geometry)
        known = region is not None
        region = region.Intersection(bounding) if known else bounding.Clone()
        if covered is not None:
            region = region.Difference(covered)
        if region.IsEmpty():
            continue
        identities.append(region_identity(path, region))
        if known:
            covered = region if covered is None else covered.Union(region)
    return tuple(row["name"] for row in identities), identities


class _TextSizes:
    """Observe actual sounding text extents at two zooms, without an old renderer."""

    def __init__(self):
        self.stage = ""
        self.sizes = {}

    def install(self):
        from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

        original = S57ChartDrawer._render_soundings
        observer = self

        class Context:
            def __init__(self, context):
                self.context = context

            def __getattr__(self, name):
                return getattr(self.context, name)

            def show_text(self, text):
                extents = self.context.text_extents(text)
                dimensions = self.context.user_to_device_distance(
                    extents[2], extents[3]
                )
                observer.sizes.setdefault(observer.stage, {})[text] = tuple(dimensions)
                return self.context.show_text(text)

        def draw(drawer, gpsmap, context, feature):
            return original(drawer, gpsmap, Context(context), feature)

        S57ChartDrawer._render_soundings = draw
        return lambda: setattr(S57ChartDrawer, "_render_soundings", original)


def _experience(report, widget, base_camera, deadline):  # noqa: C901
    """Combined scene acceptance, not pixel parity with a legacy viewport."""
    import numpy as np

    names = (
        "combined_layers_visible",
        "fractional_zoom_recovers",
        "screen_space_labels",
        "resize_recovers",
        "rapid_reversals_settle",
        "coverage_miss_explicit",
        "coverage_recovery",
        "payload_bounded",
        "camera_weather_advance_while_chart_stalled",
        "stale_chart_hidden",
        "latest_generation_only",
        "resources_bounded_and_released",
    )
    report["checks"] = {name: False for name in names}
    report["checks"]["fixture_verified"] = True
    report["notes"] = [
        "Chart, fill and barbs are visible together; no legacy pixel comparison.",
        "Stage wall spans include readiness waits, not FPS or presentation latency.",
    ]
    report["experience"] = {}
    payload_bounds = []
    layer = widget._chart_model
    platform = _platform(widget)
    report["identity"]["platform"] = platform
    report["checks"]["native_metal_dpr2"] = (
        "metal" in platform["scene_graph_backend"].lower()
        and platform["device_pixel_ratio"] == 2
    )

    def source():
        return widget._chart.property("source").toString()

    def wait(predicate, timeout=12):
        return _wait(predicate, max(0, min(timeout, deadline - time.perf_counter())))

    def ready():
        return (
            layer.state == "ready"
            and bool(widget._chart.property("ready"))
            and bool(source())
            and widget._weather.isVisible()
        )

    def settle(name, camera=None, weather=True):
        began = time.perf_counter()
        if camera is not None:
            widget.set_camera(camera)
        ok = wait(lambda: ready() and (not weather or widget._state == "ready"))
        _pump(80)
        meta = layer.enc_metadata
        camera_now = widget._camera
        from gweatherrouting.rendering.overlayrenderer import MARGIN

        byte_bound = (
            (camera_now.width + 2 * MARGIN)
            * (camera_now.height + 2 * MARGIN)
            * int(round(widget.devicePixelRatio())) ** 2
            * 4
        )
        payload_bounds.append(
            layer.observations()["payload_bytes"] <= byte_bound
            and len(widget._chart_provider._images) <= 1
        )
        report["experience"][name] = {
            "ready": ok,
            "weather_state": widget._state,
            "enc_state": meta.get("state"),
            "selected": list(meta.get("selected", ())),
            "wall_s": round(time.perf_counter() - began, 3),
            "accounted_payload_bytes": layer.observations()["payload_bytes"],
            "single_image_bound_bytes": byte_bound,
        }
        return ok

    camera = _scenario_camera(base_camera, FIDELITY["puget"])
    sizes = _TextSizes()
    restore = sizes.install()
    try:
        sizes.stage = "initial"
        startup = settle("initial", camera)
        layers = (widget._chart, widget._barbs, widget._fill)
        frame = _grab(widget)
        report["checks"]["combined_layers_visible"] = bool(
            startup
            and all(item is not None and item.isVisible() for item in layers)
            and all(bool(item.property("ready")) for item in layers)
            and (frame[:, :, 3] > 0).any()
        )

        sizes.stage = "zoom"
        widget.zoom_at(0.375)
        zoom_hidden = not source()
        zoomed = settle("fractional_zoom")
        report["checks"]["fractional_zoom_recovers"] = bool(
            zoom_hidden and zoomed and layer.enc_metadata.get("selected")
        )
        first = sizes.sizes.get("initial", {})
        second = sizes.sizes.get("zoom", {})
        common = first.keys() & second.keys()
        report["checks"]["screen_space_labels"] = bool(common) and all(
            np.allclose(first[text], second[text], rtol=0, atol=1e-6) for text in common
        )
        report["experience"]["labels"] = {
            "common_soundings": len(common),
            "initial_texts": len(first),
            "zoom_texts": len(second),
            "example": next(iter(common), None),
        }

        widget.resize(850, 620)
        resized = settle("resize")
        shape = _grab(widget).shape
        report["checks"]["resize_recovers"] = bool(
            resized
            and widget._camera.width == 850
            and widget._camera.height == 620
            and shape[:2] == (1240, 1700)
        )
        for amount in (100, -200, 150, -50):
            widget.pan_pixels(amount, 0)
        # A zoom invalidates retained scale and exercises replacement after reversal.
        widget.zoom_at(-0.125)
        reversed_ok = settle("rapid_reversals")
        report["checks"]["rapid_reversals_settle"] = bool(
            reversed_ok and layer.enc_metadata.get("selected")
        )

        covered = widget._camera
        ocean = replace(covered, center_lat=30, center_lon=-150)
        widget.set_camera(ocean)
        hidden = not source()
        missing = settle("coverage_miss", weather=False)
        report["checks"]["coverage_miss_explicit"] = bool(
            hidden
            and missing
            and layer.enc_state == "coverage_miss"
            and not layer.enc_metadata.get("selected")
            and widget._weather.property("encStatus")
        )
        recovered = settle("coverage_recovery", covered)
        report["checks"]["coverage_recovery"] = bool(
            recovered
            and layer.enc_state == "ready"
            and layer.enc_metadata.get("selected")
        )
        report["complete"] = all(report["checks"][key] for key in names[:7])
        report["checks"]["payload_bounded"] = bool(payload_bounds) and all(
            payload_bounds
        )
        if report["complete"]:
            _behavior(report, widget, layer, deadline)
            widget.close()
            _pump(50)
            report["checks"]["resources_bounded_and_released"] &= (
                layer.observations()["payload_bytes"] == 0
                and layer._composer_instance is None
                and not widget._chart_provider._images
            )
    finally:
        restore()


def _behavior(report, widget, layer, deadline):  # noqa: C901 - four runtime checks
    """Stall, supersession and release.  Each check reads the layer's own records."""
    from gweatherrouting.qt.gribviewportchart import HISTORY_LIMIT

    def chart_source():
        if widget._chart is None:
            return ""
        source = widget._chart.property("source")
        return source.toString() if hasattr(source, "toString") else str(source or "")

    def weather_publications():
        counters = widget.publication_counters()
        return sum(
            value
            for key, value in counters.items()
            if key.endswith("_publications") and not key.startswith("chart")
        )

    def settled(minimum_generation):
        observations = layer.observations()
        return (
            layer.state == "ready"
            and observations["generation"] >= minimum_generation
            and observations["generation"] in set(observations["published_generations"])
        )

    real_compose = layer._compose
    entered = Event()
    release = Event()

    def blocking(camera, cancel):
        entered.set()
        release.wait(20)
        return real_compose(camera, cancel)

    layer._compose = blocking
    try:
        # 1. The chart is stalled; the camera and the weather must keep moving.
        before_center = widget._camera.center_lon
        before_weather = weather_publications()
        layer.request(widget._camera, replace=True)
        blocked = _wait(entered.is_set, 5)
        widget.pan_pixels(60, 0)
        _wait(
            lambda: widget._camera.center_lon != before_center
            and weather_publications() > before_weather,
            6,
        )
        report["behavior"]["stalled_state"] = layer.state
        report["behavior"]["camera_moved"] = widget._camera.center_lon != before_center
        report["behavior"]["weather_advanced"] = weather_publications() > before_weather
        report["checks"]["camera_weather_advance_while_chart_stalled"] = bool(
            layer.state == "loading"
            and blocked
            and report["behavior"]["camera_moved"]
            and report["behavior"]["weather_advanced"]
        )
        release.set()
        _wait(lambda: settled(0), 20)

        # 2. Supersede a composing generation with a newer request.
        entered.clear()
        release.clear()
        probe = replace(widget._camera, time=widget._camera.time + timedelta(hours=1))
        widget.set_camera(probe)
        hidden_during_replacement = not chart_source()
        _wait(entered.is_set, 10)
        superseded = layer.observations()["generation"]
        layer.request(probe, replace=True)
        release.set()
        latest = layer.observations()["generation"]
        _wait(lambda: settled(latest), 25)
        observations = layer.observations()
        published = set(observations["published_generations"])
        rejected = set(observations["rejected_generations"])
        newest = max(published) if published else None
        report["behavior"]["superseded_generation"] = superseded
        report["behavior"]["published_generations"] = sorted(published)
        report["behavior"]["rejected_generations"] = sorted(rejected)
        report["checks"]["stale_chart_hidden"] = bool(
            hidden_during_replacement
            and superseded in rejected
            and superseded not in published
            and newest is not None
            and chart_source().endswith(f"/{newest}")
        )
        report["checks"]["latest_generation_only"] = bool(
            not (published & rejected)
            and newest == observations["generation"]
            and all(
                observations["outcomes"].get(generation) == "published"
                for generation in published
            )
        )
    finally:
        release.set()
        layer._compose = real_compose

    # 4. Bounded history, and every handle released on close.
    observations = layer.observations()
    bounded = len(observations["outcomes"]) <= HISTORY_LIMIT
    report["behavior"]["history_entries"] = len(observations["outcomes"])
    layer.close()
    thread = getattr(layer, "_thread", None)
    if thread is not None:
        thread.join(10)
    released = layer._enc_source is None and (thread is None or not thread.is_alive())
    report["checks"]["resources_bounded_and_released"] = bool(bounded and released)
    report["behavior"]["released"] = bool(released)


def _platform(widget):
    import os
    import platform

    import PySide6
    from PySide6.QtQuick import QSGRendererInterface

    quick = widget.quickWindow()
    backend = ""
    try:
        interface = quick.rendererInterface()
        api = interface.graphicsApi() if interface is not None else None
        if api is not None:
            backend = QSGRendererInterface.GraphicsApi(api).name
    except Exception:
        backend = ""
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "qt": PySide6.__version__,
        "scene_graph_backend": backend,
        "requested_backend": os.environ.get("QSG_RHI_BACKEND", "default"),
        "device_pixel_ratio": float(quick.devicePixelRatio()),
    }


def _viewport_envelope(camera):
    from gweatherrouting.core.mapviewport import MapViewport
    from gweatherrouting.qt.gribviewportchart import ChartCamera
    from gweatherrouting.rendering.overlayrenderer import MARGIN

    viewport = MapViewport(ChartCamera(camera, 1), MARGIN, lambda: None)
    p1, p2 = viewport.get_bbox()
    lat1, lon1 = p1.get_degrees()
    lat2, lon2 = p2.get_degrees()
    return (min(lon1, lon2), max(lon1, lon2), min(lat1, lat2), max(lat1, lat2))


def _camera_scale(camera):
    from gweatherrouting.core.mapviewport import MapViewport
    from gweatherrouting.qt.gribviewportchart import ChartCamera
    from gweatherrouting.rendering.overlayrenderer import MARGIN

    return MapViewport(ChartCamera(camera, 1), MARGIN, lambda: None).get_scale()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS))
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--frames", type=int, default=10)
    parser.add_argument("--experience-only", action="store_true")
    args = parser.parse_args(argv)
    report = run(
        args.snapshot,
        args.output,
        tuple(name for name in args.scenarios.split(",") if name),
        args.repeats,
        args.frames,
        args.experience_only,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
