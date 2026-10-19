"""A0j: live wind publication before independent chart-host readiness (experiment)."""

import argparse
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import architecture_run_protocol as protocol
import independent_numerical_service as numerical
import experiment_vertical_renderer as renderer
from persistent_coverage import PersistentPreparation, atomic_json

LIMIT = 64 * 1024 * 1024


def camera_at_offset(initial, dx):
    """Absolute screen displacement avoids repeated projection round-trip drift."""
    if dx == 0:
        return initial
    return replace(
        initial,
        _center_lon=initial._x_to_lon(initial._lon_to_x(initial._center_lon) - dx),
    )


def validate_identity(meta, reference):
    if meta["view"] != reference["view"] or meta["clock"] != reference["clocks"][0]:
        raise ValueError("Early publication view/clock differs")
    first = reference["field_meta"][0]
    for key in ("before", "after", "generation", "node_count"):
        if meta[key] != first[key]:
            raise ValueError(f"Early publication provenance differs: {key}")


def read_field(directory, reference):
    """Only NEW worker output is decoded here, not cached reference arrays."""
    from gweatherrouting.qt.gpuweather import encode_component

    meta = json.loads((directory / "field.json").read_text())
    validate_identity(meta, reference)
    path = directory / "field.npz"
    if path.stat().st_size > LIMIT or protocol.file_hash(path) != meta["field_sha256"]:
        raise ValueError("Early field checksum/size rejected")
    with np.load(path, allow_pickle=False) as source:
        arrays = {}
        size = 0
        for name in source.files:
            value = source[name]
            size += value.nbytes
            if size > LIMIT:
                raise ValueError("Decoded early field exceeds budget")
            value.setflags(write=False)
            arrays[name] = value
    bundle_meta = dict(
        view=meta["view"],
        field_meta=[dict(reference["field_meta"][0], file="new-field")],
    )
    prepared = renderer.prepared_fields(bundle_meta, {"new-field": arrays})[0]
    images = tuple(encode_component(arrays["field"][..., i]) for i in range(4))
    return meta, arrays, prepared, images


def rgba(image):
    image = image.convertToFormat(image.Format.Format_RGBA8888)
    return (
        np.frombuffer(image.constBits(), np.uint8)
        .reshape(image.height(), image.bytesPerLine())
        .copy()
    )


