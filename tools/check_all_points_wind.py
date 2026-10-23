#!/usr/bin/env python3
"""Native standalone app gate for unthinned GPU wind (#86)."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
from unittest.mock import patch

from check_enc_app_rollout import run, current_application_view
from check_enc_viewport import _wait, _pump


@contextmanager
def selected_wind_sampler(choice):
    """Open an independent oracle reader, not the selection's metadata object."""
    from gweatherrouting.core.grib import Grib
    from gweatherrouting.core.gribsampling import WeatherSampler
    from gweatherrouting.rendering.weathercache import (
        DecodedBandCache,
        DisplayWeatherSource,
    )

    # Match the viewport worker's reader setup, including run/member filtering.
    metadata = Grib.parse_metadata(choice.path)
    source = DisplayWeatherSource(
        metadata.path,
        metadata.name,
        metadata.centre,
        metadata.bounds,
        metadata.start_time,
        metadata.last_forecast,
        metadata.model_name,
        band_cache=DecodedBandCache(128 * 1024 * 1024),
        display_identity=choice.display_identity,
    )
    try:
        sampler = WeatherSampler((source,))
        if len(sampler.indices) != 1 or not any(
            record.element in ("UGRD", "VGRD") for record in sampler.indices[0].records
        ):
            raise ValueError("Selected forecast has no gridded wind")
        yield sampler, source
    finally:
        source.close()


def current_forecast(controller, presenter, choice):
    """Chart readiness alone does not establish selected-weather readiness."""
    selection = controller.display_forecast
    widget = presenter.widget
    return bool(
        not selection.loading
        and selection.selected_key == choice.key
        and selection.sources() == (choice,)
        and presenter._source is choice
        and widget is not None
        and widget._camera is not None
        and widget._camera.time == controller.time_control.time
        and tuple(widget._model.paths) == (choice.path,)
        and widget._model.display_identity == choice.display_identity
    )


