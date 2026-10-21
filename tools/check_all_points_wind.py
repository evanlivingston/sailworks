#!/usr/bin/env python3
"""Native app gate for unthinned All forecast points (#79)."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import time

from check_enc_app_rollout import run
from check_enc_viewport import _wait, _pump


def application(report, data, output, deadline):  # noqa: C901 - bounded native session
    import numpy as np
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.mapcanvas import TrackMap
    from gweatherrouting.qt.weatherpanel import WeatherPanel
    from gweatherrouting.rendering.windpainter import (
        WindPainter,
        mercator_project_flat,
        BARB_SHAFT_LEN,
        BARB_LEN,
    )

    SystemCredentialStore.load = lambda self: None
    app = QApplication.instance() or QApplication([])
    controller = SessionController(offline=True)
    controller.settings["gribBarbDensity"] = "all"
    controller.settings["gribWindStyle"] = "barbs"
    controller.settings["weatherRenderer"] = "viewport"
    controller.settings["gribArrowOnGround"] = True
    canvas = TrackMap(controller)
    canvas.resize(710, 743)
    panel = WeatherPanel(controller, lambda: canvas, lambda: None, lambda: None)
    report["observations"] = {}

    def wait(predicate, seconds=15):
        return _wait(predicate, max(0, min(seconds, deadline - time.perf_counter())))

    def center():
        canvas._center_lat, canvas._center_lon, canvas._zoom = 48.1125, -122.7375, 9.5
        canvas._invalidate_base()
        canvas.update()

    def current_job():
        state = canvas.wind._states.get(canvas)
        if state is None or state.frame is None:
            return None
        job = state.frame[0]
        sources = controller.display_forecast.sources()
        if not sources or job.palette[0] != controller.time_control.time:
            return None
        if tuple(entry[0].path for entry in job.charts) != tuple(
            s.path for s in sources
        ):
            return None
        return job if getattr(job, "barb_flat", None) is not None else None

    def wind_image():
        image = QImage(710, 743, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            canvas._paint_wind_barbs(painter)
        finally:
            painter.end()
        values = np.frombuffer(image.constBits(), np.uint8).reshape(
            743, image.bytesPerLine()
        )
        return int((values[:, 3 : 710 * 4 : 4] > 0).sum())

    def membership(name):
        """Observe the actual CPU foreground selection; compare every eligible node."""
        job = current_job()
        if job is None:
            return False
        captured = []
        original = WindPainter.build_barb_geometry

        def observe(xs, ys, directions, speeds):
            captured.append(
                tuple(np.array(a, copy=True) for a in (xs, ys, directions, speeds))
            )
            return original(xs, ys, directions, speeds)

        WindPainter.build_barb_geometry = staticmethod(observe)
        began = time.perf_counter()
        try:
            job.barb_geometry_view = None
            ink = wind_image()
        finally:
            WindPainter.build_barb_geometry = staticmethod(original)
        if not captured:
            return False
        dirs, speeds, lats, lons = job.barb_flat
        xs, ys = mercator_project_flat(
            lats, lons, canvas._center_lat, canvas._center_lon, canvas._zoom, 710, 743
        )
        margin = BARB_SHAFT_LEN + BARB_LEN + 4
        eligible = (
            np.isfinite(dirs)
            & np.isfinite(speeds)
            & np.isfinite(xs)
            & np.isfinite(ys)
            & (xs >= -margin)
            & (xs <= 710 + margin)
            & (ys >= -margin)
            & (ys <= 743 + margin)
        )
        if not controller.settings.gribArrowOnGround:
            eligible &= ~job.barb_land
        expected = (xs[eligible], ys[eligible], dirs[eligible], speeds[eligible])
        actual = captured[-1]
        exact = len(actual[0]) == int(eligible.sum()) and all(
            np.allclose(a, b, rtol=0, atol=1e-6) for a, b in zip(actual, expected)
        )
        source = controller.display_forecast.sources()[0]
        identity = (
            getattr(job.charts[0][0], "display_identity", None)
            == source.display_identity
        )
        report["observations"][name] = {
            "eligible_nodes": int(eligible.sum()),
            "drawn_nodes": len(actual[0]),
            "nonblank_wind_pixels": ink,
            "node_membership_exact": bool(exact),
            "source_run_member_exact": bool(identity),
            "selection_and_paint_wall_s": round(time.perf_counter() - began, 4),
            "accounted_native_array_bytes": sum(a.nbytes for a in job.barb_flat)
            + (0 if job.barb_land is None else job.barb_land.nbytes),
        }
        return bool(exact and identity and ink > 0)

    try:
        canvas.show()
        wait(lambda: not controller.display_forecast.loading)
        choices = controller.display_forecast.choices
        preferred = [c for c in choices if "hrrr" in c.name.lower()]
        choice = (preferred or list(choices))[0]
        panel.display_forecast.setCurrentIndex(
            panel.display_forecast.findData(choice.key)
        )
        controller.time_control.set_time(
            datetime.fromtimestamp(choice.first_valid + 3600)
        )
        center()
        ready = wait(lambda: current_job() is not None)
        report["checks"]["all_points_nonblank_membership"] = ready and membership(
            "all_initial"
        )
        report["checks"]["dense_uncapped"] = (
            report["observations"].get("all_initial", {}).get("drawn_nodes", 0) > 900
        )
        report["checks"]["explicit_legacy_not_loading"] = bool(
            "unthinned" in canvas._weather_presenter.status.text()
            and not canvas._weather_presenter.active
            and controller.settings.gribBarbDensity == "all"
        )
        report["checks"]["charts_navigation_retained"] = wait(
            lambda: (canvas._base_cache is not None and canvas._nav_cache is not None)
        )
        panel.on_ground.setChecked(False)
        _pump(100)
        report["checks"]["shoreline_membership"] = membership("all_shoreline")

        # All → thinned → All must stop one presenter before showing the other.
        panel.barb_density.setCurrentIndex(panel.barb_density.findData("very_dense"))
        viewport_ready = wait(
            lambda: (
                canvas._weather_presenter.widget is not None
                and canvas._weather_presenter.widget._state == "ready"
                and canvas._weather_presenter.enc_active
            ),
            20,
        )
        panel.barb_density.setCurrentIndex(panel.barb_density.findData("all"))
        legacy_ready = wait(
            lambda: (
                not canvas._weather_presenter.active
                and canvas._weather_presenter.widget is None
                and current_job() is not None
            )
        )
        report["checks"]["density_roundtrip_no_duplicate"] = bool(
            viewport_ready and legacy_ready and membership("all_return")
        )

        panel.wind_opacity.setValue(0)
        wait(lambda: current_job() is not None and current_job().palette[1] == 0)
        report["checks"]["zero_opacity_hides_wind"] = wind_image() == 0
        panel.wind_opacity.setValue(40)
        wait(lambda: current_job() is not None and current_job().palette[1] == 0.4)
        panel.wind.setChecked(False)
        report["checks"]["visibility_hides_wind"] = wind_image() == 0
        panel.wind.setChecked(True)
        canvas.queue_draw()
        wait(lambda: current_job() is not None)
        report["checks"]["visible_wind_recovers"] = membership("all_visible_again")
        canvas._center_lon += 0.02
        canvas._invalidate_base()
        canvas.update()
        _pump(150)
        report["checks"]["dense_camera_input"] = bool(
            canvas._center_lon != -122.7375 and membership("all_panned")
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        canvas.grab().save(str(output.with_suffix(".png")))
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
    report = run(args.snapshot, args.output, application=application)
    report["limits"] = (
        "CPU wall spans and accounted arrays only; no FPS, RSS/VRAM or plateau claim"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