def run(args):
    args.directory.mkdir(parents=True, exist_ok=False)
    result = dict(
        protocol="early-weather-display-a0j-v1",
        presentation_measured=False,
        production_integrated=False,
        acceptance_scope="publication mechanism, not visual usability",
        visual_acceptance=False,
        known_reference_visual_artifact="red GPU overlay in nonempty wind readback; requires separate portrayal investigation",
        errors=[],
    )
    chart = None
    thread = None
    pool = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="early-display-preparation"
    )
    statuses = []
    try:
        began = time.perf_counter()
        manifest = renderer.startup.verify_snapshot(args.snapshot)
        reference = json.loads((args.reference / "resources.json").read_text())
        if reference["snapshot_sha256"] != manifest["content_sha256"]:
            raise ValueError("Early-display reference fixture differs")
        result["snapshot_sha256"] = manifest["content_sha256"]
        data = renderer.startup.prepare_sandbox(
            args.snapshot, args.directory / "sandbox"
        )
        shaders = args.directory / "shaders"
        shaders.mkdir()
        renderer.previous.shader_sources(
            args.app_root, shaders, safe_missing=args.safe_barbs
        )
        directory = args.directory / "numerical"
        directory.mkdir()
        config = dict(
            data=str(data),
            directory=str(directory),
            snapshot_sha256=manifest["content_sha256"],
        )
        atomic_json(args.directory / "config.json", config)
        env = protocol.environment()
        sandbox = args.directory / "sandbox"
        env.update(
            HOME=str(sandbox / "home"),
            XDG_DATA_HOME=str(sandbox / "xdg-data"),
            XDG_CONFIG_HOME=str(sandbox / "xdg-config"),
            XDG_CACHE_HOME=str(sandbox / "xdg-cache"),
            PYTHONPATH=os.pathsep.join((str(args.app_root), str(ROOT))),
        )
        result["preflight_seconds"] = time.perf_counter() - began
        launch = time.perf_counter()
        chart = PersistentPreparation(
            args.directory / "chart", args.app_root, args.snapshot
        )
        command = [
            sys.executable,
            str(ROOT / "tools/independent_numerical_service.py"),
            "--worker",
            str(args.directory / "config.json"),
            "--output",
            str(directory / "service-result.json"),
        ]
        thread = Thread(
            target=lambda: statuses.append(
                protocol.supervise(
                    command, ROOT, env, directory / "process", timeout=90
                )
            )
        )
        thread.start()
        # GUI itself must not instantiate the legacy core/window, open forecasts,
        # or read coast/chart data. Those resources remain in isolated producers.
        from osgeo import gdal

        old_open, old_open_ex = gdal.Open, gdal.OpenEx

        def blocked(*_args, **_kwargs):
            raise AssertionError("Early viewport accessed GDAL")

        gdal.Open = gdal.OpenEx = blocked
        try:
            result.update(
                display(args, reference, directory, chart, thread, pool, launch)
            )
        finally:
            gdal.Open, gdal.OpenEx = old_open, old_open_ex
    except Exception as exc:
        result["errors"].append(str(exc))
    finally:
        if thread:
            atomic_json(args.directory / "numerical/stop.json", dict(stop=True))
        if chart:
            chart.close()
            result["chart_status"] = chart.status
        if thread:
            thread.join(10)
            result["numerical_status"] = statuses[0] if statuses else None
            if thread.is_alive():
                result["errors"].append("Numerical worker failed to stop")
        pool.shutdown(wait=True, cancel_futures=True)
    try:
        for status in (result.get("chart_status"), result.get("numerical_status")):
            if status:
                protocol.require_clean(status)
        if result["errors"]:
            raise ValueError("; ".join(result["errors"]))
        service = json.loads(
            (args.directory / "numerical/service-result.json").read_text()
        )
        if (
            not service["no_qt_application"]
            or not service["no_legacy_window"]
            or len({e["thread"] for e in service["worker_events"]}) != 1
        ):
            raise ValueError("Numerical owner/headless contract failed")
        result["numerical_service"] = service
        # Deferred oracle work occurs AFTER the live window/measurement closes.
        result["reference_check"] = numerical.check_reference(
            Path(result["field_directory"]), args.reference
        )
        if args.safe_barbs:
            result["acceptance_scope"] = (
                "corrected missing-data portrayal within independent CPU raster tolerance, and publication mechanism"
            )
            result["visual_acceptance"] = bool(
                result.get("independent_portrayal_passed")
            )
            result["known_reference_visual_artifact"] = (
                "original reference preserved; corrected shader validated against independent CPU geometry instead"
            )
        result["passed"] = True
    except Exception as exc:
        result["passed"] = False
        if str(exc) not in result["errors"]:
            result["errors"].append(str(exc))
    atomic_json(args.output, result)
    if not result["passed"]:
        raise ValueError(
            f"Early publication rejected; see {args.output}: {result['errors']}"
        )


