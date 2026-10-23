"""All-points remains an unthinned working mode, not a viewport loading loop."""

from types import SimpleNamespace

import numpy as np

from test.test_enc_app_rollout import presenter as _presenter_fixture
from gweatherrouting.rendering.windpainter import WindPainter, barb_density_parameters

presenter = _presenter_fixture


def test_all_points_keeps_standalone_viewport_without_changing_the_preference(
    presenter,
):
    presenter.canvas._desktop_viewport = True
    settings = presenter.canvas.controller.settings
    settings.gribBarbDensity = "all"
    assert presenter.active
    assert not presenter.enc_active
    assert settings.gribBarbDensity == "all"
    settings.gribBarbDensity = "very_dense"
    assert presenter.active
    settings.gribBarbDensity = "all"
    assert presenter.active
    assert settings.gribBarbDensity == "all"


def test_all_points_status_is_explicit_not_loading(presenter):
    presenter.canvas._desktop_viewport = True
    presenter.canvas.controller.settings.gribBarbDensity = "all"
    presenter.canvas.controller.display_forecast = SimpleNamespace(
        status="chosen HRRR", error=""
    )
    presenter.canvas.wind_visible = True
    presenter._state = "ready"
    presenter._label()
    text = presenter.status.text()
    assert "chosen HRRR" in text
    assert "Viewport wind (all forecast points, unthinned): ready" in text
    assert "legacy" not in text
    assert "loading" not in text


def test_all_points_keeps_every_eligible_node_above_the_usual_cap(monkeypatch):
    count = 5000
    dirs = np.arange(count, dtype=float)
    speeds = np.full(count, 10.0)
    lats = np.linspace(48.08, 48.13, count)
    lons = np.linspace(-122.77, -122.71, count)
    dirs[2] = np.nan
    speeds[3] = np.nan
    land = np.zeros(count, dtype=bool)
    land[10:110] = True
    selected = []

    def build(xs, ys, directions, velocities):
        selected.extend(directions.tolist())
        return {"nonempty": True}

    monkeypatch.setattr(WindPainter, "build_barb_geometry", build)
    step, cap = barb_density_parameters("all")
    assert step == 0 and cap is None
    geometry, actual_count = WindPainter.live_geometry_for_view(
        (dirs, speeds, lats, lons),
        48.1125,
        -122.7375,
        10,
        710,
        743,
        step=step,
        max_count=cap,
        land_mask=land,
    )
    expected = np.flatnonzero(np.isfinite(dirs) & np.isfinite(speeds) & ~land)
    assert actual_count == len(expected) > 900
    np.testing.assert_array_equal(selected, dirs[expected])
    assert geometry
