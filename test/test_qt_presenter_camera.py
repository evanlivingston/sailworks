"""Host camera requests must win over synchronous child lifecycle signals."""

from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QRect, QSize

from sailworks.qt.gribviewport import ViewportCamera
from sailworks.qt.viewportmap import ViewportCameraState, ViewportMap
from sailworks.qt.weatherpresenter import WeatherPresenter


def test_sync_adopts_restored_camera_before_child_resize_or_show():
    state = ViewportCameraState(47.2, -122.1, 9.5)
    canvas = Mock()
    canvas._desktop_viewport = True
    canvas.controller.settings.chartPalette = "day"
    canvas.requested_viewport_camera.side_effect = lambda: ViewportCamera(
        state.center_lat, state.center_lon, state.zoom
    )
    widget = Mock()
    widget._camera = ViewportCamera(90, -180, 6)
    widget._chart_palette = "day"
    widget._environment = None
    presenter = SimpleNamespace(
        canvas=canvas,
        widget=widget,
        foreground=Mock(),
        _source=None,
        _rendered=True,
    )

    def adopt(camera):
        state.center_lat = camera.center_lat
        state.center_lon = camera.center_lon
        state.zoom = camera.zoom

    canvas.adopt_viewport_camera.side_effect = adopt
    presenter.closed = False
    canvas.disposed = False

    def set_camera(camera):
        widget._camera = camera
        WeatherPresenter._camera_changed(presenter, camera)

    def lifecycle(*_):
        WeatherPresenter._camera_changed(presenter, widget._camera)

    widget.set_camera.side_effect = set_camera
    widget.setGeometry.side_effect = lifecycle
    widget.set_wind_style.side_effect = lifecycle
    widget.show.side_effect = lifecycle
    WeatherPresenter._sync_active(presenter, None)
    assert (state.center_lat, state.center_lon, state.zoom) == (47.2, -122.1, 9.5)
    assert widget._camera == ViewportCamera(47.2, -122.1, 9.5)
    widget.set_camera.assert_called_once()


def test_loading_child_retries_the_same_camera():
    state = ViewportCameraState(47.2, -122.1, 9.5)
    canvas = Mock()
    canvas._desktop_viewport = True
    canvas.controller.settings.chartPalette = "day"
    canvas.requested_viewport_camera.side_effect = lambda: ViewportCamera(
        state.center_lat, state.center_lon, state.zoom
    )
    widget = Mock()
    widget._camera = ViewportCamera(47.2, -122.1, 9.5)
    widget._state = "loading"
    widget._chart_palette = "day"
    widget._environment = None
    presenter = SimpleNamespace(
        canvas=canvas,
        widget=widget,
        foreground=Mock(),
        _source=None,
        _rendered=True,
    )
    presenter.closed = False
    canvas.disposed = False
    WeatherPresenter._sync_active(presenter, None)
    widget.set_camera.assert_called_once_with(widget._camera)


def test_uninitialized_child_cannot_overwrite_host_camera():
    canvas = Mock(disposed=False)
    presenter = SimpleNamespace(
        canvas=canvas, closed=False, widget=SimpleNamespace(_camera=None)
    )
    WeatherPresenter._camera_changed(presenter, ViewportCamera(90, -180, 6))
    canvas.adopt_viewport_camera.assert_not_called()


def test_user_camera_change_is_adopted_after_initialization():
    camera = ViewportCamera(48.4, -123.4, 8)
    canvas = Mock(disposed=False)
    presenter = SimpleNamespace(
        canvas=canvas, closed=False, widget=SimpleNamespace(_camera=camera)
    )
    WeatherPresenter._camera_changed(presenter, camera)
    canvas.adopt_viewport_camera.assert_called_once_with(camera)


def test_camera_keeps_full_height_for_overlay_timeline():
    class Canvas:
        camera_state = ViewportCameraState(47.2, -122.1, 9.5)
        time_control = SimpleNamespace(time=None)
        controller = SimpleNamespace(
            settings=SimpleNamespace(gribBarbDensity="standard")
        )
        viewport_rect = ViewportMap.viewport_rect
        _view_pixel_size = ViewportMap._view_pixel_size

        def __init__(self, timeline_visible):
            self.timeline = SimpleNamespace(
                isVisible=lambda: timeline_visible, height=lambda: 112
            )

        def rect(self):
            return QRect(0, 0, 904, 823)

        def devicePixelRatioF(self):
            return 2.0

    # The timeline overlays the native viewport, so it never reflows the map.
    for visible in (True, False):
        camera = ViewportMap.requested_viewport_camera(Canvas(visible))
        assert (camera.width, camera.height) == (904, 823)
        assert Canvas(visible)._view_pixel_size() == (904, 823, 2.0)


def test_camera_adopts_the_host_viewport_after_layout():
    camera = ViewportCamera(47.2, -122.1, 9.5, 904, 823)
    canvas = Mock()
    canvas._desktop_viewport = True
    canvas.controller.settings.chartPalette = "day"
    canvas.requested_viewport_camera.return_value = camera
    widget = Mock()
    widget._camera = camera
    widget._state = "ready"
    widget._chart_palette = "day"
    widget._environment = None
    host = Mock()
    host.size.return_value = QSize(904, 711)
    presenter = SimpleNamespace(
        canvas=canvas,
        widget=widget,
        foreground=Mock(),
        host_widget=host,
        _source=None,
        _rendered=True,
        _layout_host=Mock(),
    )
    presenter.closed = False
    canvas.disposed = False

    WeatherPresenter._sync_active(presenter, None)

    expected = ViewportCamera(47.2, -122.1, 9.5, 904, 711)
    widget.set_camera.assert_called_once_with(expected)


def test_workspace_restart_restores_both_map_cameras():
    from test.qt_preview_checks import QtPreviewChecks

    case = QtPreviewChecks()
    case.setUpClass()
    try:
        case.setUp()
        expected = ((47.2, -122.1, 9.5), (43.1, -125.2, 7.25))
        for canvas, values in zip((case.window.map, case.window.log_map), expected):
            camera = canvas.camera_state
            camera.center_lat, camera.center_lon, camera.zoom = values
            canvas.view_changed.emit()
        case.window.workspace.save()
        case.restart_workspace()
        for canvas, values in zip((case.window.map, case.window.log_map), expected):
            camera = canvas.camera_state
            assert (camera.center_lat, camera.center_lon, camera.zoom) == values
    finally:
        case.doCleanups()