def display(args, reference, directory, chart, thread, pool, launch):
    from PySide6.QtCore import QTimer, Qt, QUrl
    from PySide6.QtGui import QColor, QImage, QSurfaceFormat
    from PySide6.QtQml import QQmlComponent
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QApplication, QWidget, QLabel
    from gweatherrouting.qt.gpuweather import WeatherImageProvider, encode_component
    from gweatherrouting.qt.gpuscene import ImageItem
    from gweatherrouting.rendering.weatherbarbs import BarbView
    from gweatherrouting.rendering import weatherbarbs as barbs

    app = QApplication([])
    initial = BarbView(**reference["view"])
    stamp = datetime.fromisoformat(reference["clocks"][0]).timestamp()
    counts = dict(texture_uploads=0, texture_upload_bytes=0, vertex_upload_bytes=0)
    report = dict(
        view=reference["view"], clock=reference["clocks"][0], pans=[], readbacks=[]
    )
    report["safe_barbs"] = args.safe_barbs
    errors, ticks = [], []

    class Viewport(QWidget):
        def __init__(self):
            super().__init__()
            self.view = initial
            self.offset = 0
            self.prepared = None
            self.chart_bundle = None
            self.drag = None
            self.resize(initial.width, initial.height)
            self.quick = QQuickWidget(self)
            fmt = QSurfaceFormat()
            fmt.setSamples(4)
            fmt.setAlphaBufferSize(8)
            self.quick.setFormat(fmt)
            self.quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
            self.quick.setGeometry(0, 0, initial.width, initial.height)
            self.quick.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.quick.setClearColor(QColor.fromRgbF(*reference["sea_color"]))
            self.label = QLabel("Wind loading • charts and other layers loading", self)
            self.label.setGeometry(8, 8, initial.width - 16, 36)
            self.label.setStyleSheet("background: #202830; color: white; padding: 6px")
            self.component = QQmlComponent(self.quick.engine())
            self.component.setData(b"import QtQuick\nItem { clip: true }", QUrl())
            self.root = self.component.create()
            self.quick.setContent(QUrl(), self.component, self.root)
            self.background = ImageItem(self.root, self.quick, counts)
            self.navigation = ImageItem(self.root, self.quick, counts)
            self.navigation.setZ(10000)
            self.fields, self.selections = (
                WeatherImageProvider(),
                WeatherImageProvider(),
            )
            self.quick.engine().addImageProvider("earlyfield", self.fields)
            self.quick.engine().addImageProvider("earlyselection", self.selections)
            self.weather_component = QQmlComponent(
                self.quick.engine(),
                QUrl.fromLocalFile(str(args.directory / "shaders/WeatherBarbs.qml")),
            )
            self.weather = self.weather_component.create()
            if self.weather is None:
                raise ValueError(str(self.weather_component.errors()))
            self.weather.setParent(self.root)
            self.weather.setParentItem(self.root)
            self.weather.setZ(100)
            self.weather.setWidth(initial.width)
            self.weather.setHeight(initial.height)
            self.weather.setOpacity(reference["opacity"])
            self.weather.setVisible(False)

        def publish(self, provider, images, names, namespace):
            pair = SimpleNamespace(
                images=images, nbytes=sum(i.sizeInBytes() for i in images)
            )
            for name, url in zip(names, provider.publish(pair)):
                self.weather.setProperty(
                    name, QUrl(url.replace("image://weather/", f"image://{namespace}/"))
                )

        def wind(self):
            if self.prepared is None:
                return
            staged, retained, prepared, _field = self.prepared
            if not retained.covers(self.view):
                self.weather.setVisible(False)
                self.background.setVisible(False)
                self.navigation.setVisible(False)
                self.label.setText(
                    "Outside prepared coverage • weather/charts unavailable"
                )
                return
            selection = renderer.previous.make_selection(
                prepared, self.view, stamp, barbs
            )
            self.symbols = int(np.count_nonzero(selection.values[..., 0] >= 0))
            images = tuple(encode_component(selection.values[..., i]) for i in range(3))
            self.publish(
                self.selections,
                images,
                ("nodeIdSource", "anchorXSource", "anchorYSource"),
                "earlyselection",
            )
            self.weather.setProperty("rows", selection.values.shape[0])
            self.weather.setProperty("columns", selection.values.shape[1])
            self.weather.setProperty("blend", staged.weight(stamp))
            self.weather.setVisible(self.symbols > 0)
            self.label.setText(
                ("Wind ready" if self.symbols else "No wind symbols at this view/time")
                + (
                    " • charts and other layers loading"
                    if self.chart_bundle is None
                    else " • charts ready"
                )
            )
            self.draw_charts()
            self.quick.update()

        def draw_charts(self):
            if self.chart_bundle is None:
                return
            meta, arrays, _ = self.chart_bundle
            targets, reason = renderer.coverage_for_view(
                initial, self.view, meta["targets"], self.prepared[1]
            )
            for item in (self.background, self.navigation):
                item.setVisible(reason is None)
            if reason:
                self.label.setText("Wind ready • outside prepared chart coverage")
                return
            for item, image, target in zip(
                (self.background, self.navigation), arrays["_gui_images"], targets
            ):
                item.present(
                    SimpleNamespace(image=image, target=target, smooth=False),
                    meta["dpr"],
                )

        def pan_by(self, dx):
            self.offset += dx
            self.view = camera_at_offset(initial, self.offset)
            self.wind()

        def mousePressEvent(self, event):
            self.drag = event.position()

        def mouseMoveEvent(self, event):
            if self.drag is not None:
                dx = self.drag.x() - event.position().x()
                self.drag = event.position()
                self.pan_by(dx)

        def mouseReleaseEvent(self, event):
            self.drag = None

    window = Viewport()
    window.setWindowTitle("A0j • independent weather startup experiment")
    window.show()
    request_sent = False
    field_future = chart_future = None
    early_image = None
    sequence = [8, 8, -8, -8]
    pan_at = None
    chart_bound_at = None
    blank = None

    def grab(name):
        began = time.perf_counter()
        image = window.quick.grabFramebuffer()
        ended = time.perf_counter()
        if image.isNull() or not image.save(str(args.directory / f"{name}.png")):
            raise ValueError("Readback/save failed")
        report["readbacks"].append(
            dict(name=name, began_seconds=began - launch, ended_seconds=ended - launch)
        )
        return image

    def chart_payload():
        path, response = chart.prepare(
            SimpleNamespace(generation=1), reference["view"], stamp, Event()
        )
        bundle = renderer.prepare_gui_bundle(
            renderer.read_bundle(
                path,
                expected=dict(
                    snapshot_sha256=reference["snapshot_sha256"],
                    view=reference["view"],
                    clocks=[reference["clocks"][0]],
                ),
            )
        )
        return bundle, response

    def tick():
        nonlocal request_sent, field_future, chart_future, early_image, pan_at, chart_bound_at, blank
        now = time.perf_counter()
        ticks.append(now)
        try:
            if now - launch > 80:
                raise TimeoutError("Early display exceeded 80 s deadline")
            if not thread.is_alive():
                raise RuntimeError("Numerical service stopped before display completed")
            if blank is None:
                blank = grab("blank-loading")
            if not request_sent and (directory / "ready.json").exists():
                atomic_json(
                    directory / "request.json",
                    dict(generation=1, view=reference["view"], stamp=stamp),
                )
                request_sent = True
            if field_future is None and (directory / "response-1.json").exists():
                response = json.loads((directory / "response-1.json").read_text())
                if response["error"] or response["generation"] != 1:
                    raise ValueError(f"Early numerical result rejected: {response}")
                report["field_directory"] = response["directory"]
                report["field_available_seconds"] = now - launch
                field_future = pool.submit(
                    read_field, Path(response["directory"]), reference
                )
            if window.prepared is None and field_future and field_future.done():
                meta, arrays, prepared, images = field_future.result()
                window.prepared = prepared
                window.publish(
                    window.fields,
                    images,
                    ("u0Source", "v0Source", "u1Source", "v1Source"),
                    "earlyfield",
                )
                window.wind()
                report["wind_bound_seconds"] = time.perf_counter() - launch
                report["native_nodes"] = meta["node_count"]
                report["symbols"] = window.symbols
                report["field_generation"] = meta["generation"]
            if (
                early_image is None
                and window.prepared
                and window.weather.property("ready")
            ):
                if (chart.directory / "ready.json").exists():
                    raise ValueError("Chart host ready before proven wind rendering")
                early_image = grab("early-wind-charts-loading")
                if (chart.directory / "ready.json").exists():
                    raise ValueError("Chart host became ready during early readback")
                changed = int(np.any(rgba(early_image) != rgba(blank), axis=1).sum())
                if changed == 0 or not window.symbols:
                    raise ValueError("Early wind has no proven visible glyph pixels")
                report["early_nonblank_rows"] = changed
                report["early_wind_readback_seconds"] = time.perf_counter() - launch
                report["charts_ready_at_early_readback"] = False
                pan_at = now + 0.1
            if early_image is not None and sequence and now >= pan_at:
                dx = sequence.pop(0)
                window.pan_by(dx)
                report["pans"].append(
                    dict(
                        dx=dx,
                        seconds=now - launch,
                        charts_ready=(chart.directory / "ready.json").exists(),
                        view=window.view.__dict__,
                        symbols=window.symbols,
                    )
                )
                pan_at = now + 0.1
                if not sequence:
                    if window.view != initial:
                        raise ValueError("Covered A→B→A did not return exactly")
                    report["covered_return_exact"] = True
            if chart_future is None and chart.observe_ready():
                report["chart_ready_seconds"] = time.perf_counter() - launch
                chart_future = pool.submit(chart_payload)
            if (
                chart_bound_at is None
                and chart_future
                and chart_future.done()
                and not sequence
            ):
                bundle, response = chart_future.result()
                meta, _arrays, _ = bundle
                if meta["dpr"] != window.quick.devicePixelRatioF():
                    raise ValueError("Concurrent chart DPR differs")
                if (
                    meta["opacity"] != reference["opacity"]
                    or meta["sea_color"] != reference["sea_color"]
                ):
                    raise ValueError("Concurrent chart style differs")
                # Weather-only readback AFTER charts are prepared: chart arrival
                # must not replace early field, anchors, or the current camera.
                late_wind = grab("late-wind-before-chart-bind")
                if not np.array_equal(rgba(early_image), rgba(late_wind)):
                    raise ValueError("Weather/camera pixels changed before chart bind")
                previous_view, previous_field = window.view, window.prepared
                window.chart_bundle = bundle
                window.draw_charts()
                if (
                    window.view != previous_view
                    or window.prepared is not previous_field
                ):
                    raise ValueError("Chart bind replaced weather or viewport")
                window.label.setText("Wind ready • charts ready")
                window.quick.update()
                chart_bound_at = time.perf_counter()
                report["chart_bound_seconds"] = chart_bound_at - launch
                report["chart_directory"] = str(bundle[2])
                report["chart_response"] = response
                report["weather_pixels_unchanged_before_chart_bind"] = True
                report["camera_and_field_preserved_on_chart_bind"] = True
            if chart_bound_at and now - chart_bound_at > 0.25:
                final = grab("final-weather-and-charts")
                if args.safe_barbs:
                    from diagnose_barb_overlay import (
                        cpu_image,
                        support_error,
                        rgba as pixel_rgba,
                    )

                    staged, _retained, prepared, field = window.prepared
                    selection = renderer.previous.make_selection(
                        prepared, window.view, stamp, barbs
                    )
                    cpu = cpu_image(
                        field.values,
                        selection.values[..., :3],
                        staged.weight(stamp),
                        initial.width,
                        initial.height,
                        window.quick.devicePixelRatioF(),
                        reference["opacity"],
                    )
                    cpu.save(str(args.directory / "independent-cpu-wind.png"))
                    window.weather.setVisible(False)
                    chart_only = grab("charts-only-for-independent-oracle")
                    window.weather.setVisible(True)
                    expected = pixel_rgba(cpu)
                    checks = []
                    for name, image, baseline in (
                        ("early", early_image, blank),
                        ("final", final, chart_only),
                    ):
                        pixels = pixel_rgba(image)
                        difference = (
                            np.max(
                                np.abs(
                                    pixels.astype(np.int16)
                                    - pixel_rgba(baseline).astype(np.int16)
                                ),
                                axis=-1,
                            )
                            > 8
                        )
                        mask = np.zeros_like(expected)
                        mask[..., 3] = difference * 255
                        check = support_error(mask, expected)
                        check["phase"] = name
                        checks.append(check)
                        if (
                            check["gpu_outside_cpu_support"]
                            or check["cpu_outside_gpu_support"]
                        ):
                            raise ValueError(
                                f"Corrected {name} wind differs from independent CPU support: {check}"
                            )
                    report["independent_cpu_support_checks"] = checks
                    report["independent_portrayal_passed"] = True
                    report["original_bad_gpu_pixel_equality_required"] = False
                else:
                    oracle = QImage(str(Path(report["chart_directory"]) / "oracle.png"))
                    if oracle.isNull() or not np.array_equal(rgba(final), rgba(oracle)):
                        raise ValueError(
                            "Late chart/weather composition differs from concurrent oracle"
                        )
                    report["final_scene_exact_pixels"] = True
                report["host"] = dict(
                    platform=app.platformName(),
                    dpr=window.quick.devicePixelRatioF(),
                    backend=window.quick.quickWindow()
                    .rendererInterface()
                    .graphicsApi()
                    .name,
                )
                report["heartbeat_ms"] = renderer.tick_gap_summary(ticks)
                timer.stop()
                app.quit()
        except Exception as exc:
            errors.append(str(exc))
            timer.stop()
            app.quit()

    timer = QTimer()
    timer.setInterval(16)
    timer.timeout.connect(tick)
    timer.start()
    app.exec()
    timer.stop()
    window.close()
    app.processEvents()
    report["errors"] = errors
    atomic_json(args.directory / "display-result.json", report)
    if errors or not (
        report.get("final_scene_exact_pixels")
        or report.get("independent_portrayal_passed")
    ):
        raise ValueError(f"Live publication failed: {errors}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("snapshot", "app-root", "reference", "directory", "output"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--safe-barbs", action="store_true")
    args = parser.parse_args()
    for name in ("snapshot", "app_root", "reference", "directory", "output"):
        setattr(args, name, getattr(args, name).resolve())
    sys.path.insert(0, str(args.app_root))
    run(args)


if __name__ == "__main__":
    main()
