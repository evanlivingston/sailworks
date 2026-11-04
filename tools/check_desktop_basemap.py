#!/usr/bin/env python3
"""Offline full-window basemap acceptance; readbacks are not timing evidence."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import time
from unittest.mock import patch

from check_enc_app_rollout import run
from check_enc_viewport import _wait


def current_chart(canvas):
    """Require the displayed viewport publication, not the retired base cache."""
    from sailworks.qt.gribviewportchart import chart_placement

    presenter = canvas._weather_presenter
    widget = presenter.widget
    if widget is None or not presenter._rendered or widget._camera is None:
        return False
    camera = widget._camera
    model = widget._chart_model
    if model is None or model.state != "ready" or model._published is None:
        return False
    generation, _image, metadata = model._published
    chart = widget._chart
    return bool(
        (camera.center_lat, camera.center_lon, camera.zoom, camera.width, camera.height)
        == (
            canvas._center_lat,
            canvas._center_lon,
            canvas._zoom,
            canvas.width(),
            canvas.height(),
        )
        and camera.time == canvas.time_control.time
        and model._reusable(camera)
        and widget._chart_viewport is metadata["viewport"]
        and chart is not None
        and chart.isVisible()
        and chart.property("ready")
        and chart.property("source").toString() == f"image://chart/{generation}"
        and all(
            abs(chart.property(name) - value) < 0.01
            for name, value in zip(
                ("imageX", "imageY", "imageWidth", "imageHeight"),
                chart_placement(metadata["viewport"], camera),
            )
        )
    )


def application(report, data, output, deadline):
    import numpy as np
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from sailworks.core.marine.credentials import SystemCredentialStore
    from sailworks.qt.controller import SessionController
    from sailworks.qt.window import PreviewWindow
    from sailworks.qt.theme import apply_theme
    from sailworks.core.mapviewport import MapPoint
    from sailworks.rendering.style import Style

    SystemCredentialStore.load = lambda _self: None
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    controller = SessionController()
    settings = controller.settings
    for key in (
        "chartENCVisible",
        "chartRasterVisible",
        "chartMBTilesVisible",
        "chartCM93Visible",
        "chartSeamarksVisible",
        "chartVectorVisible",
        "canadianCharts",
    ):
        settings[key] = False
    settings["chartWorldVisible"] = True
    settings["weatherRenderer"] = "viewport"
    settings["gribWindStyle"] = "barbs"
    controller.display_forecast.choose("")
    window = PreviewWindow(controller, workspace_path=data / "review-workspace.json")
    canvas = window.map
    output.parent.mkdir(parents=True, exist_ok=True)

    def wait(predicate, seconds=12):
        return _wait(predicate, max(0, min(seconds, deadline - time.perf_counter())))

    def center():
        canvas.camera_state.center_lat = 48.45
        canvas.camera_state.center_lon = -124.29
        canvas.camera_state.zoom = 9.38
        canvas._invalidate_base()
        canvas.update()

    def current():
        return current_chart(canvas)

    def stage(name):
        ready = wait(current)
        presenter = canvas._weather_presenter
        widget = presenter.widget
        image = widget.grabFramebuffer().convertToFormat(QImage.Format.Format_RGBA8888)
        pixels = (
            np.frombuffer(image.constBits(), np.uint8)
            .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
            .reshape(-1, 4)
        )
        palette = Style.chart_palettes[settings.chartPalette]

        def color_pixels(color):
            rgb = np.array([round(c * 255) for c in color[:3]])
            return int((np.abs(pixels[:, :3].astype(int) - rgb).max(axis=1) <= 2).sum())

        land = color_pixels(palette.land_fill.color)
        sea = color_pixels(palette.sea.color)
        model = widget._chart_model
        published = model._published
        viewport = None if published is None else published[2]["viewport"]
        report["stages"][name] = {
            "ready": ready,
            "land_pixels": land,
            "sea_pixels": sea,
            "camera": [canvas._center_lat, canvas._center_lon, canvas._zoom],
            "fallback": presenter.reason,
            "chart_observations": model.observations(),
            "chart_source": widget._chart.property("source").toString(),
            "worker_camera": (
                [
                    viewport._center_lat,
                    viewport._center_lon,
                    viewport._zoom,
                ]
                if viewport is not None
                else None
            ),
        }
        window.grab().save(str(output.with_name(name + ".png")))
        image.save(str(output.with_name(name + "-base.png")))
        return ready and land > 0 and sea > 0 and not presenter.reason

    def route_aligned():
        image = canvas.grab().toImage()
        dpr = image.devicePixelRatio()
        for coordinates in canvas.route_plan:
            x, y = canvas.convert_geographic_to_screen(MapPoint(*coordinates))
            samples = [
                image.pixelColor(round((x + dx) * dpr), round((y + dy) * dpr))
                for dx in range(-2, 3)
                for dy in range(-2, 3)
            ]
            if not any(
                abs(c.red() - 82) < 12
                and abs(c.green() - 149) < 12
                and abs(c.blue() - 210) < 12
                for c in samples
            ):
                return False
        return len(canvas.route_plan) == 2

    try:
        window.show()
        wait(lambda: not controller.display_forecast.loading)
        center()
        window.weather_dock.show()
        window.routing_panel.set_location("start", (48.45, -124.34))
        window.routing_panel.set_location("destination", (48.45, -124.24))
        window.navigation_panel.add_poi((48.48, -124.29))
        report["checks"]["full_window_without_forecast"] = stage("no-weather")
        aligned = route_aligned()
        canvas._weather_presenter.widget.pan_pixels(40, 20)
        report["checks"]["pan_basemap"] = stage("pan")
        aligned = route_aligned() and aligned
        canvas._weather_presenter.widget.zoom_at(
            0.25, canvas.width() / 2, canvas.height() / 2
        )
        report["checks"]["fractional_zoom_basemap"] = stage("zoom")
        aligned = route_aligned() and aligned
        window.resize(1320, 860)
        report["checks"]["resize_basemap"] = stage("resize")
        report["checks"]["route_overlay_alignment"] = route_aligned() and aligned
        choices = controller.display_forecast.choices
        preferred = [c for c in choices if "hrrr" in c.name.lower()]
        choice = (preferred or list(choices))[0]
        panel = window.weather_panel
        panel.display_forecast.setCurrentIndex(
            panel.display_forecast.findData(choice.key)
        )
        controller.time_control.set_time(datetime.fromtimestamp(choice.first_valid))
        center()
        weather = wait(
            lambda: current() and canvas._weather_presenter.widget._state == "ready"
        )
        report["checks"]["weather_panel_forecast"] = stage("weather") and weather
        panel.wind.setChecked(False)
        report["checks"]["hidden_wind_basemap"] = stage("wind-hidden")
        panel.display_forecast.setCurrentIndex(panel.display_forecast.findData(""))
        report["checks"]["source_clear_basemap"] = stage("source-clear")
        # Recreate the actual viewport against missing sandbox data, rather
        # than mutating the legacy chart collection which it no longer draws.
        presenter = canvas._weather_presenter
        presenter._retire_native_widget()
        with patch("sailworks.core.storage.DATA_DIR", str(data / "missing")):
            canvas.update()
            report["checks"]["missing_coastline_fallback"] = stage(
                "missing-coastlines"
            ) and (
                "low-detail" in presenter.status.text()
                and not presenter.widget._chart_model.observations()["basemap"][
                    "has_gshhs"
                ]
            )
        presenter._retire_native_widget()
        with patch(
            "sailworks.qt.gribviewportchart.base_sources",
            side_effect=OSError("Unreadable basemap"),
        ):
            canvas.update()
            report["checks"]["loading_failure_visible"] = wait(
                lambda: not presenter.active
                and presenter.widget is None
                and "Unreadable basemap" in presenter.status.text()
                and presenter.status.isVisible()
            )
        report["complete"] = all(report["checks"].values())
    finally:
        window.close()
        controller.close()
        app.processEvents()


def interaction_application(report, data, output, deadline):
    """Time input dispatch to GUI adoption, not rendering or presentation."""
    from dataclasses import asdict

    from PySide6.QtWidgets import QApplication
    from sailworks.core.marine.credentials import SystemCredentialStore
    from sailworks.core.storage import DATA_DIR
    from sailworks.qt.controller import SessionController
    from sailworks.qt import gribviewport, weatherpresenter
    from sailworks.qt.gribviewportchart import CHART_IDLE_MS
    from sailworks.qt.theme import apply_theme
    from sailworks.qt.window import PreviewWindow

    if Path(DATA_DIR) != data:
        raise RuntimeError("Application storage escaped the sandbox")
    SystemCredentialStore.load = lambda _self: None
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    controller = SessionController()
    settings = controller.settings
    for key in (
        "chartENCVisible",
        "chartRasterVisible",
        "chartMBTilesVisible",
        "chartCM93Visible",
        "chartSeamarksVisible",
        "chartVectorVisible",
        "canadianCharts",
    ):
        settings[key] = False
    settings["chartWorldVisible"] = True
    settings["weatherRenderer"] = "viewport"
    settings["gribWindStyle"] = "barbs"
    controller.display_forecast.choose("")
    active = None
    original_factory = gribviewport._viewport_widget_class
    original_presenter_factory = weatherpresenter._viewport_widget_class
    instrumented_classes = {}

    def slot_result(original_slot_result, widget, slot, token, success):
        pending = widget._chart_pending
        original_slot_result(widget, slot, token, success)
        ended = time.perf_counter_ns()
        if active is None or not success or pending is None:
            return
        generation, metadata, pending_slot, pending_token = pending
        model = widget._chart_model
        chart = widget._chart
        if (
            (slot, token) == (pending_slot, pending_token)
            and model is not None
            and chart is not None
            and slot == widget._chart_front_slot == chart.property("frontSlot")
            and chart.property("ready")
            and chart.property(f"slot{slot}Source").toString()
            == f"image://chart/{generation}"
            and widget._chart_visible_generation == generation
            and widget._chart_viewport is metadata.get("viewport")
            and widget._chart_composition_camera
            == metadata.get("camera")
            == widget._camera
            and model._published is not None
            and model._published[0] == generation
            and model._published[2] is metadata
            and model._base_sources() == active["sources"]
            and not model._enc_enabled
            and not model._enc_only
            and (metadata.get("enc") or {}).get("state") == "disabled"
        ):
            active["evidence"].append((generation, metadata.get("camera"), ended))

    def instrumented_factory():
        widget_class = original_factory()
        if widget_class not in instrumented_classes:
            original = widget_class._chart_slot_result
            original_gate = widget_class._request_chart
            instrumented_classes[widget_class] = (original, original_gate)

            def wrapped(widget, slot, token, success):
                return slot_result(original, widget, slot, token, success)

            def traced_gate(widget):
                sample = active
                if sample is None:
                    return original_gate(widget)
                fired = time.perf_counter_ns()
                chart = widget._chart_model
                weather = widget._model
                missing_model_or_camera = chart is None or widget._camera is None
                weather_not_ready = widget._weather_ready_at is None
                weather_blocked = (
                    weather_not_ready
                    and not getattr(widget, "_chart_without_weather", False)
                    and not (widget._enc_chart_layer and weather.state == "unavailable")
                )
                busy_snapshot = weather.busy()
                busy_checked = []
                original_busy = weather.busy
                had_instance_busy = "busy" in weather.__dict__

                def observed_busy():
                    result = original_busy()
                    busy_checked.append(result)
                    return result

                if chart is None:
                    chart_active = chart_pending = requests_before = None
                else:
                    with chart._condition:
                        chart_active = chart._active is not None
                        chart_pending = chart._pending is not None
                        requests_before = chart.counters["requests"]
                elapsed = fired / 1e6 - widget._chart_camera_changed * 1000.0
                event = {
                    "timestamp_ns": fired,
                    "elapsed_since_sample_ms": (
                        fired - sample["timestamps_ns"]["input"]
                    )
                    / 1e6,
                    "elapsed_since_camera_change_ms": elapsed,
                    "weather_ready_at_is_none": weather_not_ready,
                    "weather_busy": busy_snapshot,
                    "chart_active": chart_active,
                    "chart_pending": chart_pending,
                }
                sample["gate_events"].append(event)
                weather.busy = observed_busy
                try:
                    result = original_gate(widget)
                finally:
                    if had_instance_busy:
                        weather.busy = original_busy
                    else:
                        del weather.busy
                if chart is None:
                    requests_after = None
                else:
                    with chart._condition:
                        requests_after = chart.counters["requests"]
                requested = (
                    requests_before is not None and requests_after > requests_before
                )
                event["new_request"] = requested
                event["weather_busy_checked"] = (
                    busy_checked[0] if busy_checked else None
                )
                conditions = []
                if missing_model_or_camera:
                    conditions.append("no_model_or_camera")
                if weather_blocked:
                    conditions.append("weather_not_ready")
                if elapsed < CHART_IDLE_MS:
                    conditions.append("camera_moving_at_fire")
                if busy_snapshot:
                    conditions.append("weather_busy_at_fire")
                if chart_active or chart_pending:
                    conditions.append("chart_in_flight")
                event["conditions_at_fire"] = conditions
                if requested:
                    event["outcome"] = "requested"
                elif missing_model_or_camera:
                    event["outcome"] = "returned_no_model_or_camera"
                elif weather_blocked:
                    event["outcome"] = "returned_weather_not_ready"
                elif not busy_checked:
                    event["outcome"] = "returned_camera_moving"
                elif busy_checked[0]:
                    event["outcome"] = "returned_weather_busy"
                else:
                    event["outcome"] = "request_without_new_composition"
                return result

            widget_class._chart_slot_result = wrapped
            widget_class._request_chart = traced_gate
        return widget_class

    window = None
    model = None
    original_request = None
    original_compose = None
    original_notify_publish = None
    try:
        gribviewport._viewport_widget_class = instrumented_factory
        weatherpresenter._viewport_widget_class = instrumented_factory
        window = PreviewWindow(
            controller, workspace_path=data / "interaction-workspace.json"
        )
        canvas = window.map
        report["measurement"] = (
            "camera input dispatch to GUI chart adoption; not presentation or FPS"
        )
        report["samples"] = []
        report["workload"] = {
            "pan_pixels": [800, 400],
            "zoom_steps": 0.5,
            "zoom_anchor_fraction": [0.3, 0.4],
            "settle_ms": 100,
            "timeout_after_settle_s": 3,
        }
        window.show()
        canvas.camera_state.center_lat = 48.45
        canvas.camera_state.center_lon = -124.29
        canvas.camera_state.zoom = 9.38
        canvas._invalidate_base()
        canvas.update()

        def initial_ready():
            widget = canvas._weather_presenter.widget
            return bool(
                widget is not None
                and widget._camera is not None
                and widget._chart_model is not None
                and widget._chart_model._published is not None
                and widget._chart_visible_generation
                == widget._chart_model._published[0]
                and widget._chart_composition_camera
                == widget._chart_model._published[2].get("camera")
                == widget._camera
                and widget._chart_viewport
                is widget._chart_model._published[2].get("viewport")
                and widget._chart is not None
                and widget._chart.property("ready")
                and widget._chart.property(
                    f"slot{widget._chart_front_slot}Source"
                ).toString()
                == f"image://chart/{widget._chart_visible_generation}"
            )

        if not _wait(initial_ready, min(20, max(0, deadline - time.perf_counter()))):
            raise RuntimeError("Initial chart was not adopted")
        widget = canvas._weather_presenter.widget
        model = widget._chart_model
        sources = model._base_sources()
        basemap = model.observations()["basemap"]
        report["source"] = {
            "sources": [
                {"kind": source.kind, "path": source.path} for source in sources
            ],
            "gshhs_directory_exists": (data / "gshhs").is_dir(),
            "expected_gshhs_path": str(data / "gshhs"),
            "basemap": basemap,
            "enc_enabled": model._enc_enabled,
            "enc_only": model._enc_only,
            "initial_camera": asdict(widget._camera),
        }
        if not (
            (data / "gshhs").is_dir()
            and len(sources) == 1
            and sources[0].kind == "gshhs"
            and sources[0].path == str(data / "gshhs")
            and basemap["has_gshhs"]
            and basemap["available"]
            and not basemap["error"]
            and not model._enc_enabled
            and not model._enc_only
            and (model._published[2].get("enc") or {}).get("state") == "disabled"
        ):
            raise RuntimeError("GSHHS-only source cannot be verified")
        original_request = model.request
        original_compose = model._compose
        original_notify_publish = model._notify_publish

        def compose(camera, cancel, preview=False):
            began = time.perf_counter_ns()
            sample = active
            generation = model._active[0] if model._active is not None else None
            result = original_compose(camera, cancel, preview=preview)
            ended = time.perf_counter_ns()
            if sample is not None and active is sample and generation is not None:
                sample["timeline"].setdefault(generation, {})[
                    "composed_preview"
                ] = preview
            if (
                sample is not None
                and active is sample
                and generation is not None
                and generation > sample["previous_generation"]
                and generation == model._generation
                and not cancel.is_set()
            ):
                sample["timeline"].setdefault(generation, {}).update(
                    compose_start=began, compose_end=ended
                )
            return result

        def notify_publish(payload):
            published = time.perf_counter_ns()
            sample = active
            if (
                sample is not None
                and payload[0] > sample["previous_generation"]
                and payload[0] == model._generation
            ):
                sample["timeline"].setdefault(payload[0], {})["publish"] = published
            return original_notify_publish(payload)

        def request(camera, replace=False, preview=False):
            requested = time.perf_counter_ns()
            sample = active
            record = None
            if sample is not None:
                record = {
                    "camera": camera,
                    "generation": None,
                    "preview": preview,
                    "requested_ns": requested,
                    "requests_before": model.counters["requests"],
                    "reuses_before": model.counters["reuses"],
                }
                sample["requests"].append(record)
            generation = original_request(camera, replace=replace, preview=preview)
            if record is not None:
                record["generation"] = generation
                record["requests_delta"] = (
                    model.counters["requests"] - record["requests_before"]
                )
                record["reuses_delta"] = (
                    model.counters["reuses"] - record["reuses_before"]
                )
            return generation

        model.request = request
        model._compose = compose
        model._notify_publish = notify_publish
        for kind in ("pan", "zoom"):
            for index in range(3):
                previous = model._generation
                active = {
                    "kind": kind,
                    "index": index + 1,
                    "previous_generation": previous,
                    "sources": sources,
                    "requests": [],
                    "gate_events": [],
                    "evidence": [],
                    "timeline": {},
                    "phases_ms": {},
                }
                previous_camera = widget._camera
                started = time.perf_counter_ns()
                active["timestamps_ns"] = {"input": started}
                if kind == "pan":
                    widget.pan_pixels(800, 400)
                else:
                    widget.zoom_at(
                        0.5, widget._camera.width * 0.3, widget._camera.height * 0.4
                    )
                requested_camera = widget._camera
                active["requested_camera"] = asdict(requested_camera)

                def outcome():
                    for request_record in active["requests"]:
                        if request_record.get("preview"):
                            # A coarse interactive preview must never be
                            # mistaken for the settled full-quality result.
                            continue
                        generation = request_record["generation"]
                        if (
                            request_record["camera"] != requested_camera
                            or generation is None
                        ):
                            continue
                        for adopted, camera, ended in active["evidence"]:
                            if (
                                generation > previous
                                and adopted == generation
                                and camera == requested_camera
                            ):
                                return ("published_match", request_record, ended)
                        if (
                            generation <= previous
                            and request_record.get("reuses_delta", 0) > 0
                        ):
                            return ("reused", request_record, None)
                    return None

                if requested_camera == previous_camera:
                    active["status"] = "unchanged_camera"
                else:
                    _wait(
                        lambda: outcome() is not None,
                        min(3.5, max(0, deadline - time.perf_counter())),
                    )
                    resolved = outcome()
                    if resolved:
                        status, request_record, ended = resolved
                        active["status"] = status
                        active["requested_generation"] = request_record["generation"]
                        if ended is not None:
                            active["adopted_generation"] = request_record["generation"]
                            active["metadata_camera"] = asdict(requested_camera)
                            active["elapsed_ms"] = (ended - started) / 1e6
                            timeline = active["timeline"].get(
                                request_record["generation"], {}
                            )
                            timestamps = {
                                "input": started,
                                "request": request_record["requested_ns"],
                                "adopt": ended,
                                **timeline,
                            }
                            active["timestamps_ns"] = timestamps
                            active["phases_ms"] = {
                                name: (timestamps[end] - timestamps[start]) / 1e6
                                for name, start, end in (
                                    ("settle_and_gating", "input", "request"),
                                    ("scheduling", "request", "compose_start"),
                                    ("compose", "compose_start", "compose_end"),
                                    ("handoff", "compose_end", "publish"),
                                    ("gui_adoption", "publish", "adopt"),
                                )
                                if start in timestamps and end in timestamps
                            }
                    else:
                        active["status"] = (
                            "timeout"
                            if any(
                                r["camera"] == requested_camera
                                for r in active["requests"]
                            )
                            else "no_request"
                        )
                result = active.copy()
                active = None
                for request_record in result["requests"]:
                    composition = result["timeline"].get(
                        request_record["generation"], {}
                    )
                    if "composed_preview" in composition:
                        request_record["composed_preview"] = composition[
                            "composed_preview"
                        ]
                del result["sources"]
                del result["evidence"]
                del result["timeline"]
                for request_record in result["requests"]:
                    request_record["camera"] = asdict(request_record["camera"])
                report["samples"].append(result)
                if result["status"] != "published_match":
                    break
            if report["samples"][-1]["status"] != "published_match":
                break
        report["complete"] = len(report["samples"]) == 6 and all(
            sample["status"] == "published_match" for sample in report["samples"]
        )
    finally:
        try:
            if window is not None:
                window.close()
        finally:
            if original_request is not None:
                model.request = original_request
            if original_compose is not None:
                model._compose = original_compose
            if original_notify_publish is not None:
                model._notify_publish = original_notify_publish
            gribviewport._viewport_widget_class = original_factory
            weatherpresenter._viewport_widget_class = original_presenter_factory
            for widget_class, (original, original_gate) in instrumented_classes.items():
                widget_class._chart_slot_result = original
                widget_class._request_chart = original_gate
            controller.close()
            app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--chart-publication",
        action="store_true",
        help="measure camera dispatch to verified chart adoption (diagnostic only)",
    )
    args = parser.parse_args()
    if args.chart_publication:
        import tempfile

        approved = Path(
            "/private/var/folders/w7/hb5wv1x1715821rqd1_3xf4r0000gn/T/opencode"
        ).resolve()
        output = args.output.resolve()
        if not output.is_relative_to(approved) or output == approved:
            parser.error(f"--chart-publication output must be inside {approved}")
        tempfile.tempdir = str(approved)
    report = run(
        args.snapshot,
        args.output,
        application=interaction_application if args.chart_publication else application,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
