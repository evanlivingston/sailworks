"""Application wiring regressions; native presentation is gated separately."""

from types import SimpleNamespace
from unittest.mock import Mock
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
