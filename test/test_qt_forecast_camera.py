"""Asynchronous forecast publication must not fit over the saved camera."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject

from gweatherrouting.qt.displayforecast import DisplayForecastSelection
from gweatherrouting.qt.gribviewport import ViewportCamera
from gweatherrouting.qt.mapforecast import MapForecastMixin
from gweatherrouting.qt.viewportmap import ViewportCameraState
from gweatherrouting.qt.weatherpresenter import WeatherPresenter


def test_delayed_forecast_metadata_preserves_restored_and_user_camera():
    controller = QObject()
    controller.settings = {"displayForecast": "saved"}
    controller.core = Mock()
    controller.data = Mock()
    controller.core.grib_manager.local_gribs = ()
    controller.core.grib_manager.gribs = ()
    with patch.object(DisplayForecastSelection, "_start"):
        selection = DisplayForecastSelection(controller)
    controller.display_forecast = selection
    state = ViewportCameraState(
        48.36226835822424, -122.87866626968656, 7.868936827179724
    )
    canvas = Mock(disposed=False, controller=controller)
    canvas._center_lat, canvas._center_lon, canvas._zoom = (
        state.center_lat,
        state.center_lon,
        state.zoom,
    )
    canvas._apply_forecast_extents.side_effect = lambda path: (
        MapForecastMixin._apply_forecast_extents(canvas, path)
    )
    selection.changed.connect(
        lambda: MapForecastMixin._display_forecast_changed(canvas)
    )
    source = SimpleNamespace(key="saved", path="saved.grb", start_time=object())

    def camera_values():
        return canvas._center_lat, canvas._center_lon, canvas._zoom

    restored = camera_values()
    # The worker completes after workspace restoration; charts/data may notify
    # again after publication. Neither notification expresses a camera request.
    with patch(
        "gweatherrouting.core.grib.Grib.parse_metadata",
        return_value=SimpleNamespace(bounds=[-180, 90, -180, 90]),
    ):
        selection._ready(selection._signature, (source,), "")
        selection.changed.emit()
    assert camera_values() == restored
    canvas._apply_forecast_extents.assert_not_called()
    canvas.time_control.set_time.assert_called_with(source.start_time)

    def adopt(camera):
        canvas._center_lat, canvas._center_lon, canvas._zoom = (
            camera.center_lat,
            camera.center_lon,
            camera.zoom,
        )
        canvas.view_changed.emit()

    canvas.adopt_viewport_camera.side_effect = adopt
    user_camera = ViewportCamera(47.2, -122.1, 9.5)
    presenter = SimpleNamespace(
        canvas=canvas, closed=False, widget=SimpleNamespace(_camera=user_camera)
    )
    WeatherPresenter._camera_changed(presenter, user_camera)
    assert camera_values() == (47.2, -122.1, 9.5)
    selection.changed.emit()
    assert camera_values() == (47.2, -122.1, 9.5)
    canvas.view_changed.emit.assert_called()
    selection.close()
