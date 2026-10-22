"""Application wiring regressions; native presentation is gated separately."""

from types import SimpleNamespace
from unittest.mock import Mock, call
from datetime import datetime
from threading import Event
from threading import Condition
import importlib.util
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from gweatherrouting.qt import weatherpresenter
from gweatherrouting.qt.gribviewportchart import ViewportChartLayer
from gweatherrouting.rendering.chartcollection import ChartCollection


@pytest.fixture
def presenter(monkeypatch):
    app = QApplication.instance() or QApplication([])
    canvas = QWidget()
    settings = SimpleNamespace(gribWindStyle="barbs", chartPalette="dark")
    settings.get = lambda key, default=None: (
        "viewport" if key == "weatherRenderer" else default
    )
    canvas.controller = SimpleNamespace(settings=settings)
    canvas._invalidate_base = Mock()
    result = weatherpresenter.WeatherPresenter(canvas)
    monkeypatch.setattr(
        weatherpresenter.QGuiApplication, "platformName", lambda: "cocoa"
    )
    yield result
    result.close()
    canvas.close()
    app.processEvents()


def test_app_constructs_enc_only_viewport_by_default(presenter, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(weatherpresenter, "_viewport_widget_class", lambda: factory)
    monkeypatch.setattr(weatherpresenter, "WeatherForeground", Mock())
    model = Mock()
    monkeypatch.setattr(presenter, "_model", lambda source: model)
    presenter._create(None)
    kwargs = factory.call_args.kwargs
    assert kwargs["chart_layer"] and kwargs["enc_chart_layer"] and kwargs["enc_only"]
    assert kwargs["chart_palette"] == "dark"
    assert presenter.enc_active


def test_desktop_map_uses_world_chart_layer_without_enc(presenter, monkeypatch):
    presenter.canvas._desktop_viewport = True
    factory = Mock()
    widget = Mock()
    widget.quickWindow.return_value.afterRendering = Mock()
    monkeypatch.setattr(weatherpresenter, "_viewport_widget_class", lambda: factory)
    monkeypatch.setattr(weatherpresenter, "WeatherForeground", Mock())
    monkeypatch.setattr(presenter, "_model", lambda _source: Mock())

    presenter._create(None)

    kwargs = factory.call_args.kwargs
    assert kwargs["chart_layer"]
    assert not kwargs["enc_chart_layer"]
    assert kwargs["chart_without_weather"]
    assert not kwargs["enc_only"]


def test_chart_child_is_disposed_not_weather_only_replaced(presenter):
    widget, foreground = Mock(), Mock()
    presenter.widget, presenter.foreground = widget, foreground
    presenter._source = object()
    presenter._drop_widget()
    widget.dispose.assert_called_once()
    widget.replace_model.assert_not_called()
    foreground.deleteLater.assert_called_once()
    assert presenter.widget is None and presenter._source is None
    presenter.canvas._invalidate_base.assert_called_once()


def test_app_supplies_basemap_inside_quick_before_weather(presenter):
    canvas = presenter.canvas
    settings = canvas.controller.settings
    settings.gribBarbDensity, settings.gribArrowOpacity = "standard", 1
    canvas._center_lat, canvas._center_lon, canvas._zoom = 48.4, -123.4, 9
    canvas.time_control = SimpleNamespace(time=datetime(2026, 10, 22))
    canvas.wind_visible = True
    canvas._base_key = Mock(return_value="base")
    canvas._base_cache = canvas._environment_cache = None
    canvas._base_image, canvas._environment_image = Mock(), Mock()
    base, environment = object(), object()
    base_target, environment_target = object(), object()
    base_cache, environment_cache = (None,) * 4 + (base,), (None,) * 4 + (environment,)
    canvas._resolve_reusable_layer = Mock(
        side_effect=[(base_cache, base_target), (environment_cache, environment_target)]
    )
    widget = Mock(_chart_palette="dark")
    presenter.widget, presenter.foreground = widget, Mock()
    presenter._sync_active(None)
    widget.set_base.assert_called_once_with(base, base_target)
    widget.set_environment.assert_called_once_with(environment, environment_target)
    assert canvas._base_cache is base_cache
    assert canvas._environment_cache is environment_cache
    calls = [entry[0] for entry in widget.mock_calls]
    assert calls.index("set_base") < calls.index("set_environment")


def test_legacy_recovery_disables_both_viewport_weather_and_enc(presenter):
    presenter.widget = Mock()
    presenter.canvas.controller.settings.get = lambda key, default=None: "legacy"
    assert not presenter.active
    assert not presenter.enc_active


def test_enc_only_worker_never_composes_the_standalone_coastline(tmp_path):
    layer = ViewportChartLayer((), tmp_path, enc_enabled=True, enc_only=True)
    assert layer._base_sources() == ()
    layer.close()


def test_chart_split_does_not_double_draw_enc_or_mbtiles():
    charts = ChartCollection.__new__(ChartCollection)
    charts.charts = []
    charts._palette = "dark"
    charts._chs_renderer = Mock()
    charts._enc_renderer = Mock()
    charts._mbtiles_renderer = Mock()
    canvas = SimpleNamespace(_base_chart_renderer=Mock())
    charts.do_draw(canvas, Mock(), include_enc=False, include_mbtiles=False)
    charts._enc_renderer.draw.assert_not_called()
    charts._enc_renderer.forget.assert_called_once_with(canvas)
    charts._mbtiles_renderer.draw.assert_not_called()
    charts.draw_above_enc(canvas, Mock())
    charts._mbtiles_renderer.draw.assert_called_once()


def test_panning_keeps_the_retained_chart_while_zoom_or_time_clear_it():
    from datetime import datetime as _datetime

    from gweatherrouting.qt.gribviewport import (
        ViewportCamera,
        chart_withdrawn_on_camera_change,
    )

    clock = _datetime(2026, 10, 21, 9)

    def view(lon=-124.3, zoom=9.38, time=clock):
        return ViewportCamera(48.45, lon, zoom, 710, 743, time, "very_dense", False)

    base = view()
    assert not chart_withdrawn_on_camera_change(base, view(lon=-124.1), True)
    assert chart_withdrawn_on_camera_change(base, view(zoom=10.0), True)
    assert chart_withdrawn_on_camera_change(
        base, view(time=_datetime(2026, 10, 21, 10)), True
    )
    assert not chart_withdrawn_on_camera_change(base, view(zoom=10.0), False)


def test_chart_placement_scales_the_retained_zoom_preview_about_its_center():
    from datetime import datetime as _datetime

    from gweatherrouting.core.mapviewport import MapViewport
    from gweatherrouting.qt.gribviewport import ViewportCamera
    from gweatherrouting.qt.gribviewportchart import (
        CHART_SETTLE_MS,
        ChartCamera,
        chart_placement,
    )
    from gweatherrouting.rendering.overlayrenderer import MARGIN

    camera = ViewportCamera(48.45, -124.29, 9.38, 710, 743, _datetime(2026, 10, 22))
    composed = MapViewport(ChartCamera(camera), MARGIN, lambda: None)
    zoomed = ViewportCamera(
        camera.center_lat,
        camera.center_lon,
        camera.zoom + 1,
        camera.width,
        camera.height,
        camera.time,
    )

    x, y, width, height = chart_placement(composed, zoomed)

    assert CHART_SETTLE_MS == 500
    assert width == composed.width * 2
    assert height == composed.height * 2
    assert x + width / 2 == camera.width / 2
    assert y + height / 2 == camera.height / 2


def test_app_image_provider_namespace_matches_its_registration(presenter):
    from gweatherrouting.qt.gribviewportchart import chart_image_provider

    provider = chart_image_provider("environment")
    assert provider.publish(17, Mock()) == "image://environment/17"


def test_missing_forecast_worker_is_terminal_not_permanently_busy():
    from gweatherrouting.qt.gribviewport import GribViewportModel, ViewportCamera

    done = Event()
    model = GribViewportModel(())
    model.on_change = done.set
    try:
        model.update(ViewportCamera(48.1, -122.7, 14, 710, 743, datetime(2026, 10, 19)))
        assert done.wait(5)
        assert model.state == "unavailable"
        assert not model.busy()
        state, _, _ = model.update(
            ViewportCamera(48.2, -122.8, 14.25, 850, 620, datetime(2026, 10, 19))
        )
        assert state == "unavailable"
        assert not model.busy()
    finally:
        model.close()


def test_rollout_readiness_rejects_a_previous_application_camera():
    path = Path(__file__).resolve().parents[1] / "tools/check_enc_app_rollout.py"
    # The owning tool normally runs with tools/ on sys.path.
    import sys

    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("enc_app_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    clock = datetime(2026, 10, 19)
    camera = SimpleNamespace(
        center_lat=48, center_lon=-122, zoom=14, width=710, height=743, time=clock
    )
    canvas = SimpleNamespace(
        _center_lat=48,
        _center_lon=-122,
        _zoom=14,
        width=lambda: 710,
        height=lambda: 743,
        time_control=SimpleNamespace(time=clock),
        _environment_cache=(
            None,
            None,
            None,
            None,
            SimpleNamespace(cacheKey=lambda: 7),
        ),
    )
    widget = SimpleNamespace(
        _camera=camera,
        _environment_key=7,
        _chart_model=SimpleNamespace(
            _condition=Condition(), _published=object(), _reusable=lambda camera: True
        ),
    )
    assert module.current_application_view(canvas, widget)
    canvas._zoom = 14.25
    assert not module.current_application_view(canvas, widget)
    canvas._zoom = 14
    widget._environment_key = 6
    assert not module.current_application_view(canvas, widget)


def test_basemap_gate_checks_the_displayed_chart_not_legacy_cache():
    import sys

    from PySide6.QtCore import QUrl
    from gweatherrouting.qt.gribviewport import ViewportCamera
    from gweatherrouting.qt.gribviewportchart import chart_placement, ChartCamera
    from gweatherrouting.core.mapviewport import MapViewport
    from gweatherrouting.rendering.overlayrenderer import MARGIN

    path = Path(__file__).resolve().parents[1] / "tools/check_desktop_basemap.py"
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("basemap_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    clock = datetime(2026, 10, 22)
    camera = ViewportCamera(48.45, -124.29, 9.38, 710, 743, clock)
    viewport = MapViewport(ChartCamera(camera), MARGIN, lambda: None)
    properties = dict(
        zip(
            ("imageX", "imageY", "imageWidth", "imageHeight"),
            chart_placement(viewport, camera),
        )
    )
    properties.update(ready=True, source=QUrl("image://chart/3"))
    widget = SimpleNamespace(
        _camera=camera,
        _chart_viewport=viewport,
        _chart=SimpleNamespace(isVisible=lambda: True, property=properties.get),
        _chart_model=SimpleNamespace(
            state="ready",
            _published=(3, None, {"viewport": viewport}),
            _reusable=lambda camera: True,
        ),
    )
    canvas = SimpleNamespace(
        _center_lat=48.45,
        _center_lon=-124.29,
        _zoom=9.38,
        width=lambda: 710,
        height=lambda: 743,
        time_control=SimpleNamespace(time=clock),
        _weather_presenter=SimpleNamespace(widget=widget, _rendered=True),
    )
    # No legacy cache/renderer exists in this fixture.
    assert module.current_chart(canvas)
    canvas._zoom += 0.25
    assert not module.current_chart(canvas)
    canvas._zoom = camera.zoom
    properties["source"] = QUrl("image://chart/2")
    assert not module.current_chart(canvas)
    properties["source"] = QUrl("image://chart/3")
    properties["imageWidth"] += 1
    assert not module.current_chart(canvas)
    properties["imageWidth"] -= 1
    widget._chart_model._reusable = lambda camera: False
    assert not module.current_chart(canvas)


def test_viewport_camera_handoff_is_encapsulated_by_the_map_host():
    from gweatherrouting.qt.viewportmap import ViewportMap

    changed = Mock()
    foreground = SimpleNamespace(update=Mock())
    host = SimpleNamespace(
        _center_lat=48.0,
        _center_lon=-123.0,
        _zoom=8.0,
        _cancel_cursor=Mock(),
        invalidate_navigation=Mock(),
        view_changed=SimpleNamespace(emit=changed),
        _weather_presenter=SimpleNamespace(foreground=foreground),
    )
    camera = SimpleNamespace(center_lat=48.45, center_lon=-124.29, zoom=9.38)

    assert ViewportMap.adopt_viewport_camera(host, camera)
    assert (host._center_lat, host._center_lon, host._zoom) == (
        camera.center_lat,
        camera.center_lon,
        camera.zoom,
    )
    host._cancel_cursor.assert_called_once_with()
    host.invalidate_navigation.assert_called_once_with()
    changed.assert_called_once_with()
    foreground.update.assert_called_once_with()

    assert not ViewportMap.adopt_viewport_camera(host, camera)
    assert changed.call_count == 1


def test_map_host_translates_app_state_to_the_viewport_camera_contract():
    from gweatherrouting.qt.gribviewport import ViewportCamera
    from gweatherrouting.qt.viewportmap import ViewportMap

    instant = datetime(2026, 10, 22, 12)
    host = SimpleNamespace(
        _center_lat=48.45,
        _center_lon=-124.29,
        _zoom=9.38,
        width=lambda: 710,
        height=lambda: 743,
        time_control=SimpleNamespace(time=instant),
        controller=SimpleNamespace(settings=SimpleNamespace(gribBarbDensity="dense")),
    )

    assert ViewportMap.requested_viewport_camera(host) == ViewportCamera(
        48.45,
        -124.29,
        9.38,
        710,
        743,
        instant,
        "dense",
        True,
    )


def test_map_host_publishes_existing_routes_to_the_viewport_overlay_layer():
    from gweatherrouting.qt.viewportmap import ViewportMap

    class Route:
        visible = True
        points = ((48.4, -124.3), (48.5, -124.2))

        def __len__(self):
            return len(self.points)

    active_route = Route()
    host = SimpleNamespace(
        controller=SimpleNamespace(
            chart_errors=(),
            core=SimpleNamespace(
                routingManager=SimpleNamespace(get_by_name=lambda name: active_route)
            ),
        ),
        navigation=SimpleNamespace(hl_routing="selected route"),
        selected_route_point=1,
        route_plan=((48.1, -124.1), (48.2, -124.0)),
        route_preview=((48.3, -124.3), (48.4, -124.2)),
        points=(
            SimpleNamespace(lat=48.0, lon=-124.0),
            SimpleNamespace(lat=48.1, lon=-124.1),
            SimpleNamespace(lat=48.2, lon=-124.2),
        ),
        offsets=(0, 10, 20),
        position=15,
        _MAX_TRACK_POLYLINE=1500,
        move_callback=None,
        dashboard_visible=True,
    )

    assert ViewportMap.viewport_route_overlay(host) == {
        "route_plan": [[48.1, -124.1], [48.2, -124.0]],
        "route_preview": [[48.3, -124.3], [48.4, -124.2]],
        "selected_point": [48.5, -124.2],
        "track_points": [[48.0, -124.0], [48.1, -124.1], [48.2, -124.2]],
        "track_position": [48.1, -124.1],
        "caption": "",
        "dashboard_visible": True,
    }


def test_route_overlay_is_drawn_above_weather_in_the_viewport_scene():
    import gweatherrouting.qt.gribviewport as viewport_module

    scene = (
        Path(viewport_module.__file__).parent / "shaders" / "ViewportChartScene.qml"
    ).read_text()
    assert scene.index('objectName: "weatherBarbs"') < scene.index(
        'objectName: "navigationOverlay"'
    )
    assert scene.index('objectName: "navigationOverlay"') < scene.index(
        'objectName: "routeOverlay"'
    )
    assert scene.index('objectName: "routeOverlay"') < scene.index(
        'objectName: "dashboardOverlay"'
    )
    assert "drawRoute(context, routePlan" in scene
    assert "drawRoute(context, routePreview" in scene
    assert "trackPoints" in scene
    assert "trackPosition" in scene


def test_viewport_publishes_retained_navigation_images_without_reupload():
    from PySide6.QtCore import QRectF, QUrl
    from PySide6.QtGui import QImage
    from gweatherrouting.qt.gribviewport import _viewport_widget_class

    provider = SimpleNamespace(publish=Mock(return_value="image://navigation/4"))
    item = SimpleNamespace(setProperty=Mock())
    viewport = SimpleNamespace(
        _navigation_overlay=item,
        _navigation_provider=provider,
        _navigation_key=None,
    )
    image = QImage(20, 10, QImage.Format.Format_ARGB32_Premultiplied)
    target = QRectF(-5, -7, 30, 24)
    method = _viewport_widget_class().set_navigation_overlay

    method(viewport, image, target)
    provider.publish.assert_called_once_with(image.cacheKey(), image)
    assert item.setProperty.call_args_list[0].args == (
        "source",
        QUrl("image://navigation/4"),
    )
    assert item.setProperty.call_args_list[-4:] == [
        call("imageX", target.x()),
        call("imageY", target.y()),
        call("imageWidth", target.width()),
        call("imageHeight", target.height()),
    ]

    method(viewport, image, target)
    assert provider.publish.call_count == 1


def test_viewport_dashboard_layer_publishes_and_clears_the_cached_image():
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QImage
    from gweatherrouting.qt.gribviewport import _viewport_widget_class

    provider = SimpleNamespace(publish=Mock(return_value="image://dashboard/9"))
    item = SimpleNamespace(setProperty=Mock())
    viewport = SimpleNamespace(
        _dashboard_overlay=item,
        _dashboard_provider=provider,
        _dashboard_key=None,
        width=lambda: 800,
        height=lambda: 600,
    )
    image = QImage(800, 600, QImage.Format.Format_ARGB32_Premultiplied)
    method = _viewport_widget_class().set_dashboard_overlay

    method(viewport, image)
    provider.publish.assert_called_once_with(image.cacheKey(), image)
    assert item.setProperty.call_args_list[0] == call(
        "source", QUrl("image://dashboard/9")
    )
    assert item.setProperty.call_args_list[-4:] == [
        call("imageX", 0),
        call("imageY", 0),
        call("imageWidth", 800),
        call("imageHeight", 600),
    ]

    method(viewport, None)
    assert item.setProperty.call_args == call("source", QUrl(""))
    assert viewport._dashboard_key is None
