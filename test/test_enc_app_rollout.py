"""Application wiring regressions; native presentation is gated separately."""

from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch
from datetime import datetime
from threading import Event
from threading import Condition
import importlib.util
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from sailworks.qt import weatherpresenter
from sailworks.qt.gribviewportchart import ViewportChartLayer
from sailworks.rendering.chartcollection import ChartCollection


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
    model_factory = Mock(return_value=model)
    monkeypatch.setattr(presenter, "_model", model_factory)
    presenter._create(None)
    model_factory.assert_called_once_with(None, grid=False)
    kwargs = factory.call_args.kwargs
    assert kwargs["chart_layer"] and kwargs["enc_chart_layer"] and kwargs["enc_only"]
    assert kwargs["chart_palette"] == "dark"
    assert presenter.enc_active


def test_desktop_map_uses_world_chart_layer_with_enc(presenter, monkeypatch):
    presenter.canvas._desktop_viewport = True
    factory = Mock()
    widget = Mock()
    widget.quickWindow.return_value.afterRendering = Mock()
    monkeypatch.setattr(weatherpresenter, "_viewport_widget_class", lambda: factory)
    monkeypatch.setattr(weatherpresenter, "WeatherForeground", Mock())
    model_factory = Mock(return_value=Mock())
    monkeypatch.setattr(presenter, "_model", model_factory)

    presenter._create(None)

    model_factory.assert_called_once_with(None, grid=True)
    kwargs = factory.call_args.kwargs
    assert kwargs["chart_layer"]
    assert kwargs["enc_chart_layer"]
    assert kwargs["chart_without_weather"]
    assert kwargs["gpu_weather_fill"]
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


def test_track_point_fit_action_fits_selected_point_only():
    from sailworks.qt.navigationpanel import NavigationPanel

    class Track:
        points = [(48.1, -123.1, None), (48.2, -123.2, None)]

        def __len__(self):
            return len(self.points)

    track = Track()
    panel = SimpleNamespace(
        core=SimpleNamespace(trackManager=SimpleNamespace(get_active=lambda: track)),
        point_index=1,
        canvas=SimpleNamespace(fit_coordinates=Mock()),
        fit_track=Mock(),
    )

    NavigationPanel.fit_track_point(panel)

    panel.canvas.fit_coordinates.assert_called_once_with([(48.2, -123.2)])
    panel.fit_track.assert_not_called()


def test_app_supplies_basemap_inside_quick_before_weather(presenter):
    canvas = presenter.canvas
    settings = canvas.controller.settings
    settings.gribBarbDensity, settings.gribArrowOpacity = "standard", 1
    settings.gribWindGradient = False
    settings.gribWindGradientOpacity = 1.0
    settings.gribWindGradientSmooth = True
    settings.gribWindGradientLevels = 15
    canvas._center_lat, canvas._center_lon, canvas._zoom = 48.4, -123.4, 9
    canvas.time_control = SimpleNamespace(time=datetime(2026, 10, 22))
    canvas.marine = SimpleNamespace(
        visible=True,
        sources=object(),
        time_control=canvas.time_control,
        watch=Mock(),
    )
    canvas.wind_visible = True
    canvas.ship_observations = SimpleNamespace(reports=[], visible=False)
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
    widget.set_base.assert_called_once_with(base, base_target, (None, None, None))
    widget.set_environment.assert_called_once_with(
        environment, environment_target, (None, None, None)
    )
    assert canvas._base_cache is base_cache
    assert canvas._environment_cache is environment_cache
    calls = [entry[0] for entry in widget.mock_calls]
    assert calls.index("set_base") < calls.index("set_environment")


def test_legacy_recovery_disables_both_viewport_weather_and_enc(presenter):
    presenter.widget = Mock()
    presenter.canvas.controller.settings.gribWindStyle = "legacy"
    presenter.canvas.controller.settings.get = lambda key, default=None: "legacy"
    assert not presenter.active
    assert not presenter.enc_active


