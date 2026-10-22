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


def application(report, data, output, deadline):
    import numpy as np
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.window import PreviewWindow
    from gweatherrouting.qt.theme import apply_theme
    from gweatherrouting.core.mapviewport import MapPoint

    SystemCredentialStore.load = lambda _self: None
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    controller = SessionController(offline=True)
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
        canvas._center_lat, canvas._center_lon, canvas._zoom = (48.45, -124.29, 9.38)
        canvas._invalidate_base()
        canvas.update()

    def current():
        presenter = canvas._weather_presenter
        widget = presenter.widget
        cache = canvas._base_cache
        renderer = canvas._base_chart_renderer
        state = renderer._states.get(canvas)
        return bool(
            widget is not None
            and presenter._rendered
            and widget._camera is not None
            and (
                widget._camera.center_lat,
                widget._camera.center_lon,
                widget._camera.zoom,
                widget._camera.width,
                widget._camera.height,
            )
            == (
                canvas._center_lat,
                canvas._center_lon,
                canvas._zoom,
                canvas.width(),
                canvas.height(),
            )
            and cache is not None
            and cache[0] == canvas._base_key()
            and widget._base is not None
            and widget._base.property("ready")
            and widget._base_key == cache[4].cacheKey()
            and state is not None
            and state.frame is not None
            and state.requested is None
            and not renderer._running
        )

    def stage(name):
        ready = wait(current)
        image = canvas._base_image().convertToFormat(QImage.Format.Format_RGBA8888)
        pixels = (
            np.frombuffer(image.constBits(), np.uint8)
            .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
            .reshape(-1, 4)
        )
        sea = canvas._sea_color().getRgb()
        nonsea = int(np.any(pixels[:, :3] != sea[:3], axis=1).sum())
        presenter = canvas._weather_presenter
        renderer = canvas._base_chart_renderer
        state = renderer._states.get(canvas)
        report["stages"][name] = {
            "ready": ready,
            "non_sea_pixels": nonsea,
            "camera": [canvas._center_lat, canvas._center_lon, canvas._zoom],
            "fallback": presenter.reason,
            "base_diagnostics": renderer.diagnostics,
            "worker_camera": (
                [
                    state.frame[0].viewport._center_lat,
                    state.frame[0].viewport._center_lon,
                    state.frame[0].viewport._zoom,
                ]
                if state is not None and state.frame is not None
                else None
            ),
        }
        window.grab().save(str(output.with_name(name + ".png")))
        image.save(str(output.with_name(name + "-base.png")))
        return ready and nonsea > 0 and not presenter.reason

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
        canvas._pan_pixels(40, 20)
        canvas._refresh_after_gesture()
        report["checks"]["pan_basemap"] = stage("pan")
        aligned = route_aligned() and aligned
        canvas._zoom += 0.25
        canvas._refresh_after_gesture()
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
        charts = controller.charts
        original, coast = charts.charts[:], charts.gshhsLayer
        charts.charts, charts.gshhsLayer = [], None
        with patch(
            "gweatherrouting.rendering.chartcollection.DATA_DIR", str(data / "missing")
        ):
            charts.load_base_chart()
        canvas.invalidate_chart_sources()
        report["checks"]["missing_coastline_fallback"] = stage(
            "missing-coastlines"
        ) and (
            "not installed" in canvas._weather_presenter.status.text()
            and "low-detail" in canvas._weather_presenter.status.text()
        )
        charts.charts = []
        with (
            patch(
                "gweatherrouting.rendering.chartcollection.DATA_DIR",
                str(data / "missing"),
            ),
            patch(
                "gweatherrouting.rendering.chartcollection.GDALVectorChart",
                side_effect=OSError("Unreadable basemap"),
            ),
        ):
            charts.load_base_chart()
        canvas._weather_presenter._label()
        report["checks"]["loading_failure_visible"] = (
            "Unreadable basemap" in canvas._weather_presenter.status.text()
            and "world basemap unavailable" in canvas._weather_presenter.status.text()
        )
        charts.charts, charts.gshhsLayer, charts.basemap_error = original, coast, ""
        canvas.invalidate_chart_sources()
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
