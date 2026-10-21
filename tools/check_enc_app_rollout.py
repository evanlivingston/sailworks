#!/usr/bin/env python3
"""Exercise default ENC through the actual application TrackMap/Weather panel."""

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_enc_viewport import _load_tool, _wait  # noqa: E402


def current_application_view(canvas, widget):
    """A previous ready frame must not satisfy a new application view request."""
    camera = widget._camera
    if camera is None:
        return False
    expected = (
        canvas._center_lat,
        canvas._center_lon,
        canvas._zoom,
        canvas.width(),
        canvas.height(),
        canvas.time_control.time,
    )
    actual = (
        camera.center_lat,
        camera.center_lon,
        camera.zoom,
        camera.width,
        camera.height,
        camera.time,
    )
    if expected != actual:
        return False
    layer = widget._chart_model
    with layer._condition:
        if layer._published is None or not layer._reusable(camera):
            return False
    cache = canvas._environment_cache
    return cache is not None and widget._environment_key == cache[4].cacheKey()


def run(snapshot, output):
    began = time.perf_counter()
    report = {"checks": {}, "stages": {}, "complete": False}
    fixture = _load_tool(
        "app_enc_fixture", ROOT / "tools/benchmark_startup_rendering.py"
    )
    manifest = fixture.verify_snapshot(snapshot)
    report["snapshot_sha256"] = manifest["content_sha256"]
    root = Path(tempfile.mkdtemp(prefix="enc-app-rollout-"))
    try:
        data = fixture.prepare_sandbox(snapshot, root)
        os.environ["HOME"] = str(root / "home")
        os.environ["XDG_DATA_HOME"] = str(root / "xdg-data")
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")

        def no_network(*args, **kwargs):
            raise RuntimeError("Network disabled during ENC rollout acceptance")

        socket.socket.connect = no_network
        socket.socket.connect_ex = no_network
        _application(report, data, output, began + 95)
    except Exception as exc:
        import traceback

        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    report["wall_s"] = round(time.perf_counter() - began, 3)
    report["timing_note"] = "Process wall time, not FPS or presentation latency"
    return report


def _application(report, data, output, deadline):  # noqa: C901 - one native app session
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.core.storage import DATA_DIR
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.mapcanvas import TrackMap
    from gweatherrouting.qt.weatherpanel import WeatherPanel

    if Path(DATA_DIR) != data:
        raise RuntimeError("Application storage escaped the sandbox")
    SystemCredentialStore.load = lambda _self: None
    app = QApplication.instance() or QApplication([])
    controller = SessionController(offline=True)
    canvas = TrackMap(controller)
    canvas.resize(710, 743)
    panel = WeatherPanel(controller, lambda: canvas, lambda: None, lambda: None)

    def center():
        canvas._center_lat, canvas._center_lon, canvas._zoom = (
            48.1125,
            -122.7375,
            14.236,
        )
        canvas._invalidate_base()
        canvas.update()

    def wait(predicate, timeout=12):
        return _wait(predicate, max(0, min(timeout, deadline - time.perf_counter())))

    def ready():
        presenter = canvas._weather_presenter
        widget = presenter.widget
        return bool(
            presenter.enc_active
            and widget is not None
            and widget.isVisible()
            and widget._chart_model.enc_state == "ready"
            and widget._chart.property("ready")
            and widget._environment.property("ready")
            and widget._chart_model.enc_metadata.get("selected")
            and current_application_view(canvas, widget)
        )

    def stage(name, weather=False):
        ok = wait(
            lambda: ready()
            and (not weather or canvas._weather_presenter.widget._state == "ready")
        )
        presenter = canvas._weather_presenter
        widget = presenter.widget
        report["stages"][name] = {
            "ready": ok,
            "fallback_reason": presenter.reason,
            "enc": None if widget is None else widget._chart_model.enc_metadata,
            "weather": None if widget is None else widget._state,
            "chart_loading": None if widget is None else widget._chart_model.state,
            "model_busy": None if widget is None else widget._model.busy(),
            "chart_requests": (
                None if widget is None else widget.counters["chart_requests"]
            ),
            "chart_image_error": (
                None if widget is None else widget._chart.property("sourceError")
            ),
            "environment_error": (
                None if widget is None else widget._environment.property("sourceError")
            ),
        }
        return ok

    try:
        controller.settings["weatherRenderer"] = "viewport"
        controller.settings["gribWindStyle"] = "barbs"
        # An empty selection must not hide the chart or pick another forecast.
        controller.display_forecast.choose("")
        center()
        canvas.show()
        report["checks"]["charts_without_forecast"] = stage("no_forecast")
        report["checks"][
            "no_implicit_forecast"
        ] = not controller.display_forecast.sources()
        wait(lambda: not controller.display_forecast.loading, 15)
        choices = controller.display_forecast.choices
        preferred = [c for c in choices if "hrrr" in c.name.lower()]
        choice = (preferred or list(choices))[0]
        panel.display_forecast.setCurrentIndex(
            panel.display_forecast.findData(choice.key)
        )
        controller.time_control.set_time(datetime.fromtimestamp(choice.first_valid))
        center()
        report["checks"]["selected_forecast_with_enc"] = stage("forecast", weather=True)
        widget = canvas._weather_presenter.widget
        report["checks"]["enc_only_no_duplicate_legacy"] = bool(
            widget is not None
            and widget._chart_model._enc_only
            and canvas not in controller.charts._enc_renderer._states
        )
        report["checks"]["app_layer_order_preserved"] = bool(
            widget is not None
            and widget._environment is not None
            and widget._environment.property("ready")
            and canvas._weather_presenter.foreground.isVisible()
        )

        canvas._center_lon += 0.002
        canvas._zoom += 0.25
        canvas._invalidate_base()
        canvas.update()
        report["checks"]["app_pan_zoom"] = stage("pan_zoom", weather=True)
        canvas.resize(850, 620)
        canvas.update()
        report["checks"]["app_resize"] = stage("resize", weather=True)
        controller.time_control.set_time(
            controller.time_control.time + timedelta(hours=1)
        )
        report["checks"]["app_time_change"] = stage("time", weather=True)

        panel.wind.setChecked(False)
        report["checks"]["charts_with_wind_hidden"] = stage("hidden_wind")
        panel.wind.setChecked(True)
        controller.settings["chartENCVisible"] = False
        canvas.invalidate_chart_sources()
        report["checks"]["chart_switch_off"] = wait(
            lambda: (
                canvas._weather_presenter.widget is not None
                and canvas._weather_presenter.widget._chart_model.enc_state
                == "disabled"
            )
        )
        controller.settings["chartENCVisible"] = True
        canvas.invalidate_chart_sources()
        report["checks"]["chart_switch_on"] = stage("enc_on")

        previous_model = canvas._weather_presenter.widget._model
        panel.display_forecast.setCurrentIndex(panel.display_forecast.findData(""))
        center()
        report["checks"]["source_clear_keeps_charts"] = bool(
            stage("source_clear")
            and canvas._weather_presenter.widget._model is not previous_model
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        canvas.grab().save(str(output.with_suffix(".png")))
        panel.weather_renderer.setCurrentIndex(
            panel.weather_renderer.findData("legacy")
        )
        report["checks"]["legacy_recovery"] = wait(
            lambda: (
                not canvas._weather_presenter.active
                and not canvas._weather_presenter.enc_active
                and canvas._base_cache is not None
            )
        )
        report["complete"] = all(report["checks"].values())
    finally:
        panel.close()
        canvas.close()
        controller.close()
        app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.snapshot, args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
