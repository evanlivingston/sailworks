#!/usr/bin/env python3
"""Diagnostic timing of the application ENC chart path at the user's wide view.

Reports stage wall spans only. It never changes rendering inputs or outputs and
asserts no speed threshold.
"""

import argparse
import json
from pathlib import Path
import time

from check_enc_app_rollout import run
from check_enc_viewport import _wait

SAVED_VIEW = (48.45003186114848, -124.28875762043288, 9.380182039882325)


def application(report, data, output, deadline):  # noqa: C901 - one bounded session
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.mapcanvas import TrackMap
    from gweatherrouting.qt.weatherpanel import WeatherPanel

    SystemCredentialStore.load = lambda self: None
    app = QApplication.instance() or QApplication([])
    controller = SessionController(offline=True)
    controller.settings["weatherRenderer"] = "viewport"
    controller.settings["gribWindStyle"] = "barbs"
    canvas = TrackMap(controller)
    canvas.resize(710, 743)
    panel = WeatherPanel(controller, lambda: canvas, lambda: None, lambda: None)
    report["stages"] = {}

    def wait(predicate, seconds=20):
        return _wait(predicate, max(0, min(seconds, deadline - time.perf_counter())))

    def model():
        widget = canvas._weather_presenter.widget
        return None if widget is None else widget._chart_model

    def ready_chart():
        chart = model()
        return chart is not None and chart.state == "ready" and chart.enc_state == "ready"

    def spans():
        chart = model()
        return [] if chart is None else list(chart.observations()["spans"])

    def stage(name, action):
        before = len(spans())
        began = time.perf_counter()
        action()
        ready = wait(ready_chart)
        ended = time.perf_counter()
        chart = model()
        report["stages"][name] = {
            "ready": bool(ready),
            "wall_to_ready_s": round(ended - began, 3),
            "composition_spans_s": [round(e - s, 3) for s, e in spans()[before:]],
            "selected": list(chart.enc_metadata.get("selected", ())) if chart else [],
        }

    try:
        hrrr = [c for c in controller.display_forecast.choices if "hrrr" in c.name.lower()]
        if hrrr:
            panel.display_forecast.setCurrentIndex(
                panel.display_forecast.findData(hrrr[0].key)
            )
        wait(lambda: not controller.display_forecast.loading, 20)
        canvas.show()
        lat, lon, zoom = SAVED_VIEW
        canvas._center_lat, canvas._center_lon, canvas._zoom = lat, lon, zoom
        canvas._invalidate_base()
        canvas.update()
        stage("startup_wide_view", lambda: None)

        def pan():
            canvas._center_lon = lon + 0.02
            canvas._invalidate_base()
            canvas.update()

        def zoom_in():
            canvas._zoom = zoom + 0.5
            canvas._invalidate_base()
            canvas.update()

        stage("pan_small", pan)
        stage("zoom_in_fractional", zoom_in)
        report["complete"] = all(item["ready"] for item in report["stages"].values())
        output.parent.mkdir(parents=True, exist_ok=True)
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
    report = run(args.snapshot, args.output, application=application)
    report["view"] = "saved wide view (48.45, -124.29, zoom 9.38)"
    report["limits"] = (
        "Diagnostic wall spans only; no speed threshold, FPS or presentation latency"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