def test_all_points_uses_viewport_and_failure_stays_unavailable(presenter, monkeypatch):
    canvas = presenter.canvas
    canvas._desktop_viewport = True
    canvas.controller.settings.gribBarbDensity = "all"
    canvas.controller.display_forecast = SimpleNamespace(sources=lambda: ())
    canvas.controller.set_renderer_status = Mock()
    canvas.disposed = False
    canvas.queue_draw = Mock()
    canvas.coverage_notice, canvas.coverage_fetch = Mock(), Mock()
    canvas.coverage_fetch_buttons = {}
    canvas.show()
    monkeypatch.setattr(presenter, "_label", Mock())
    monkeypatch.setattr(
        presenter, "_sync_active", Mock(side_effect=RuntimeError("GPU lost"))
    )
    assert presenter.active
    presenter.sync()
    presenter._sync_active.assert_called_once_with(None)
    assert not presenter.active
    assert presenter.reason == "GPU lost"
    presenter.sync()
    # In particular, all-density sync must not overwrite unavailable with cpu.
    canvas.controller.set_renderer_status.assert_called_once_with(
        canvas, "unavailable", reason="GPU lost"
    )


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

    from sailworks.qt.gribviewport import (
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

    from sailworks.core.mapviewport import MapViewport
    from sailworks.qt.gribviewport import ViewportCamera
    from sailworks.qt.gribviewportchart import (
        CHART_IDLE_MS,
        ChartCamera,
        chart_placement,
    )
    from sailworks.rendering.overlayrenderer import MARGIN

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

    assert CHART_IDLE_MS == 100
    assert width == composed.width * 2
    assert height == composed.height * 2
    assert x + width / 2 == camera.width / 2
    assert y + height / 2 == camera.height / 2


def test_app_image_provider_namespace_matches_its_registration(presenter):
    from sailworks.qt.gribviewportchart import chart_image_provider

    provider = chart_image_provider("environment")
    assert provider.publish(17, Mock()) == "image://environment/17"


def test_chart_provider_retains_only_front_and_latest_pending(presenter):
    from PySide6.QtGui import QImage
    from sailworks.qt.gribviewportchart import chart_image_provider

    provider = chart_image_provider()
    images = [QImage(n, 1, QImage.Format.Format_ARGB32) for n in range(1, 5)]
    provider.publish_retained(1, images[0])
    provider.publish_retained(2, images[1], 1)
    assert provider.requestImage("1", None, None).width() == 1
    assert provider.requestImage("2", None, None).width() == 2

    provider.publish_retained(3, images[2], 1)
    assert provider.requestImage("1", None, None).width() == 1
    assert provider.requestImage("2", None, None).isNull()
    assert provider.requestImage("3", None, None).width() == 3
    provider.retain(3)
    assert provider.requestImage("1", None, None).isNull()
    provider.publish_retained(4, images[3], 3)
    provider.retain(3)  # failed pending image
    assert provider.requestImage("3", None, None).width() == 3
    assert provider.requestImage("4", None, None).isNull()
    provider.retain()
    assert provider.requestImage("3", None, None).isNull()


def test_chart_qml_status_follows_front_not_pending(presenter):
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QImage
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from sailworks.qt.gribviewportchart import chart_image_provider

    engine = QQmlEngine()
    provider = chart_image_provider()
    engine.addImageProvider("chart", provider)
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    provider.publish_retained(1, image)
    component = QQmlComponent(
        engine,
        QUrl.fromLocalFile(
            str(
                Path(__file__).resolve().parents[1]
                / "sailworks/qt/shaders/ChartLayer.qml"
            )
        ),
    )
    chart = component.create()
    assert chart is not None, component.errorString()
    chart.setProperty("slot0Source", QUrl("image://chart/1"))
    assert chart.property("ready")
    chart.setProperty("slot1Source", QUrl("image://chart/missing"))
    assert chart.property("ready")
    assert chart.property("sourceError") == ""
    chart.setProperty("frontSlot", 1)
    assert not chart.property("ready")
    assert chart.property("sourceError") == "chart image unavailable"
    chart.setProperty("frontSlot", 0)
    assert chart.property("ready")
    assert chart.property("sourceError") == ""
    chart.deleteLater()


def test_chart_staging_ignores_stale_callbacks_and_adopts_metadata(presenter):
    from types import MethodType
    from PySide6.QtGui import QImage
    from sailworks.qt.gribviewport import _viewport_widget_class
    from sailworks.qt.gribviewportchart import chart_image_provider
    from sailworks.qt.viewportpublication import LayerRole, PublicationBudget

    cls = _viewport_widget_class()
    properties = {}
    manager = Mock()
    manager.budget = PublicationBudget()
    assert manager.budget.allocate(LayerRole.CHART, 2 * 2 * 4)
    manager.stage_resources.return_value = True
    manager.verify_and_adopt.return_value = (True, "")
    old_viewport = object()
    widget = SimpleNamespace(
        _disposed=False,
        _chart_payload_valid=Mock(return_value=True),
        _publication_manager=manager,
        _chart_front_slot=0,
        _chart_pending=None,
        _chart_attempt=1,
        _chart_redelivery_generation=None,
        _chart_visible_generation=1,
        _chart_provider=chart_image_provider(),
        _chart=SimpleNamespace(setProperty=properties.__setitem__),
        _chart_viewport=old_viewport,
        _chart_composition_camera="old camera",
        _chart_composition_dpr=1,
        _place_chart=Mock(),
        _chart_state_now=Mock(return_value="ready"),
        _update_enc_status=Mock(),
        _enc_chart_layer=False,
        _chart_ready_at=None,
        counters={"chart_publications": 0},
    )
    for name in (
        "_publish_chart",
        "_reject_chart_pending",
        "_chart_slot_result",
        "_chart_state_changed",
    ):
        setattr(widget, name, MethodType(getattr(cls, name), widget))
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    widget._chart_provider.publish_retained(1, image)
    first = {"viewport": object(), "camera": "first", "factor": 2}
    final_viewport = object()
    final = {"viewport": final_viewport, "camera": "final", "factor": 3}
    widget._publish_chart((2, image, first))
    assert widget._chart_viewport is old_viewport
    assert widget._chart_visible_generation == 1
    widget._publish_chart((3, image, final))
    widget._chart_slot_result(1, "2", True)  # delayed superseded completion
    assert widget._chart_pending[0] == 3
    assert widget._chart_viewport is old_viewport
    widget._chart_slot_result(1, "3", False)
    assert properties["chartUnavailable"] is True
    assert widget._chart_pending is None
    assert widget._chart_visible_generation == 1
    assert widget._chart_viewport is old_viewport
    widget._publish_chart((4, image, final))
    widget._chart_payload_valid.return_value = False
    widget._chart_slot_result(1, "4", True)  # became stale while loading
    assert widget._chart_visible_generation == 1
    assert widget._chart_viewport is old_viewport
    widget._chart_payload_valid.return_value = True
    widget._publish_chart((5, image, final))
    widget._chart_slot_result(1, "5", True)
    assert widget._chart_visible_generation == 5
    assert widget._chart_front_slot == 1
    assert widget._chart_viewport is final_viewport
    assert widget._chart_composition_camera == "final"
    assert widget._chart_composition_dpr == 3
    assert properties["frontSlot"] == 1
    assert properties["chartUnavailable"] is False
    assert widget.counters["chart_publications"] == 1
    assert widget._chart_provider.requestImage("1", None, None).isNull()
    assert not widget._chart_provider.requestImage("5", None, None).isNull()


def test_chart_reuse_redelivers_rejected_cold_start_without_stale_adoption(
    presenter, tmp_path
):
    from types import MethodType

    from PySide6.QtGui import QImage
    from sailworks.qt.gribviewport import _viewport_widget_class
    from sailworks.qt.gribviewportchart import chart_image_provider
    from sailworks.qt.viewportpublication import PublicationBudget

    model = ViewportChartLayer((), tmp_path)
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    metadata = {"viewport": object()}
    model._generation = 1
    model._published = (1, image, metadata)
    model._reusable = lambda camera, preview=False: (
        camera == "covered" and metadata.get("preview", False) == preview
    )
    assert model._reusable("covered", False)
    assert not model._reusable("covered", True)
    metadata["preview"] = True
    assert model._reusable("covered", True)
    assert not model._reusable("covered", False)
    metadata["preview"] = False
    manager = Mock()
    manager.budget = PublicationBudget()
    manager.stage_resources.return_value = True
    manager.verify_and_adopt.return_value = (True, "")
    widget = SimpleNamespace(
        _disposed=False,
        _chart_model=model,
        _camera="outside",
        _chart_pending=None,
        _chart_attempt=0,
        _chart_redelivery_generation=None,
        _chart_front_slot=0,
        _chart_visible_generation=None,
        _chart_provider=chart_image_provider(),
        _chart=SimpleNamespace(setProperty=Mock()),
        _publication_manager=manager,
        _chart_camera_changed=0,
        _weather_ready_at=1,
        _enc_chart_layer=False,
        _model=SimpleNamespace(busy=lambda: False),
        _chart_state_now=Mock(),
        _place_chart=Mock(),
        _update_enc_status=Mock(),
        _chart_ready_at=None,
        counters={"chart_requests": 0, "chart_publications": 0},
    )
    cls = _viewport_widget_class()
    for name in (
        "_publish_chart",
        "_chart_payload_valid",
        "_reject_chart_pending",
        "_chart_slot_result",
        "_request_chart",
    ):
        setattr(widget, name, MethodType(getattr(cls, name), widget))
    try:
        widget._publish_chart(model._published)  # queued delivery after camera moved
        assert widget._chart_pending is None
        widget._camera = "covered"
        widget._request_chart()
        first_token = widget._chart_pending[3]
        widget._request_chart()  # a valid pending delivery is not restaged
        assert widget._chart_pending[3] == first_token
        assert manager.stage_resources.call_count == 1
        widget._camera = "outside"
        widget._chart_slot_result(1, first_token, True)
        assert widget._chart_pending is None
        widget._camera = "covered"
        widget._request_chart()
        second_token = widget._chart_pending[3]
        assert second_token != first_token
        widget._chart_slot_result(1, first_token, True)
        manager.verify_and_adopt.assert_not_called()
        assert widget._chart_pending[3] == second_token
        widget._chart_slot_result(1, second_token, True)
        assert widget._chart_visible_generation == 1
        assert widget.counters["chart_publications"] == 1
        widget._request_chart()
        assert manager.stage_resources.call_count == 2
        assert model.counters["compositions"] == 0
    finally:
        model.close()


def test_clearing_chart_withdraws_visible_manager_state(presenter):
    from types import MethodType

    from PySide6.QtGui import QImage

    from sailworks.qt.gribviewport import _viewport_widget_class
    from sailworks.qt.gribviewportchart import chart_image_provider

    cls = _viewport_widget_class()
    properties = {}
    manager = Mock()
    provider = chart_image_provider()
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    provider.publish_retained(1, image)
    widget = SimpleNamespace(
        _chart_pending=None,
        _chart_attempt=0,
        _chart_redelivery_generation=None,
        _chart_front_slot=0,
        _chart_visible_generation=1,
        _chart_viewport=object(),
        _chart_composition_camera=object(),
        _chart_composition_dpr=2,
        _chart=SimpleNamespace(setProperty=properties.__setitem__),
        _chart_provider=provider,
        _publication_manager=manager,
    )
    for name in ("_reject_chart_pending", "_clear_chart"):
        setattr(widget, name, MethodType(getattr(cls, name), widget))
    widget._clear_chart()
    manager.withdraw_visible.assert_called_once()
    assert widget._chart_visible_generation is None
    assert widget._chart_viewport is None
    assert widget._chart_composition_camera is None
    assert widget._chart_composition_dpr is None
    assert properties["slot0Source"].isEmpty()
    assert provider.requestImage("1", None, None).isNull()


def _budget_chart_widget(image):
    from types import MethodType

    from sailworks.qt.gribviewport import _viewport_widget_class
    from sailworks.qt.gribviewportchart import chart_image_provider
    from sailworks.qt.viewportpublication import (
        LayerIdentity,
        LayerPlacement,
        LayerRole,
        PublicationManager,
        ResourceKind,
    )

    cls = _viewport_widget_class()
    properties = {}
    manager = PublicationManager()
    provider = chart_image_provider()
    provider.publish_retained(1, image)
    assert manager.stage_resources(
        LayerRole.CHART,
        LayerIdentity(LayerRole.CHART, 1, (), "old"),
        LayerPlacement(),
        {ResourceKind.IMAGE: image},
        image.sizeInBytes(),
        completion_token="old",
    )
    assert manager.commit_pending(LayerRole.CHART, "old")[0]
    widget = SimpleNamespace(
        _disposed=False,
        _chart_payload_valid=Mock(return_value=True),
        _chart_pending=None,
        _chart_attempt=0,
        _chart_redelivery_generation=None,
        _chart_front_slot=0,
        _chart_visible_generation=1,
        _chart_viewport=object(),
        _chart_composition_camera=None,
        _chart_composition_dpr=None,
        _chart=SimpleNamespace(setProperty=properties.__setitem__),
        _chart_provider=provider,
        _publication_manager=manager,
        _enc_chart_layer=False,
        _update_enc_status=Mock(),
        _place_chart=Mock(),
        _chart_state_now=Mock(),
        _chart_ready_at=None,
        counters={"chart_publications": 0},
    )
    for name in (
        "_reject_chart_pending",
        "_clear_chart",
        "_chart_state_changed",
        "_publish_chart",
        "_chart_slot_result",
    ):
        setattr(widget, name, MethodType(getattr(cls, name), widget))
    return widget, properties, manager, provider


def test_chart_budget_withdraws_front_and_stages_full_resolution(presenter):
    from PySide6.QtGui import QImage

    image = QImage(3424, 2624, QImage.Format.Format_ARGB32)
    widget, properties, manager, provider = _budget_chart_widget(image)
    widget._publish_chart((2, image, {"viewport": object()}))
    assert widget._chart_visible_generation is None
    assert properties["chartLoading"] is True
    assert provider.requestImage("1", None, None).isNull()
    assert manager.budget.current_usage["chart"] == image.sizeInBytes()
    slot, token = widget._chart_pending[2:]
    assert token == "2"
    widget._chart_slot_result(slot, "1", True)
    assert widget._chart_pending[3] == token
    widget._chart_slot_result(slot, token, True)
    assert widget._chart_visible_generation == 2
    assert manager.budget.current_usage["chart"] == image.sizeInBytes()
    assert not provider.requestImage("2", None, None).isNull()


def test_chart_oversized_budget_refusal_is_terminal_for_generation(presenter):
    from PySide6.QtGui import QImage

    old = QImage(2, 2, QImage.Format.Format_ARGB32)
    widget, properties, manager, provider = _budget_chart_widget(old)
    image = QImage(4097, 4097, QImage.Format.Format_ARGB32)
    payload = (2, image, {"viewport": object()})
    widget._publish_chart(payload)
    assert widget._chart_state == "unavailable"
    assert properties["chartUnavailable"] is True
    assert properties["chartLoading"] is False
    assert widget._chart_visible_generation is None
    assert manager.budget.current_usage["chart"] == 0
    assert provider.requestImage("1", None, None).isNull()
    assert provider.requestImage("2", None, None).isNull()
    attempt = widget._chart_attempt
    widget._publish_chart(payload)
    assert widget._chart_attempt == attempt


def test_chart_global_cap_reclaims_visible_when_layer_cap_allows_pair(presenter):
    from PySide6.QtGui import QImage
    from sailworks.qt.viewportpublication import LayerRole

    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    widget, properties, manager, provider = _budget_chart_widget(image)
    size = image.sizeInBytes()
    manager.budget.global_cap = 2 * size
    assert manager.budget.allocate(LayerRole.FILL, size // 2)
    assert 2 * size <= manager.budget.layer_caps[LayerRole.CHART]

    widget._publish_chart((2, image, {"viewport": object()}))
    assert widget._chart_visible_generation is None
    assert properties["chartLoading"] is True
    slot, token = widget._chart_pending[2:]
    assert token == "2"
    widget._chart_slot_result(slot, "1", True)
    assert widget._chart_pending[3] == token
    widget._chart_slot_result(slot, token, True)
    assert widget._chart_visible_generation == 2
    assert provider.requestImage("2", None, None).size() == image.size()
    assert manager.budget.total_used() <= manager.budget.global_cap


def test_chart_refusal_retries_only_after_capacity_changes(presenter):
    from types import MethodType
    from PySide6.QtGui import QImage
    from sailworks.qt.gribviewport import _viewport_widget_class
    from sailworks.qt.viewportpublication import LayerRole

    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    widget, properties, manager, provider = _budget_chart_widget(image)
    size = image.sizeInBytes()
    assert manager.budget.allocate(LayerRole.FILL, size + 1)
    manager.budget.global_cap = 2 * size
    payload = (2, image, {"viewport": object()})
    widget._publish_chart(payload)
    assert widget._chart_state == "unavailable"
    assert properties["chartUnavailable"] is True
    assert widget._chart_pending is None
    attempt = widget._chart_attempt

    widget._request_chart = MethodType(_viewport_widget_class()._request_chart, widget)
    widget._camera = object()
    widget._weather_ready_at = 1
    widget._chart_camera_changed = 0
    widget._model = SimpleNamespace(busy=Mock(return_value=False))
    widget._chart_model = SimpleNamespace(
        request=Mock(return_value=2), _condition=Condition(), _published=payload
    )
    widget.counters["chart_requests"] = 0
    widget._request_chart()
    widget._request_chart()
    assert widget._chart_attempt == attempt

    manager.budget.release(LayerRole.FILL, 1)
    widget._request_chart()
    assert widget._chart_attempt == attempt + 1
    slot, token = widget._chart_pending[2:]
    widget._chart_slot_result(slot, str(attempt), True)
    assert widget._chart_pending[3] == token
    widget._chart_slot_result(slot, token, True)
    assert widget._chart_visible_generation == 2
    assert provider.requestImage("2", None, None).size() == image.size()


def test_chart_budget_fallback_revalidates_after_withdrawal(presenter):
    from PySide6.QtGui import QImage

    image = QImage(3424, 2624, QImage.Format.Format_ARGB32)
    widget, _, manager, provider = _budget_chart_widget(image)
    widget._chart_payload_valid.side_effect = [True, False]
    widget._publish_chart((2, image, {"viewport": object()}))
    assert widget._chart_pending is None
    assert widget._chart_redelivery_generation == 2
    assert manager.budget.current_usage["chart"] == 0
    assert provider.requestImage("2", None, None).isNull()


def test_missing_forecast_worker_is_terminal_not_permanently_busy():
    from sailworks.qt.gribviewport import GribViewportModel, ViewportCamera

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
    from sailworks.qt.gribviewport import ViewportCamera
    from sailworks.qt.gribviewportchart import chart_placement, ChartCamera
    from sailworks.core.mapviewport import MapViewport
    from sailworks.rendering.overlayrenderer import MARGIN

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
    from sailworks.qt.viewportmap import (
        ViewportCameraState,
        ViewportMapState,
        ViewportMap,
    )

    changed = Mock()
    foreground = SimpleNamespace(update=Mock(), repaint=Mock())
    camera_state = ViewportCameraState(48.0, -123.0, 8.0)
    host = SimpleNamespace(
        camera_state=camera_state,
        _view_state=ViewportMapState(camera_state),
        _cancel_cursor=Mock(),
        invalidate_navigation=Mock(),
        view_changed=SimpleNamespace(emit=changed),
        _weather_presenter=SimpleNamespace(foreground=foreground),
    )
    camera = SimpleNamespace(center_lat=48.45, center_lon=-124.29, zoom=9.38)

    assert ViewportMap.adopt_viewport_camera(host, camera)
    assert (
        host.camera_state.center_lat,
        host.camera_state.center_lon,
        host.camera_state.zoom,
    ) == (
        camera.center_lat,
        camera.center_lon,
        camera.zoom,
    )
    host._cancel_cursor.assert_called_once_with()
    host.invalidate_navigation.assert_called_once_with()
    changed.assert_called_once_with()
    foreground.repaint.assert_called_once_with()
    foreground.update.assert_not_called()

    assert not ViewportMap.adopt_viewport_camera(host, camera)
    assert changed.call_count == 1


def test_map_host_keeps_navigation_cache_for_camera_only_pan():
    from sailworks.qt.viewportmap import (
        ViewportCameraState,
        ViewportMapState,
        ViewportMap,
    )

    camera_state = ViewportCameraState(48.0, -123.0, 8.0)
    host = SimpleNamespace(
        camera_state=camera_state,
        _view_state=ViewportMapState(camera_state),
        _cancel_cursor=Mock(),
        invalidate_navigation=Mock(),
        view_changed=SimpleNamespace(emit=Mock()),
        _weather_presenter=SimpleNamespace(foreground=None),
    )
    camera = SimpleNamespace(center_lat=48.45, center_lon=-124.29, zoom=8.0)

    assert ViewportMap.adopt_viewport_camera(host, camera)
    host.invalidate_navigation.assert_not_called()


def test_zoom_repaints_navigation_after_host_adopts_viewport_camera():
    from sailworks.qt.viewportmap import (
        ViewportCameraState,
        ViewportMapState,
        ViewportMap,
    )

    camera_state = ViewportCameraState(48.0, -123.0, 8.0)
    observed = []
    foreground = SimpleNamespace(
        update=Mock(),
        repaint=lambda: observed.append((camera_state.zoom, host._nav_cache)),
    )
    host = SimpleNamespace(
        camera_state=camera_state,
        _view_state=ViewportMapState(camera_state),
        _nav_cache=object(),
        _cancel_cursor=Mock(),
        view_changed=SimpleNamespace(emit=Mock()),
        _weather_presenter=SimpleNamespace(foreground=foreground, widget=None),
        viewport_route_overlay=Mock(),
    )
    host.invalidate_navigation = lambda: setattr(host, "_nav_cache", None)
    camera = SimpleNamespace(center_lat=48.2, center_lon=-123.1, zoom=8.35)

    assert ViewportMap.adopt_viewport_camera(host, camera)
    assert observed == [(camera.zoom, None)]
    foreground.update.assert_not_called()


def test_map_host_translates_app_state_to_the_viewport_camera_contract():
    from sailworks.qt.gribviewport import ViewportCamera
    from sailworks.qt.viewportmap import (
        ViewportCameraState,
        ViewportMap,
    )

    instant = datetime(2026, 10, 22, 12)
    camera_state = ViewportCameraState(48.45, -124.29, 9.38)
    host = SimpleNamespace(
        camera_state=camera_state,
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


def test_weather_stager_prefers_viewport_camera_and_falls_back_to_trackmap_fields():
    from sailworks.qt.gpuweatherjobs import _camera_coordinates

    legacy = SimpleNamespace(_center_lat=1, _center_lon=2, _zoom=3)
    assert _camera_coordinates(legacy) == (1, 2, 3)
    legacy.camera_state = SimpleNamespace(center_lat=4, center_lon=5, zoom=6)
    assert _camera_coordinates(legacy) == (4, 5, 6)


def test_renderer_failure_reports_unavailable_for_desktop_and_generic_hosts():
    from sailworks.qt.controller import SessionController
    from PySide6.QtWidgets import QWidget
    from sailworks.qt.viewportmap import ViewportMap
    from sailworks.qt.weatherpresenter import WeatherPresenter

    assert issubclass(ViewportMap, QWidget)
    canvas = SimpleNamespace(
        _desktop_viewport=True,
        controller=SimpleNamespace(set_renderer_status=Mock()),
        queue_draw=Mock(),
    )
    presenter = SimpleNamespace(
        closed=False,
        reason="",
        suspend=Mock(),
        canvas=canvas,
        _label=Mock(),
    )

    WeatherPresenter.fail(presenter, "viewport initialization failed")

    presenter.suspend.assert_called_once_with()
    canvas.controller.set_renderer_status.assert_called_once_with(
        canvas, "unavailable", reason="viewport initialization failed"
    )
    canvas.queue_draw.assert_called_once_with()

    controller = SimpleNamespace(
        _renderer_states={
            object(): {
                "actual": "unavailable",
                "reason": "viewport initialization failed",
                "api": "",
            }
        },
        map_renderer="gpu",
        renderer_override=None,
    )
    assert SessionController.renderer_status(controller) == (
        "Viewport unavailable: viewport initialization failed"
    )

    legacy_canvas = SimpleNamespace(
        _desktop_viewport=False,
        controller=SimpleNamespace(set_renderer_status=Mock()),
        queue_draw=Mock(),
    )
    legacy_presenter = SimpleNamespace(
        closed=False,
        reason="",
        suspend=Mock(),
        canvas=legacy_canvas,
        _label=Mock(),
    )
    WeatherPresenter.fail(legacy_presenter, "legacy renderer failure")
    legacy_canvas.controller.set_renderer_status.assert_called_once_with(
        legacy_canvas, "unavailable", reason="legacy renderer failure"
    )


def test_fit_zoom_preserves_projection_bounds_policy():
    import math

    from sailworks.qt.mapstate import fit_zoom

    assert fit_zoom(512, 256, 1104, 592) == 1
    assert fit_zoom(256, 256, 1104, 720) == math.log2(2.5)
    assert fit_zoom(0, 0, 1000, 800) == 14
    assert fit_zoom(1000, 1000, 200, 200) == 1


def test_map_host_size_and_invalidation_helpers_preserve_dpr_contract():
    from sailworks.qt.maphost import MapHostMixin

    host = SimpleNamespace(
        width=lambda: 320,
        height=lambda: 200,
        devicePixelRatioF=lambda: 1.75,
        _render_padding=20,
        _base_cache=object(),
        _environment_cache=object(),
        _nav_cache=object(),
        background=object(),
    )

    assert MapHostMixin.get_allocated_width(host) == 360
    assert MapHostMixin.get_allocated_height(host) == 240
    assert MapHostMixin.get_scale_factor(host) == 2
    assert MapHostMixin._view_pixel_size(host) == (320, 200, 1.75)
    MapHostMixin._invalidate_base(host)
    MapHostMixin.invalidate_navigation(host)
    assert host._base_cache is None
    assert host._environment_cache is None
    assert host._nav_cache is None
    assert host.background is None


def test_environment_layer_excludes_marine_and_weather_raster():
    from datetime import datetime

    from sailworks.qt.mapenvironment import MapEnvironmentMixin

    source = object()
    wind = SimpleNamespace(draw=Mock())

    class Host(MapEnvironmentMixin):
        pass

    host = Host()
    host.get_scale_factor = lambda: 2
    host.get_allocated_width = lambda: 10
    host.get_allocated_height = lambda: 8
    host.charts = SimpleNamespace(draw_above_enc=Mock())
    host.marine = SimpleNamespace()
    host.wind = wind
    host.wind_visible = True
    host.time_control = SimpleNamespace(time=datetime(2026, 10, 22, 12))
    host.controller = SimpleNamespace(
        settings=SimpleNamespace(gribArrowOpacity=0.7),
        display_forecast=SimpleNamespace(sources=lambda: (source,)),
    )
    host._desktop_viewport = True

    image = MapEnvironmentMixin._environment_image(host)

    assert (image.width(), image.height(), image.devicePixelRatio()) == (20, 16, 2)
    host.charts.draw_above_enc.assert_called_once()
    wind.draw.assert_not_called()

    host._desktop_viewport = False
    MapEnvironmentMixin._environment_image(host)
    wind.draw.assert_not_called()


def test_map_state_isochrone_updates_keep_empty_and_nonempty_semantics():
    from sailworks.qt.mapstate import MapStateMixin

    host = SimpleNamespace(
        isochrones=SimpleNamespace(forget=Mock()),
        invalidate_navigation=Mock(),
        update=Mock(),
    )

    MapStateMixin.set_isochrones(host, ())

    assert host.isochrone_frame.isochrones == ()
    assert host.isochrone_frame.path == ()
    assert host.isochrone_frame.exclusions == ()
    host.isochrones.forget.assert_called_once_with(host)
    host.invalidate_navigation.assert_called_once_with()
    host.update.assert_called_once_with()

    MapStateMixin.set_isochrones(host, ((48.4, -123.4),))

    assert host.isochrone_frame.isochrones == ((48.4, -123.4),)
    assert host.isochrone_frame.path == ()
    assert host.isochrone_frame.exclusions == ()
    host.isochrones.forget.assert_called_once_with(host)
    assert host.invalidate_navigation.call_count == 2
    assert host.update.call_count == 2


def test_viewport_foreground_publishes_navigation_instead_of_legacy_painting():
    from sailworks.qt.viewportmap import ViewportMap

    image = object()
    target = object()
    painter = object()
    cache = ("key", 0, 0, 0, image)
    widget = SimpleNamespace(set_navigation_overlay=Mock())
    presenter = SimpleNamespace(active=True, widget=widget)
    host = SimpleNamespace(
        _weather_presenter=presenter,
        _nav_cache=None,
        _resolve_reusable_layer=Mock(return_value=(cache, target)),
        _nav_key=Mock(return_value="key"),
        _navigation_image=Mock(return_value=image),
        _paint_dashboard=Mock(),
        _paint_measurement_overlay=Mock(),
        _paint_route_overlay=Mock(),
        _paint_track_overlay=Mock(),
        _paint_caption=Mock(),
    )

    ViewportMap._paint_foreground(host, painter)

    host._resolve_reusable_layer.assert_called_once_with(
        None, "key", host._navigation_image
    )
    widget.set_navigation_overlay.assert_called_once_with(image, target)
    host._paint_dashboard.assert_called_once_with(painter)
    host._paint_measurement_overlay.assert_called_once_with(painter)
    host._paint_route_overlay.assert_not_called()
    host._paint_track_overlay.assert_not_called()
    host._paint_caption.assert_not_called()


def test_viewport_dashboard_publishes_or_clears_only_through_qml():
    from sailworks.qt.viewportmap import ViewportMap

    image = object()
    widget = SimpleNamespace(set_dashboard_overlay=Mock())
    presenter = SimpleNamespace(active=True, widget=widget)
    painter = SimpleNamespace(drawImage=Mock())
    host = SimpleNamespace(
        _weather_presenter=presenter,
        dashboard_visible=True,
        _dash_cache=None,
        _dash_key=Mock(return_value="dashboard-key"),
        _dashboard_image=Mock(return_value=image),
    )

    ViewportMap._paint_dashboard(host, painter)

    assert host._dash_cache == ("dashboard-key", image)
    widget.set_dashboard_overlay.assert_called_once_with(image)
    painter.drawImage.assert_not_called()

    host.dashboard_visible = False
    ViewportMap._paint_dashboard(host, painter)
    assert widget.set_dashboard_overlay.call_args == call(None)
    painter.drawImage.assert_not_called()

    presenter.active = False
    host.dashboard_visible = True
    widget.set_dashboard_overlay.reset_mock()
    ViewportMap._paint_dashboard(host, painter)
    widget.set_dashboard_overlay.assert_not_called()
    painter.drawImage.assert_not_called()


def test_retained_overlay_mixin_preserves_dpr_and_renderer_draw_calls():
    from sailworks.qt import mapoverlays
    from sailworks.qt.mapoverlays import RetainedOverlayMixin

    surfaces = []

    class Surface:
        def __init__(self, _format, width, height):
            self.width = width
            self.height = height
            surfaces.append(self)

        def set_device_scale(self, _x, _y):
            pass

        def flush(self):
            pass

        def get_data(self):
            return b"pixels"

        def get_width(self):
            return self.width

        def get_height(self):
            return self.height

        def get_stride(self):
            return self.width * 4

    class Image:
        Format = SimpleNamespace(Format_ARGB32_Premultiplied=1)

        def __init__(self, *args):
            self.args = args
            self.dpr = None

        def copy(self):
            return self

        def setDevicePixelRatio(self, dpr):
            self.dpr = dpr

    isochrones = SimpleNamespace(draw=Mock())
    navigation = SimpleNamespace(draw_routes=Mock(), draw_navigation=Mock())
    dashboard = SimpleNamespace(boat_info=None, _draw_dashboard=Mock())
    host = SimpleNamespace(
        get_scale_factor=lambda: 2,
        get_allocated_width=lambda: 10,
        get_allocated_height=lambda: 8,
        width=lambda: 11,
        height=lambda: 9,
        isochrone_frame=SimpleNamespace(isochrones=()),
        isochrones=isochrones,
        navigation=navigation,
        time_control=SimpleNamespace(time=datetime(2026, 10, 22, 12)),
        ais_visible=False,
        dashboard=dashboard,
        controller=SimpleNamespace(core=SimpleNamespace(boat_info=object())),
    )

    with (
        patch.object(mapoverlays.cairo, "ImageSurface", side_effect=Surface),
        patch.object(mapoverlays.cairo, "Context", return_value=object()),
        patch.object(mapoverlays, "QImage", Image),
    ):
        navigation_image = RetainedOverlayMixin._navigation_image(host)
        dashboard_image = RetainedOverlayMixin._dashboard_image(host)

    assert [(surface.width, surface.height) for surface in surfaces] == [
        (20, 16),
        (22, 18),
    ]
    assert navigation_image.dpr == dashboard_image.dpr == 2
    isochrones.draw.assert_called_once()
    navigation.draw_routes.assert_called_once()
    navigation.draw_navigation.assert_called_once()
    dashboard._draw_dashboard.assert_called_once_with(host, ANY)


def test_overlay_service_owns_hide_and_close_lifecycle():
    from sailworks.qt.mapoverlays import MapOverlayServices

    host = object()
    service = object.__new__(MapOverlayServices)
    service.isochrones = SimpleNamespace(forget=Mock())
    service.marine = SimpleNamespace(forget=Mock(), close=Mock())

    service.forget(host)
    service.isochrones.forget.assert_called_once_with(host)
    service.marine.forget.assert_called_once_with(host)

    service.close(host)
    assert service.isochrones.forget.call_count == 2
    service.isochrones.forget.assert_called_with(host)
    service.marine.close.assert_called_once_with()


def test_map_host_publishes_existing_routes_to_the_viewport_overlay_layer():
    from sailworks.qt.viewportmap import (
        ViewportCameraState,
        ViewportMapState,
        ViewportMap,
    )

    class Route:
        visible = True
        points = ((48.4, -124.3), (48.5, -124.2))

        def __len__(self):
            return len(self.points)

    active_route = Route()
    from datetime import datetime, timezone

    class TrackCollection(list):
        revision = 0

    host = SimpleNamespace(
        controller=SimpleNamespace(
            chart_errors=(),
            core=SimpleNamespace(
                routingManager=SimpleNamespace(get_by_name=lambda name: active_route),
                trackManager=TrackCollection(),
                poiManager=[],
            ),
        ),
        navigation=SimpleNamespace(
            hl_routing="selected route",
            hl_track_item=None,
            hl_poi=None,
            route_overlay_data=lambda _: [],
        ),
        _view_state=ViewportMapState(
            camera=ViewportCameraState(),
            selected_route_point=1,
            route_plan=((48.1, -124.1), (48.2, -124.0)),
            route_preview=((48.3, -124.3), (48.4, -124.2)),
            points=(
                SimpleNamespace(lat=48.0, lon=-124.0, hdg=10, awa=350),
                SimpleNamespace(lat=48.1, lon=-124.1, hdg=123, awa=270),
                SimpleNamespace(lat=48.2, lon=-124.2, hdg=250, awa=45),
            ),
            offsets=(0, 10, 20),
            position=15,
        ),
        _MAX_TRACK_POLYLINE=1500,
        move_callback=None,
        dashboard_visible=True,
        dashboard=SimpleNamespace(overlay_data=lambda: []),
        time_control=SimpleNamespace(time=datetime.now(timezone.utc)),
        ais_visible=False,
        isochrone_frame=SimpleNamespace(isochrones=(), path=(), exclusions=()),
    )

    overlay = ViewportMap.viewport_route_overlay(host)
    for key in ("_pois_key", "_tracks_key", "_race_tracks_key"):
        assert overlay.pop(key) is not None
    assert overlay.pop("_routes_key") is None
    assert overlay == {
        "route_plan": [[48.1, -124.1], [48.2, -124.0]],
        "route_preview": [[48.3, -124.3], [48.4, -124.2]],
        "selected_point": [48.5, -124.2],
        "track_points": [[48.0, -124.0], [48.1, -124.1], [48.2, -124.2]],
        "track_position": [48.1, -124.1],
        "track_heading": 123,
        "track_awa": 270,
        "pois": [],
        "tracks": [],
        "race_tracks": [],
        "routes": [],
        "ais_targets": [],
        "isochrones": {"frontiers": [], "path": [], "exclusions": []},
        "dashboard": [],
        "caption": "",
        "dashboard_visible": True,
    }


def test_log_boat_heading_falls_back_to_course_between_track_fixes():
    from sailworks.qt.viewportmap import (
        ViewportCameraState,
        ViewportMapState,
        ViewportMap,
    )

    from datetime import datetime, timezone

    class TrackCollection(list):
        revision = 0

    host = SimpleNamespace(
        controller=SimpleNamespace(
            chart_errors=(),
            core=SimpleNamespace(
                routingManager=SimpleNamespace(get_by_name=lambda _name: None),
                trackManager=TrackCollection(),
                poiManager=[],
            ),
        ),
        navigation=SimpleNamespace(
            hl_routing="",
            hl_track_item=None,
            hl_poi=None,
            route_overlay_data=lambda _: [],
        ),
        _view_state=ViewportMapState(
            camera=ViewportCameraState(),
            points=(
                SimpleNamespace(lat=48.0, lon=-124.0, hdg=None, awa=90.0),
                SimpleNamespace(lat=48.0, lon=-123.9, hdg=None, awa=90.0),
            ),
            offsets=(0, 10),
            position=0,
        ),
        _MAX_TRACK_POLYLINE=1500,
        move_callback=None,
        dashboard_visible=True,
        dashboard=SimpleNamespace(overlay_data=lambda: []),
        time_control=SimpleNamespace(time=datetime.now(timezone.utc)),
        ais_visible=False,
        isochrone_frame=SimpleNamespace(isochrones=(), path=(), exclusions=()),
    )

    overlay = ViewportMap.viewport_route_overlay(host)
    assert overlay["track_position"] == [48.0, -124.0]
    assert overlay["track_heading"] == pytest.approx(90, abs=0.1)
    assert overlay["track_awa"] == 90


def test_route_overlay_is_drawn_above_weather_in_the_viewport_scene():
    import sailworks.qt.gribviewport as viewport_module

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
    assert "trackHeading" in scene and "trackAwa" in scene
    assert "drawTrackMarker(context)" in scene
    assert "context.rotate(heading * Math.PI / 180)" in scene
    assert "if (!hasFiniteValue(trackHeading))" in scene
    assert "if (hasFiniteValue(trackAwa))" in scene
    assert "if (awa > 180)" in scene
    assert "context.rotate(awa * Math.PI / 180)" in scene


def test_viewport_publishes_retained_navigation_images_without_reupload():
    from PySide6.QtCore import QRectF, QUrl
    from PySide6.QtGui import QImage
    from sailworks.qt.gribviewport import _viewport_widget_class

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
    from sailworks.qt.gribviewport import _viewport_widget_class

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


def test_chart_idle_timer_debounces_camera_changes(presenter):
    import time
    from types import MethodType

    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtTest import QTest

    from sailworks.qt.gribviewport import _viewport_widget_class
    from sailworks.qt.gribviewportchart import CHART_IDLE_MS

    calls = []

    def request(camera, preview=False):
        calls.append((time.monotonic(), camera, preview))
        return len(calls)

    widget = SimpleNamespace(
        _disposed=False,
        _chart_timer=QTimer(),
        _chart_model=SimpleNamespace(request=request),
        _camera="first",
        _weather_ready_at=1,
        _model=SimpleNamespace(busy=lambda: False),
        _chart_interactive_at=None,
        _chart_redelivery_generation=None,
        _chart_state_now=lambda: {},
        counters={"chart_requests": 0},
    )
    cls = _viewport_widget_class()
    for name in ("_restart_chart_settle", "_request_interactive_chart", "_request_chart"):
        setattr(widget, name, MethodType(getattr(cls, name), widget))
    widget._chart_timer.setSingleShot(True)
    widget._chart_timer.setTimerType(Qt.TimerType.PreciseTimer)
    widget._chart_timer.setInterval(CHART_IDLE_MS)
    widget._chart_timer.timeout.connect(widget._request_chart)
    widget._chart_camera_changed = time.perf_counter()
    widget._restart_chart_settle()
    widget._request_interactive_chart()
    QTest.qWait(CHART_IDLE_MS // 2)
    assert len(calls) <= 1
    assert all(preview for _, _, preview in calls)
    changed = time.monotonic()
    widget._camera = "second"
    widget._chart_camera_changed = time.perf_counter()
    widget._restart_chart_settle()
    widget._request_interactive_chart()
    QTest.qWait(CHART_IDLE_MS // 2)
    assert len(calls) <= 1
    assert all(preview for _, _, preview in calls)
    QTest.qWait(CHART_IDLE_MS)
    full = [(when, camera) for when, camera, preview in calls if not preview]
    assert len(full) == 1
    assert full[0][1] == "second"
    assert full[0][0] - changed >= CHART_IDLE_MS / 1000
    QTest.qWait(CHART_IDLE_MS)
    assert len([call for call in calls if not call[2]]) == 1


def test_chart_worker_finishes_active_and_coalesces_latest_camera(tmp_path):
    from PySide6.QtGui import QImage

    started, release, finished = Event(), Event(), Event()
    cameras, cancelled = [], []
    layer = ViewportChartLayer((), tmp_path)

    def compose(camera, cancel):
        cameras.append(camera)
        if camera == "first":
            started.set()
            assert release.wait(5)
            cancelled.append(cancel.is_set())
        else:
            finished.set()
        return QImage(1, 1, QImage.Format.Format_ARGB32), {}

    layer._compose = compose
    try:
        layer.request("first")
        assert started.wait(5)
        layer.request("intermediate")
        layer.request("latest")
        assert not layer._active[2].is_set()
        release.set()
        assert finished.wait(5)
        assert cameras == ["first", "latest"]
        assert cancelled == [False]
    finally:
        release.set()
        layer.close()


def test_chart_weather_busy_rearms_real_timer(presenter):
    import time
    from types import MethodType

    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest

    from sailworks.qt.gribviewport import _viewport_widget_class
    from sailworks.qt.gribviewportchart import CHART_IDLE_MS

    requests = []
    model = SimpleNamespace(busy=Mock(return_value=True))
    widget = SimpleNamespace(
        _disposed=False,
        _chart_timer=QTimer(),
        _chart_model=SimpleNamespace(request=requests.append),
        _camera="camera",
        _weather_ready_at=1,
        _enc_chart_layer=False,
        _model=model,
        _chart_camera_changed=time.perf_counter() - 1,
        _chart_state_now=lambda: {},
        counters={"chart_requests": 0},
    )
    cls = _viewport_widget_class()
    widget._restart_chart_settle = MethodType(cls._restart_chart_settle, widget)
    widget._request_chart = MethodType(cls._request_chart, widget)
    widget._chart_timer.setSingleShot(True)
    widget._chart_timer.setInterval(CHART_IDLE_MS)
    widget._chart_timer.timeout.connect(widget._request_chart)
    widget._request_chart()
    assert widget._chart_timer.isActive()
    assert not requests
    model.busy.return_value = False
    QTest.qWait(CHART_IDLE_MS * 2)
    assert requests == ["camera"]
    assert not widget._chart_timer.isActive()


def test_chart_budget_release_rearms_only_after_refusal(presenter):
    from types import MethodType

    from sailworks.qt.gribviewport import _viewport_widget_class
    from sailworks.qt.viewportpublication import LayerRole, PublicationManager

    manager = PublicationManager()
    widget = SimpleNamespace(
        _publication_manager=manager,
        _chart_budget_refusal=None,
        _restart_chart_settle=Mock(),
    )
    MethodType(_viewport_widget_class()._watch_chart_budget, widget)()
    assert manager.budget.allocate(LayerRole.FILL, 10)
    manager.budget.release(LayerRole.FILL, 1)
    widget._restart_chart_settle.assert_not_called()
    widget._chart_budget_refusal = (1, ())
    manager.budget.release(LayerRole.FILL, 0)
    widget._restart_chart_settle.assert_not_called()
    manager.budget.release(LayerRole.FILL, 1)
    widget._restart_chart_settle.assert_called_once_with()


def test_chart_early_timer_rearms_for_remaining_idle_interval():
    from sailworks.qt.gribviewport import _viewport_widget_class

    widget = SimpleNamespace(
        _chart_model=object(),
        _camera=object(),
        _weather_ready_at=1,
        _chart_camera_changed=99.94,
        _chart_timer=SimpleNamespace(start=Mock()),
    )
    with patch("sailworks.qt.gribviewport.time.perf_counter", return_value=100.0):
        _viewport_widget_class()._request_chart(widget)
    remaining = widget._chart_timer.start.call_args.args[0]
    assert 40 <= remaining <= 41