def application(report, data, output, deadline):  # noqa: C901
    import numpy as np
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.gribviewport import _source_key
    from gweatherrouting.qt.viewportmap import ViewportMap
    from gweatherrouting.qt.weatherpanel import WeatherPanel
    from gweatherrouting.rendering.weatherbarbs import _native_points, _view_bounds
    from gweatherrouting.rendering.windpainter import WindPainter, mercator_project_flat

    SystemCredentialStore.load = lambda self: None
    app = QApplication.instance() or QApplication([])
    controller = SessionController(offline=True)
    controller.settings["gribBarbDensity"] = "all"
    controller.settings["gribWindStyle"] = "barbs"
    controller.settings["weatherRenderer"] = "viewport"
    controller.settings["gribArrowOnGround"] = True
    canvas = ViewportMap(controller)
    canvas.resize(710, 743)
    panel = WeatherPanel(controller, lambda: canvas, lambda: None, lambda: None)
    report["observations"] = {}
    choice = None

    def wait(predicate, seconds=20):
        return _wait(predicate, max(0, min(seconds, deadline - time.perf_counter())))

    def ready():
        presenter = canvas._weather_presenter
        widget = presenter.widget
        return bool(
            choice is not None
            and current_forecast(controller, presenter, choice)
            and presenter.active
            and widget is not None
            and widget._state == "ready"
            and widget._barbs.property("ready")
            and widget._camera.density == controller.settings.gribBarbDensity
            and current_application_view(canvas, widget)
        )

    def pixels():
        image = canvas._weather_presenter.widget.grabFramebuffer()
        return np.frombuffer(image.constBits(), np.uint8).copy()

    def membership(name):
        if not wait(ready):
            return False
        widget = canvas._weather_presenter.widget
        camera = widget._camera
        state, field, selection = widget._model.update(camera)
        if state != "ready":
            return False
        with selected_wind_sampler(choice) as (sampler, source):
            if field.identity.source != (_source_key(source),):
                raise AssertionError("Rendered wind does not match selected forecast")
            points = _native_points(sampler, _view_bounds(camera.view()), lambda: False)
            uv = sampler.sample_timestamps(
                "wind", camera.stamp(), points[:, 0], points[:, 1]
            )
        xx, yy = mercator_project_flat(
            points[:, 0],
            points[:, 1],
            camera.center_lat,
            camera.center_lon,
            camera.zoom,
            camera.width,
            camera.height,
        )
        eligible = (
            np.isfinite(uv).all(axis=1)
            & np.isfinite(xx)
            & np.isfinite(yy)
            & (xx >= -32)
            & (xx <= camera.width + 32)
            & (yy >= -32)
            & (yy <= camera.height + 32)
        )
        if not camera.over_land:
            widget._model._land.refresh_source(camera.view().get_scale())
            eligible &= np.array(
                [not widget._model._land.contains(*point) for point in points]
            )
        ids = selection.node_ids.ravel()
        ids = ids[ids >= 0].astype(int)
        expected, actual = points[eligible], field.points[ids]
        order = lambda a: a[np.lexsort((a[:, 0], a[:, 1]))]
        exact = (
            actual.shape == expected.shape
            and len(np.unique(ids)) == len(expected)
            and np.allclose(order(actual), order(expected), rtol=0, atol=1e-9)
        )
        # Observe actual GPU ink by withdrawing wind alone, not CPU painting.
        before = pixels()
        widget.set_wind_opacity(0)
        _pump(100)
        hidden = pixels()
        widget.set_wind_opacity(controller.settings.gribArrowOpacity)
        _pump(100)
        ink = int(np.count_nonzero(before != hidden))
        report["observations"][name] = {
            "eligible_nodes": len(expected),
            "drawn_nodes": selection.count,
            "membership_exact": bool(exact),
            "wind_pixel_changes": ink,
            "forecast_time": camera.time.isoformat(),
            "source_paths": [choice.path],
            "display_identity": choice.display_identity,
            "mesh_batches": int(
                np.ceil(selection.shape[0] / widget._barbs.property("batchRows"))
            ),
        }
        return bool(exact and ink > 0 and selection.count == len(expected))

    # A call here is a test failure, never an allowed recovery path.
    with patch.object(
        WindPainter,
        "live_geometry_for_view",
        side_effect=AssertionError("legacy CPU wind"),
    ) as cpu:
        try:
            canvas.show()
            if not wait(lambda: not controller.display_forecast.loading):
                raise RuntimeError("Forecast metadata did not finish loading")
            choices = controller.display_forecast.choices
            if not choices:
                raise RuntimeError(
                    "No fixture forecasts available: "
                    + controller.display_forecast.error
                )
            preferred = [choice for choice in choices if "hrrr" in choice.name.lower()]
            choice = (preferred or list(choices))[0]
            index = panel.display_forecast.findData(choice.key)
            if index < 0:
                raise RuntimeError("Fixture forecast is absent from the Weather panel")
            panel.display_forecast.setCurrentIndex(index)
            if not wait(lambda: controller.display_forecast.sources() == (choice,)):
                raise RuntimeError("Weather panel did not select the fixture forecast")
            controller.time_control.set_time(
                datetime.fromtimestamp(choice.first_valid + 3600)
            )
            canvas._center_lat, canvas._center_lon, canvas._zoom = (
                48.1125,
                -122.7375,
                9.5,
            )
            canvas._invalidate_base()
            canvas.update()
            report["checks"]["all_points_nonblank_membership"] = membership(
                "all_initial"
            )
            report["checks"]["dense_uncapped"] = (
                report["observations"].get("all_initial", {}).get("drawn_nodes", 0)
                > 900
            )
            report["checks"]["explicit_gpu_not_loading"] = bool(
                canvas._weather_presenter.active
                and "unthinned" in canvas._weather_presenter.status.text()
                and "loading" not in canvas._weather_presenter.status.text().lower()
            )
            report["checks"]["charts_navigation_retained"] = bool(
                ready()
                and canvas._weather_presenter.widget._chart.property("ready")
                and canvas._weather_presenter.widget._environment.property("ready")
            )
            panel.set_preference("gribArrowOnGround", False)
            report["checks"]["shoreline_membership"] = membership("all_shoreline")
            panel.barb_density.setCurrentIndex(
                panel.barb_density.findData("very_dense")
            )
            thinned = wait(ready)
            panel.barb_density.setCurrentIndex(panel.barb_density.findData("all"))
            report["checks"]["density_roundtrip_no_duplicate"] = thinned and membership(
                "all_return"
            )
            panel.wind_opacity.setValue(0)
            report["checks"]["zero_opacity_hides_wind"] = wait(
                lambda: canvas._weather_presenter.widget._barbs.opacity() == 0
            )
            panel.wind_opacity.setValue(40)
            panel.wind.setChecked(False)
            report["checks"]["visibility_hides_wind"] = wait(
                lambda: canvas._weather_presenter.widget._barbs.opacity() == 0
            )
            panel.wind.setChecked(True)
            report["checks"]["visible_wind_recovers"] = membership("all_visible_again")
            canvas._center_lon += 0.02
            canvas._invalidate_base()
            canvas.update()
            report["checks"]["dense_camera_input"] = membership("all_panned")
            controller.time_control.set_time(
                controller.time_control.time + timedelta(hours=1)
            )
            report["checks"]["forecast_time_selection"] = membership("all_later")
            output.parent.mkdir(parents=True, exist_ok=True)
            canvas.grab().save(str(output.with_suffix(".png")))
            canvas._weather_presenter.fail("injected all-points renderer failure")
            _pump(100)
            report["checks"]["failure_no_cpu_recovery"] = bool(
                not canvas._weather_presenter.active
                and "unavailable" in controller.renderer_status().lower()
                and cpu.call_count == 0
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
    report = run(args.snapshot, args.output, application=application)
    report["limits"] = "Functional GPU readback only; no performance or RSS/VRAM claim"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
