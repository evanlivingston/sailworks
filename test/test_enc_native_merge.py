"""The integrated native-weather host must preserve ENC ownership."""

from unittest.mock import Mock

import pytest

from gweatherrouting.qt import weatherpresenter

pytest_plugins = ["test.test_enc_app_rollout"]


def test_factory_gets_both_enc_and_native_options(presenter, monkeypatch):
    presenter.canvas.controller.native_weather_fields = True
    presenter.canvas.controller.native_weather_budget = object()
    factory = Mock()
    monkeypatch.setattr(weatherpresenter, "_viewport_widget_class", lambda: factory)
    monkeypatch.setattr(weatherpresenter, "WeatherForeground", Mock())
    monkeypatch.setattr(presenter, "_model", lambda source: Mock())
    presenter._create(None)
    kwargs = factory.call_args.kwargs
    assert kwargs["chart_layer"] and kwargs["enc_chart_layer"] and kwargs["enc_only"]
    assert kwargs["chart_palette"] == "dark"
    assert kwargs["native_weather_fields"] is True
    assert (
        kwargs["native_weather_budget"]
        is presenter.canvas.controller.native_weather_budget
    )
    assert presenter.enc_active


@pytest.mark.parametrize("wind_visible,source", [(True, None), (False, object())])
def test_native_chart_host_syncs_without_visible_weather(
    presenter, monkeypatch, wind_visible, source
):
    canvas = presenter.canvas
    canvas.controller.native_weather_fields = True
    canvas.controller.display_forecast = Mock()
    canvas.controller.display_forecast.sources.return_value = (
        [] if source is None else [source]
    )
    canvas.wind_visible = wind_visible
    canvas.disposed = False
    canvas.coverage_notice = Mock()
    canvas.coverage_fetch = Mock()
    canvas.coverage_fetch_buttons = {}
    canvas.show()
    monkeypatch.setattr(presenter, "_sync_active", Mock())
    monkeypatch.setattr(presenter, "_label", Mock())
    monkeypatch.setattr(presenter, "_retire_native_widget", Mock())
    presenter.widget = Mock()
    presenter.sync()
    presenter._sync_active.assert_called_once_with(source)
    presenter._retire_native_widget.assert_not_called()
    # Teardown uses the real close implementation after monkeypatch restoration.
    presenter.widget = None


def test_native_host_retirement_rejects_queued_old_signals(presenter):
    presenter.canvas.controller.native_weather_fields = True
    widget = Mock()
    presenter.widget = widget
    presenter.foreground = Mock()
    old_generation = presenter._host_generation
    presenter._drop_widget()
    widget.dispose.assert_called_once()
    widget.deleteLater.assert_called_once()
    assert presenter.widget is None
    assert presenter._host_generation > old_generation
    presenter._native_failed(old_generation, "obsolete failure")
    assert presenter.reason == ""
    assert not presenter.deadline.isActive()
    presenter.canvas._invalidate_base.assert_called_once()


def test_non_native_source_change_replaces_weather_only_once(presenter):
    widget = Mock()
    presenter.widget = widget
    presenter._model = Mock(return_value=object())
    presenter.source_changed()
    widget.replace_model.assert_called_once()
    presenter.canvas._invalidate_base.assert_called_once()
