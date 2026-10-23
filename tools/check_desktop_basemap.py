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
    from gweatherrouting.qt.gribviewportchart import chart_placement

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
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.window import PreviewWindow
    from gweatherrouting.qt.theme import apply_theme
    from gweatherrouting.core.mapviewport import MapPoint
    from gweatherrouting.rendering.style import Style

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
        with patch("gweatherrouting.core.storage.DATA_DIR", str(data / "missing")):
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
            "gweatherrouting.qt.gribviewportchart.base_sources",
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.snapshot, args.output, application=application)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
