"""Native layer binding policy without starting a scene graph."""

from types import SimpleNamespace

import pytest

from sailworks.qt.gribviewport import _viewport_widget_class
from test.test_native_weather_store_gate import load


@pytest.mark.parametrize(
    "environment,override,expected",
    [
        (None, None, True),
        ("1", None, True),
        ("0", None, False),
        ("1", False, False),
        ("0", True, True),
    ],
)
def test_native_default_and_explicit_recovery(
    monkeypatch, environment, override, expected
):
    from unittest.mock import patch
    from sailworks.qt import controller, gpuweathertextures

    if environment is None:
        monkeypatch.delenv("GWR_NATIVE_WEATHER_FIELDS", raising=False)
    else:
        monkeypatch.setenv("GWR_NATIVE_WEATHER_FIELDS", environment)
    assert gpuweathertextures.native_weather_fields_enabled(override) is expected

    class StopBeforeCore(Exception):
        pass

    with (
        patch.object(controller, "Core", side_effect=StopBeforeCore),
        patch.object(gpuweathertextures, "NativeWeatherBudget") as budget,
    ):
        with pytest.raises(StopBeforeCore):
            if override is None:
                controller.SessionController()
            else:
                controller.SessionController(native_weather_fields=override)
        assert budget.call_count == int(expected)


@pytest.mark.parametrize("entrypoint", ["application", "viewport"])
@pytest.mark.parametrize(
    "arguments,expected",
    [
        ([], None),
        (["--no-native-weather-fields"], False),
        (["--native-weather-fields"], True),
    ],
)
def test_entrypoints_default_native_and_offer_recovery(
    monkeypatch, entrypoint, arguments, expected
):
    import argparse
    import inspect
    from sailworks.qt import __main__ as launcher, gribviewport

    parse_args = argparse.ArgumentParser.parse_args
    captured = []

    class StopBeforeGui(Exception):
        pass

    def capture(parser, argv=None, namespace=None):
        parsed = parse_args(parser, argv, namespace)
        captured.append(parsed.native_weather_fields)
        raise StopBeforeGui

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(StopBeforeGui):
        (launcher if entrypoint == "application" else gribviewport).main(arguments)
    assert captured == [expected]
    assert (
        inspect.signature(_viewport_widget_class().__init__)
        .parameters["native_weather_fields"]
        .default
        is None
    )


class Layer:
    def __init__(self):
        self.values = {}

    def setProperty(self, name, value):
        self.values[name] = value

    def setVisible(self, visible):
        self.values["visible"] = visible

    def setOpacity(self, opacity):
        self.values["opacity"] = opacity

    def property(self, name):
        return self.values.get(name)


class Context:
    ready = False

    def __init__(self):
        self.requests = []

    def request(self, identity, images, representation):
        self.requests.append((identity, images, representation))
        return self.ready


def test_native_binding_reuses_current_ready_identity_and_separates_layers():
    cls = _viewport_widget_class()
    contexts = {(role, 0): Context() for role in ("barbs", "fill")}
    host = SimpleNamespace(
        _barbs=Layer(),
        _barbs_back=None,
        _fill=Layer(),
        _fill_back=None,
        _native_front_index=0,
        _native_contexts=contexts,
        _native_bindings={},
        counters={"native_field_requests": 0},
    )
    host._native_item = lambda role, slot: cls._native_item(host, role, slot)
    host._reset_barb_zoom_preview_item = cls._reset_barb_zoom_preview_item
    cls._bind_native_layer(host, "barbs", "A", (1, 2, 3, 4))
    assert host._barbs.values == {"nativeFrameObserved": False, "nativeReady": False}
    contexts["barbs", 0].ready = True
    cls._bind_native_layer(host, "barbs", "A", (1, 2, 3, 4))
    assert len(contexts["barbs", 0].requests) == 1
    cls._bind_native_layer(host, "fill", "A", (5, 6, 7, 8))
    assert contexts["fill", 0].requests == [("A", (5, 6, 7, 8), "grid")]
    assert contexts["barbs", 0].requests == [("A", (1, 2, 3, 4), "nodes")]
    assert host.counters["native_field_requests"] == 2


def test_unready_same_identity_is_retried_and_readiness_reset():
    cls = _viewport_widget_class()
    context = Context()
    host = SimpleNamespace(
        _barbs=Layer(),
        _barbs_back=None,
        _fill=None,
        _fill_back=None,
        _native_front_index=0,
        _native_contexts={("barbs", 0): context},
        _native_bindings={("barbs", 0): "A"},
        counters={"native_field_requests": 0},
    )
    host._native_item = lambda role, slot: cls._native_item(host, role, slot)
    cls._bind_native_layer(host, "barbs", "A", (1, 2, 3, 4))
    assert len(context.requests) == 1
    context.ready = True
    cls._bind_native_layer(host, "barbs", "B", (5, 6, 7, 8))
    assert host._barbs.values["nativeFrameObserved"] is False
    assert host._native_bindings["barbs", 0] == "B"


def test_pending_native_binding_does_not_mutate_visible_layer():
    cls = _viewport_widget_class()
    front, back = Layer(), Layer()
    host = SimpleNamespace(
        _barbs=front,
        _barbs_back=back,
        _fill=None,
        _fill_back=None,
        _native_front_index=0,
        _native_contexts={("barbs", 0): Context(), ("barbs", 1): Context()},
        _native_bindings={},
        counters={"native_field_requests": 0},
    )
    host._native_item = lambda role, slot: cls._native_item(host, role, slot)
    cls._bind_native_layer(host, "barbs", "later", (1, 2, 3, 4), pending=True)
    assert front.values == {}
    assert back.values == {"nativeFrameObserved": False, "nativeReady": False}
    assert host._native_bindings == {("barbs", 1): "later"}


def test_native_status_correlates_ready_and_invalidation_by_slot(monkeypatch):
    import shiboken6
    from sailworks.qt.viewportpublication import (
        LayerPlacement,
        PublicationManager,
    )

    monkeypatch.setattr(shiboken6, "isValid", lambda item: True)
    cls = _viewport_widget_class()
    manager = PublicationManager()
    old, new = object(), object()
    host = SimpleNamespace(
        _disposed=False,
        _barbs=Layer(),
        _barbs_back=Layer(),
        _fill=None,
        _fill_back=None,
        _native_front_index=0,
        _native_bindings={("barbs", 0): "old", ("barbs", 1): "new"},
        _native_publication_tokens={("barbs", 0): old, ("barbs", 1): new},
        _publication_manager=manager,
        _camera=None,
        _set_weather_visible=lambda visible: None,
        _update_native_state=lambda: None,
    )
    host._native_item = lambda role, slot: cls._native_item(host, role, slot)
    host._clear_barb_zoom_preview = lambda: cls._clear_barb_zoom_preview(host)
    assert manager.stage_resources("barbs", "new", LayerPlacement(), {}, 10, new)
    cls._native_status(
        host, "barbs", 0, {"key": SimpleNamespace(identity="old"), "ready": True}
    )
    assert manager.layers["barbs"].pending == "new"
    assert manager.layers["barbs"].visible is None
    cls._native_status(host, "barbs", 0, {"invalidated": True})
    assert manager.layers["barbs"].pending == "new"
    assert ("barbs", 0) not in host._native_publication_tokens
    cls._native_status(
        host, "barbs", 1, {"key": SimpleNamespace(identity="new"), "ready": True}
    )
    assert host._barbs_back.values["nativeReady"] is True
    assert manager.layers["barbs"].visible is None
    assert manager.layers["barbs"].pending == "new"


@pytest.mark.parametrize("token_kind", ["matching", "older", "absent"])
def test_native_error_retires_only_matching_pending_resources(monkeypatch, token_kind):
    import shiboken6
    from sailworks.qt.viewportpublication import (
        LayerPlacement,
        PublicationManager,
    )

    monkeypatch.setattr(shiboken6, "isValid", lambda item: True)
    cls = _viewport_widget_class()
    manager = PublicationManager()
    visible_resources = {"visible": object()}
    assert manager.stage_resources(
        "barbs", "visible", LayerPlacement(), visible_resources, 5
    )
    assert manager.commit_pending("barbs") == (True, "adopted")
    token, older_token = object(), object()
    assert manager.stage_resources("barbs", "new", LayerPlacement(), {}, 10, token)
    key = SimpleNamespace(identity="A")
    slot = ("barbs", 1)
    events = []
    back = Layer()
    back.values.update(nativeReady=True, nativeFrameObserved=True)
    host = SimpleNamespace(
        _disposed=False,
        _barbs=Layer(),
        _barbs_back=back,
        _fill=None,
        _fill_back=None,
        _native_front_index=0,
        _native_bindings={slot: "A"},
        _native_requests={slot: (2, key)},
        _native_images={slot: (object(),)},
        _native_publication_tokens=(
            {}
            if token_kind == "absent"
            else {slot: token if token_kind == "matching" else older_token}
        ),
        _publication_manager=manager,
        _show_state=lambda state: events.append(state),
        _set_weather_visible=lambda visible: events.append(visible),
        nativeError=SimpleNamespace(emit=lambda error: events.append(error)),
    )
    host._native_item = lambda role, index: cls._native_item(host, role, index)
    cls._native_status(host, "barbs", 1, {"sequence": 1, "key": key, "error": "old"})
    assert manager.layers["barbs"].pending == "new"
    assert host._native_requests[slot] == (2, key)
    assert events == []

    cls._native_status(host, "barbs", 1, {"sequence": 2, "key": key, "error": "failed"})
    layer = manager.layers["barbs"]
    assert layer.visible == "visible"
    assert layer.visible_resources is visible_resources
    assert layer.native_ready and layer.native_frame_observed
    assert layer.pending == (None if token_kind == "matching" else "new")
    assert manager.budget.total_used() == (5 if token_kind == "matching" else 15)
    assert slot not in host._native_bindings
    assert slot not in host._native_requests
    assert slot not in host._native_images
    assert slot not in host._native_publication_tokens
    assert back.values == {"nativeReady": False, "nativeFrameObserved": False}
    assert events == ["unavailable", "failed"]


def test_same_identity_changed_images_and_stale_status(monkeypatch):
    from threading import RLock
    import shiboken6
    from sailworks.qt.viewportpublication import (
        LayerPlacement,
        PublicationManager,
    )

    monkeypatch.setattr(shiboken6, "isValid", lambda item: True)
    cls = _viewport_widget_class()

    class SequencedContext(Context):
        def __init__(self):
            super().__init__()
            self._lock = RLock()
            self._latest = None

        def request(self, identity, images, representation):
            super().request(identity, images, representation)
            self._latest = (len(self.requests), SimpleNamespace(identity=identity))
            return False

    context = SequencedContext()
    manager = PublicationManager()
    token = object()
    host = SimpleNamespace(
        _disposed=False,
        _barbs=Layer(),
        _barbs_back=Layer(),
        _fill=None,
        _fill_back=None,
        _native_front_index=0,
        _native_contexts={("barbs", 0): context},
        _native_bindings={},
        _native_publication_tokens={},
        _publication_manager=manager,
        counters={"native_field_requests": 0},
        _camera=None,
        _native_pending_camera=None,
        _native_active_camera=None,
        _native_active_dpr=None,
        _native_active_selection=None,
        _native_pending_selection=None,
        _native_pending_dpr=1.0,
        _native_pending_selection_urls=(),
        _native_pending_identity="A",
        devicePixelRatio=lambda: 1.0,
        _set_weather_visible=lambda visible: None,
        _update_native_state=lambda: None,
    )
    host._native_item = lambda role, slot: cls._native_item(host, role, slot)
    host._native_publication_dpr = lambda: cls._native_publication_dpr(host)
    host._selection_is_displayed = lambda field, selection: (
        cls._selection_is_displayed(host, field, selection)
    )
    images = (object(),)
    cls._bind_native_layer(host, "barbs", "A", images, completion_token=token)
    old_sequence, old_key = context._latest
    context.ready = True
    assert manager.stage_resources("barbs", "new", LayerPlacement(), {}, 10, token)
    cls._bind_native_layer(host, "barbs", "A", (object(),), completion_token=token)
    assert len(context.requests) == 2
    cls._native_status(
        host, "barbs", 0, {"sequence": old_sequence, "key": old_key, "ready": True}
    )
    cls._native_status(
        host, "barbs", 0, {"invalidated": True, "sequence": old_sequence}
    )
    assert host._native_bindings["barbs", 0] == "A"
    assert host._barbs.values["nativeReady"] is False
    assert manager.layers["barbs"].pending == "new"
    assert manager.layers["barbs"].visible is None
    sequence, key = context._latest
    cls._native_status(
        host, "barbs", 0, {"sequence": sequence, "key": key, "ready": True}
    )
    assert manager.layers["barbs"].visible == "new"


def test_warm_back_binding_adopts_only_on_swap():
    from sailworks.qt.viewportpublication import (
        LayerPlacement,
        PublicationManager,
    )

    cls = _viewport_widget_class()
    context = Context()
    context.ready = True
    manager = PublicationManager()
    token = object()
    assert manager.stage_resources("barbs", "new", LayerPlacement(), {}, 10, token)
    host = SimpleNamespace(
        _barbs=Layer(),
        _barbs_back=Layer(),
        _fill=None,
        _fill_back=None,
        _native_front_index=0,
        _native_contexts={("barbs", 1): context},
        _native_bindings={},
        _native_publication_tokens={},
        _publication_manager=manager,
        counters={"native_field_requests": 0},
        _wind_opacity=1,
        _native_pending_camera="camera",
        _native_pending_identity="new",
        _native_pending_selection=None,
        _native_pending_selection_urls=(),
        _provider=SimpleNamespace(pin_selection=lambda urls, retained=(): None),
    )
    host._native_item = lambda role, slot: cls._native_item(host, role, slot)
    host._adopt_shared_anchor_front = lambda: cls._adopt_shared_anchor_front(host)
    host._clear_barb_zoom_preview = lambda: cls._clear_barb_zoom_preview(host)
    images = (object(),)
    cls._bind_native_layer(
        host, "barbs", "A", images, pending=True, completion_token=token
    )
    cls._bind_native_layer(
        host, "barbs", "A", images, pending=True, completion_token=token
    )
    assert len(context.requests) == 1
    assert host._barbs_back.values["nativeReady"] is True
    assert manager.layers["barbs"].visible is None
    assert manager.layers["barbs"].pending == "new"
    cls._commit_native_weather(host)
    assert manager.layers["barbs"].visible == "new"


def test_native_barbs_budget_refusal_still_binds():
    from unittest.mock import Mock
    from sailworks.qt.viewportpublication import (
        LayerPlacement,
        PublicationManager,
    )

    cls = _viewport_widget_class()
    manager = PublicationManager()
    assert manager.stage_resources("barbs", "visible", LayerPlacement(), {}, 5)
    assert manager.commit_pending("barbs") == (True, "adopted")
    token = object()
    assert manager.stage_resources("barbs", "old", LayerPlacement(), {}, 10, token)
    manager.stage_resources = Mock(return_value=False)
    bind = Mock()
    host = SimpleNamespace(
        _barbs=Layer(),
        _native_weather_fields=True,
        _native_pending_camera=None,
        _publication_manager=manager,
        _bind_native_layer=bind,
        _native_pending_selection=None,
        _native_pending_selection_urls=(),
        _barb_anchor_offset=lambda camera, selection=None: (0.0, 0.0),
        counters={"barbs_selection_publications": 0, "barbs_selection_uploads": 0},
        _wind_opacity=1.0,
        _reset_barb_zoom_preview_item=cls._reset_barb_zoom_preview_item,
    )
    host._shared_anchor_pool = lambda: cls._shared_anchor_pool(host)
    field = SimpleNamespace(
        images=(1, 2),
        generation=1,
        identity="field",
        staged=SimpleNamespace(weight=lambda _: 0),
    )
    selection = SimpleNamespace(shape=(1, 1))
    camera = SimpleNamespace(stamp=lambda: 0)
    cls._publish_barbs(host, field, selection, (), (), camera)
    bind.assert_called_once_with(
        "barbs", "field", (1, 2), pending=False, completion_token=None
    )
    assert manager.layers["barbs"].visible == "visible"
    assert manager.layers["barbs"].pending is None
    assert manager.layers["barbs"].completion_token is None
    assert manager.budget.current_usage["barbs"] == 5
    assert manager.commit_pending("barbs", token) == (False, "no pending resources")


def test_native_weather_swap_keeps_front_until_pending_layers_are_ready():
    from sailworks.qt.viewportpublication import PublicationManager

    cls = _viewport_widget_class()
    front, back = Layer(), Layer()
    front_fill, back_fill = Layer(), Layer()
    contexts = {
        (role, index): SimpleNamespace(clear=lambda: None)
        for role in ("barbs", "fill")
        for index in (0, 1)
    }
    pinned = []
    host = SimpleNamespace(
        _barbs=front,
        _barbs_back=back,
        _fill=front_fill,
        _fill_back=back_fill,
        _native_front_index=0,
        _native_pending_camera="new time",
        _native_active_camera="old time",
        _native_pending_identity="new field",
        _native_active_identity="old field",
        _native_pending_selection_urls=("image://weather/new",),
        _native_pending_selection=None,
        _native_publishing=False,
        _weather_display_camera="old time",
        _native_contexts=contexts,
        _provider=SimpleNamespace(
            pin_selection=lambda urls, retained=(): pinned.append((urls, retained))
        ),
        _publication_manager=PublicationManager(),
        _wind_opacity=0.88,
        _wind_style="barbs",
        _wind_gradient_enabled=True,
        _wind_gradient_opacity=0.12,
        _disposed=False,
        _camera="new time",
        _model=SimpleNamespace(state="ready"),
        _state="loading",
    )
    host._commit_native_weather = lambda: cls._commit_native_weather(host)
    host._adopt_shared_anchor_front = lambda: cls._adopt_shared_anchor_front(host)
    host._clear_barb_zoom_preview = lambda: cls._clear_barb_zoom_preview(host)
    host._reset_barb_zoom_preview_item = cls._reset_barb_zoom_preview_item
    host._show_state = lambda state: setattr(host, "_state", state)
    front.values["ready"] = front_fill.values["ready"] = True
    # The pending frame republished its anchors; the superseded front's
    # anchors belong to the previous frame.
    front.values["anchorFrameValid"] = False
    back.values["anchorFrameValid"] = True
    back.values["prepared"] = True
    back_fill.values["prepared"] = False
    cls._update_native_state(host)
    assert host._barbs is front and host._fill is front_fill
    assert host._native_active_camera == "old time"
    back_fill.values["prepared"] = True
    cls._update_native_state(host)
    assert host._barbs is back and host._fill is back_fill
    assert front.values["visible"] is False
    assert front_fill.values["visible"] is False
    assert back.values["visible"] is True
    assert back_fill.values["visible"] is True
    assert host._native_active_camera == host._weather_display_camera == "new time"
    assert host._state == "loading"
    # The committed frame is the valid one; the retired front stays invalid
    # until its slot is republished for a later frame.
    assert back.values["anchorFrameValid"] is True
    assert front.values["anchorFrameValid"] is False
    assert pinned == [(("image://weather/new",), ())]


def test_translation_only_selection_needs_no_native_anchor_reload():
    import numpy as np
    from datetime import datetime, timezone

    from sailworks.qt.gpuweatherfields import BarbSelection
    from sailworks.qt.gribviewport import ViewportCamera

    cls = _viewport_widget_class()
    ids = np.array([[0, 1, -1], [2, 3, 0]], dtype=np.int32)
    displayed = BarbSelection("g", ids, (), 5, None)
    moved = BarbSelection("g", ids.copy(), (), 5, None)
    assert moved.matches(displayed)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    published = ViewportCamera(48.3, -123.0, 9, 900, 700, stamp)
    panned = ViewportCamera(48.3, -123.0005, 9, 900, 700, stamp)

    class ForbiddenProvider:
        def publish_selection(self, *args):
            raise AssertionError("a covered pan reloaded the selection")

    front, back = Layer(), Layer()
    host = SimpleNamespace(
        _native_weather_fields=True,
        _native_pending_camera=None,
        _native_active_camera=published,
        _native_active_identity="field",
        _native_active_selection=displayed,
        _native_active_dpr=1.0,
        _native_pending_dpr=None,
        _weather_display_camera=published,
        _provider=ForbiddenProvider(),
        _barbs=front,
        _barbs_back=back,
        devicePixelRatio=lambda: 1.0,
    )
    host._selection_is_displayed = lambda field, selection: (
        cls._selection_is_displayed(host, field, selection)
    )
    host._native_publication_dpr = lambda: cls._native_publication_dpr(host)
    host._native_retained_view_is_current = (
        lambda camera, dpr=None: cls._native_retained_view_is_current(host, camera, dpr)
    )
    front_anchor_sources = dict(front.values)
    back_anchor_sources = dict(back.values)
    field = SimpleNamespace(generation="g", node_count=4, identity="field")
    assert cls._publish(host, field, moved, panned) is True
    # The displayed camera tracks the pan without touching any anchor source.
    assert host._native_active_camera is panned
    assert host._weather_display_camera is panned
    assert front.values == front_anchor_sources
    assert back.values == back_anchor_sources


def test_changed_forecast_time_republishes_even_for_same_selection():
    import numpy as np
    from datetime import datetime, timedelta, timezone

    from sailworks.qt.gpuweatherfields import BarbSelection
    from sailworks.qt.gribviewport import ViewportCamera

    cls = _viewport_widget_class()
    ids = np.array([[0, 1, -1], [2, 3, 0]], dtype=np.int32)
    displayed = BarbSelection("g", ids, (), 5, None)
    same_symbols = BarbSelection("g", ids.copy(), (), 5, None)
    # A forecast-time step inside one resident window keeps every node id
    # while changing the sampled values and blend weights.
    assert same_symbols.matches(displayed)

    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    published = ViewportCamera(48.3, -123.0, 9, 900, 700, first)
    later = ViewportCamera(48.3, -123.0, 9, 900, 700, first + timedelta(hours=1))

    front, back = Layer(), Layer()
    fill_calls, barbs_calls, published_selections = [], [], []

    def publish_selection(images, generation):
        published_selections.append((images, generation))
        return ("node", "x", "y")

    host = SimpleNamespace(
        _native_weather_fields=True,
        _native_pending_camera=None,
        _native_active_camera=published,
        _native_active_identity="field",
        _native_active_selection=displayed,
        _native_active_dpr=1.0,
        _native_pending_dpr=None,
        _native_pending_identity=None,
        _native_pending_selection=None,
        _weather_display_camera=published,
        _barbs=front,
        _barbs_back=back,
        _fill=None,
        _fill_back=None,
        _wind_gradient_enabled=False,
        _native_budget=SimpleNamespace(byte_limit=1024),
        _provider=SimpleNamespace(publish_selection=publish_selection),
        _publish_fill=lambda field, field_urls, camera: fill_calls.append(camera),
        _publish_barbs=lambda *args: barbs_calls.append(args),
        _published=None,
        _published_generations=set(),
        devicePixelRatio=lambda: 1.0,
    )
    host._selection_is_displayed = lambda field, selection: (
        cls._selection_is_displayed(host, field, selection)
    )
    host._native_publication_dpr = lambda: cls._native_publication_dpr(host)
    host._native_retained_view_is_current = (
        lambda camera, dpr=None: cls._native_retained_view_is_current(host, camera, dpr)
    )
    field = SimpleNamespace(
        generation="g",
        node_count=4,
        identity="field",
        texture_bytes=0,
        grid=None,
        images=(),
    )
    assert cls._publish(host, field, same_symbols, later) is not False
    # The new forecast time must go through the blend and both layers.
    assert fill_calls == [later]
    assert barbs_calls and barbs_calls[0][4] is later
    assert host._native_pending_camera is later
    assert host._native_active_camera is published
    assert published_selections and published_selections[0][1] == "g"


def test_device_pixel_ratio_change_republishes_instead_of_reusing_anchors():
    import numpy as np
    from datetime import datetime, timezone

    from sailworks.qt.gpuweatherfields import BarbSelection
    from sailworks.qt.gribviewport import ViewportCamera

    cls = _viewport_widget_class()
    ids = np.array([[0, 1]], dtype=np.int32)
    displayed = BarbSelection("g", ids, (), 2, None)
    same_symbols = BarbSelection("g", ids.copy(), (), 2, None)
    assert same_symbols.matches(displayed)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    camera = ViewportCamera(48.3, -123.0, 9, 900, 700, stamp)

    front, back = Layer(), Layer()
    fill_calls, barbs_calls = [], []
    host = SimpleNamespace(
        _native_weather_fields=True,
        _native_pending_camera=None,
        _native_active_camera=camera,
        _native_active_identity="field",
        _native_active_selection=displayed,
        # The retained resources were composed at ratio 1.0; the display has
        # since moved to 2.0, so the anchors cannot be reused as they are.
        _native_active_dpr=1.0,
        _native_pending_dpr=None,
        _native_pending_identity=None,
        _native_pending_selection=None,
        _weather_display_camera=camera,
        _barbs=front,
        _barbs_back=back,
        _fill=None,
        _fill_back=None,
        _wind_gradient_enabled=False,
        _native_budget=SimpleNamespace(byte_limit=1024),
        _provider=SimpleNamespace(publish_selection=lambda images, generation: None),
        _publish_fill=lambda field, field_urls, active: fill_calls.append(active),
        _publish_barbs=lambda *args: barbs_calls.append(args),
        _published=None,
        _published_generations=set(),
        devicePixelRatio=lambda: 2.0,
    )
    host._selection_is_displayed = lambda field, selection: (
        cls._selection_is_displayed(host, field, selection)
    )
    host._native_publication_dpr = lambda: cls._native_publication_dpr(host)
    host._native_retained_view_is_current = (
        lambda camera, dpr=None: cls._native_retained_view_is_current(host, camera, dpr)
    )
    field = SimpleNamespace(
        generation="g",
        node_count=4,
        identity="field",
        texture_bytes=0,
        grid=None,
        images=(),
    )
    assert cls._publish(host, field, same_symbols, camera) is not False
    assert fill_calls == [camera]
    assert barbs_calls
    assert host._native_pending_dpr == 2.0


def test_changed_selection_still_publishes_to_the_pending_slot():
    import numpy as np
    from datetime import datetime, timezone

    from sailworks.qt.gpuweatherfields import BarbSelection
    from sailworks.qt.gribviewport import ViewportCamera

    cls = _viewport_widget_class()
    displayed = BarbSelection("g", np.array([[0, 1]], dtype=np.int32), (), 2, None)
    changed = BarbSelection("g", np.array([[0, 2]], dtype=np.int32), (), 2, None)
    assert not changed.matches(displayed)
    front, back = Layer(), Layer()
    published = []
    barbs_calls = []

    def publish(images, generation):
        published.append((images, generation))
        return ("node", "x", "y")

    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    active = ViewportCamera(48.3, -123.0, 9, 900, 700, stamp)
    camera = ViewportCamera(48.3, -123.0005, 9, 900, 700, stamp)
    host = SimpleNamespace(
        _native_weather_fields=True,
        _native_pending_camera=None,
        _native_active_camera=active,
        _native_active_identity="field",
        _native_active_selection=displayed,
        _native_active_dpr=1.0,
        _native_pending_dpr=None,
        _native_pending_identity=None,
        _native_pending_selection=None,
        _weather_display_camera=active,
        _barbs=front,
        _barbs_back=back,
        _fill=None,
        _fill_back=None,
        _wind_gradient_enabled=False,
        _native_budget=SimpleNamespace(byte_limit=1024),
        _provider=SimpleNamespace(publish_selection=publish),
        _publish_fill=lambda field, field_urls, camera: None,
        _publish_barbs=lambda *args: barbs_calls.append(args),
        _published=None,
        _published_generations=set(),
        devicePixelRatio=lambda: 1.0,
    )
    host._selection_is_displayed = lambda field, selection: (
        cls._selection_is_displayed(host, field, selection)
    )
    host._native_publication_dpr = lambda: cls._native_publication_dpr(host)
    host._native_retained_view_is_current = (
        lambda camera, dpr=None: cls._native_retained_view_is_current(host, camera, dpr)
    )
    field = SimpleNamespace(
        generation="g",
        node_count=4,
        identity="field",
        texture_bytes=0,
        grid=None,
        images=(),
    )
    assert cls._publish(host, field, changed, camera) is not False
    assert host._native_pending_camera is camera
    assert host._native_pending_selection is changed
    assert host._native_pending_dpr == 1.0
    assert back.values["visible"] is True
    assert published and published[0][1] == "g"
    assert barbs_calls


def test_rebase_shifts_front_and_pending_anchor_offsets():
    cls = _viewport_widget_class()
    front, back = Layer(), Layer()
    host = SimpleNamespace(
        _barbs=front,
        _barbs_back=back,
        _base=None,
        _environment=None,
        _fill=None,
        _fill_back=None,
    )
    cls._rebase_retained_imagery(host, 4.5, -3.25)
    for item in (front, back):
        assert item.property("anchorOffsetX") == 4.5
        assert item.property("anchorOffsetY") == -3.25
    # A later rebase accumulates, so the layer stays on its anchors.
    cls._rebase_retained_imagery(host, -1.0, 2.0)
    for item in (front, back):
        assert item.property("anchorOffsetX") == 3.5
        assert item.property("anchorOffsetY") == -1.25


def test_barb_anchor_offset_inverts_a_pan_and_rejects_a_new_frame():
    from datetime import datetime, timezone

    from sailworks.qt.gribviewport import ViewportCamera
    from sailworks.qt.viewporttransform import PanReference

    cls = _viewport_widget_class()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    placed = ViewportCamera(48.3, -123.0, 9, 900, 700, stamp)
    panned = ViewportCamera(48.3, -123.001, 9, 900, 700, stamp)
    reference = PanReference.from_camera(placed)
    dx, dy = reference.translation_to(panned, reference.dpr)
    assert (dx, dy) != (0.0, 0.0)
    host = SimpleNamespace(_map_pan_reference=reference)
    assert cls._barb_anchor_offset(host, panned) == (-dx, -dy)
    # A zoom or resize is a new frame: the retained anchors cannot be placed,
    # so the caller must refuse them instead of guessing an offset.
    zoomed = ViewportCamera(48.3, -123.001, 10, 900, 700, stamp)
    assert cls._barb_anchor_offset(host, zoomed) is None
    resized = ViewportCamera(48.3, -123.001, 9, 901, 700, stamp)
    assert cls._barb_anchor_offset(host, resized) is None
    assert cls._barb_anchor_offset(
        SimpleNamespace(_map_pan_reference=None), panned
    ) == (0.0, 0.0)


def test_invalid_anchor_frame_suppresses_the_native_barbs_layer():
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from sailworks.qt.gribviewport import ViewportCamera
    from sailworks.qt.viewportpublication import PublicationManager
    from sailworks.qt.viewporttransform import PanReference

    cls = _viewport_widget_class()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    placed = ViewportCamera(48.3, -123.0, 9, 900, 700, stamp)
    reference = PanReference.from_camera(placed)
    item = Layer()
    host = SimpleNamespace(
        _barbs=item,
        _native_weather_fields=True,
        _native_pending_camera=None,
        _publication_manager=PublicationManager(),
        _bind_native_layer=Mock(),
        _native_pending_selection=None,
        _native_pending_selection_urls=(),
        _map_pan_reference=reference,
        counters={"barbs_selection_publications": 0, "barbs_selection_uploads": 0},
        _wind_opacity=1.0,
    )
    host._barb_anchor_offset = lambda camera, selection=None: (
        cls._barb_anchor_offset(host, camera, selection)
    )
    host._shared_anchor_pool = lambda: cls._shared_anchor_pool(host)
    host._reset_barb_zoom_preview_item = cls._reset_barb_zoom_preview_item
    field = SimpleNamespace(
        images=(1, 2),
        generation=1,
        identity="field",
        staged=SimpleNamespace(weight=lambda _: 0),
    )
    anchor = (placed.center_lat, placed.center_lon, 9, 900, 700)
    selection = SimpleNamespace(shape=(1, 1), anchor_reference=anchor)
    # A rebase or zoom that overtakes the staged publication marks the frame
    # invalid, so the scene keeps the layer hidden at an unknown offset.
    for camera in (
        ViewportCamera(48.3, -123.0, 10, 900, 700, stamp),
        ViewportCamera(48.3, -123.0, 9, 901, 700, stamp),
    ):
        item.values.clear()
        cls._publish_barbs(host, field, selection, (), (), camera)
        assert item.values["anchorFrameValid"] is False
        assert "anchorOffsetX" not in item.values
        assert "anchorOffsetY" not in item.values
    # A covered pan inside the anchor frame remains displayable.
    item.values.clear()
    cls._publish_barbs(
        host, field, selection, (), (), ViewportCamera(48.3, -123.0, 9, 900, 700, stamp)
    )
    assert item.values["anchorFrameValid"] is True
    assert item.values["anchorOffsetX"] == 0.0
    assert item.values["anchorOffsetY"] == 0.0


def test_camera_handoff_retires_barb_anchors_only_for_a_new_frame():
    from datetime import datetime, timezone

    from sailworks.qt.gribviewport import ViewportCamera
    from sailworks.qt.viewporttransform import PanReference

    cls = _viewport_widget_class()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    placed = ViewportCamera(48.3, -123.0, 9, 900, 700, stamp)
    reference = PanReference.from_camera(placed)

    def host(dpr=1.0):
        front, back = Layer(), Layer()
        front.values["anchorFrameValid"] = True
        back.values["anchorFrameValid"] = True
        state = SimpleNamespace(
            _barbs=front,
            _barbs_back=back,
            _native_active_camera=placed,
            _native_active_dpr=1.0,
            _native_pending_camera=None,
            _native_pending_dpr=None,
            _map_pan_reference=reference,
            devicePixelRatio=lambda: dpr,
        )
        state._native_publication_dpr = lambda: cls._native_publication_dpr(state)
        state._barb_anchor_frame_places = cls._barb_anchor_frame_places
        state._set_barb_anchor_frame_valid = cls._set_barb_anchor_frame_valid
        state._barb_zoom_preview_holds = (
            lambda camera, dpr=None: cls._barb_zoom_preview_holds(state, camera, dpr)
        )
        return state, front, back

    # A covered pan inside one reference can still be placed.
    state, front, back = host()
    panned = ViewportCamera(48.3, -123.0005, 9, 900, 700, stamp)
    assert cls._retire_barb_anchor_frame(state, placed, panned, 1.0) is False
    assert front.values["anchorFrameValid"] is True
    assert back.values["anchorFrameValid"] is True

    # A rebase is still a covered pan; the caller compensates the offset.
    state, front, _back = host()
    rebased = ViewportCamera(48.3, -124.0, 9, 900, 700, stamp)
    assert cls._retire_barb_anchor_frame(state, placed, rebased, 1.0) is False
    assert front.values["anchorFrameValid"] is True

    # A zoom, a resize and a ratio change are new frames: the old anchors
    # must not be presented as current while a replacement prepares.
    for camera, dpr in (
        (ViewportCamera(48.3, -123.0, 10, 900, 700, stamp), 1.0),
        (ViewportCamera(48.3, -123.0, 9, 901, 700, stamp), 1.0),
        (ViewportCamera(48.3, -123.0, 9, 900, 700, stamp), 2.0),
    ):
        state, front, _back = host(dpr)
        assert cls._retire_barb_anchor_frame(state, placed, camera, 1.0) is True
        assert front.values["anchorFrameValid"] is False
        # A retired frame publishes no offset, so the scene cannot place it
        # or report it ready.
        assert "anchorOffsetX" not in front.values

    # Non-chart scenes keep no map reference; the camera frame still decides.
    state, front, _back = host()
    state._map_pan_reference = None
    assert (
        cls._retire_barb_anchor_frame(
            state,
            placed,
            ViewportCamera(48.3, -123.0, 10, 900, 700, stamp),
            1.0,
        )
        is True
    )
    assert front.values["anchorFrameValid"] is False
    state, front, _back = host()
    state._map_pan_reference = None
    assert cls._retire_barb_anchor_frame(state, placed, panned, 1.0) is False
    assert front.values["anchorFrameValid"] is True


def test_camera_handoff_retires_only_an_incompatible_pending_back():
    from datetime import datetime, timezone

    from sailworks.qt.gribviewport import ViewportCamera

    cls = _viewport_widget_class()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    placed = ViewportCamera(48.3, -123.0, 9, 900, 700, stamp)
    zoomed = ViewportCamera(48.3, -123.0, 10, 900, 700, stamp)

    def host(pending_camera, pending_dpr=1.0, dpr=1.0):
        front, back = Layer(), Layer()
        front.values["anchorFrameValid"] = True
        back.values["anchorFrameValid"] = True
        state = SimpleNamespace(
            _barbs=front,
            _barbs_back=back,
            _native_active_camera=placed,
            _native_active_dpr=1.0,
            _native_pending_camera=pending_camera,
            _native_pending_dpr=pending_dpr,
            _map_pan_reference=None,
            devicePixelRatio=lambda: dpr,
        )
        state._native_publication_dpr = lambda: cls._native_publication_dpr(state)
        state._barb_anchor_frame_places = cls._barb_anchor_frame_places
        state._set_barb_anchor_frame_valid = cls._set_barb_anchor_frame_valid
        state._barb_zoom_preview_holds = (
            lambda camera, dpr=None: cls._barb_zoom_preview_holds(state, camera, dpr)
        )
        return state, front, back

    # A pending frame staged for another view is not the incoming one.
    state, front, back = host(zoomed)
    further = ViewportCamera(48.3, -123.0, 11, 900, 700, stamp)
    assert cls._retire_barb_anchor_frame(state, placed, further, 1.0) is True
    assert front.values["anchorFrameValid"] is False
    assert back.values["anchorFrameValid"] is False

    # A pending frame that already matches the incoming camera stays staged.
    state, front, back = host(zoomed)
    assert cls._retire_barb_anchor_frame(state, placed, zoomed, 1.0) is True
    assert front.values["anchorFrameValid"] is False
    assert back.values["anchorFrameValid"] is True

    # A ratio-only change also retires the front but keeps a pending frame
    # already composed at the new ratio.
    state, front, back = host(placed, pending_dpr=2.0, dpr=2.0)
    assert cls._retire_barb_anchor_frame(state, placed, placed, 1.0) is True
    assert front.values["anchorFrameValid"] is False
    assert back.values["anchorFrameValid"] is True


def test_republished_native_frame_restores_anchor_validity_on_the_back():
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from sailworks.qt.gribviewport import ViewportCamera
    from sailworks.qt.viewportpublication import PublicationManager
    from sailworks.qt.viewporttransform import PanReference

    cls = _viewport_widget_class()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    camera = ViewportCamera(48.3, -123.0, 9, 900, 700, stamp)
    reference = PanReference.from_camera(camera)
    front, back = Layer(), Layer()
    front.values["anchorFrameValid"] = False
    back.values["anchorFrameValid"] = False
    host = SimpleNamespace(
        _barbs=front,
        _barbs_back=back,
        _native_weather_fields=True,
        _native_pending_camera=camera,
        _native_pending_dpr=1.0,
        _publication_manager=PublicationManager(),
        _bind_native_layer=Mock(),
        _native_pending_selection=None,
        _native_pending_selection_urls=(),
        _map_pan_reference=reference,
        _provider=SimpleNamespace(retain_selection=lambda urls: None),
        counters={"barbs_selection_publications": 0, "barbs_selection_uploads": 0},
        _wind_opacity=1.0,
    )
    host._barb_anchor_offset = lambda camera, selection=None: (
        cls._barb_anchor_offset(host, camera, selection)
    )
    host._shared_anchor_pool = lambda: cls._shared_anchor_pool(host)
    host._reset_barb_zoom_preview_item = cls._reset_barb_zoom_preview_item
    host._clear_barb_zoom_preview = lambda: cls._clear_barb_zoom_preview(host)
    field = SimpleNamespace(
        images=(1, 2),
        generation=1,
        identity="field",
        staged=SimpleNamespace(weight=lambda _: 0),
    )
    selection = SimpleNamespace(
        shape=(1, 1),
        anchor_reference=(camera.center_lat, camera.center_lon, 9, 900, 700),
    )
    # The new publication is staged on the back layer, not the retired front.
    cls._publish_barbs(host, field, selection, (), (), camera)
    assert front.values["anchorFrameValid"] is False
    assert back.values["anchorFrameValid"] is True
    assert back.values["anchorOffsetX"] == 0.0
    assert back.values["anchorOffsetY"] == 0.0


def test_trace_attribution_is_unavailable_not_zero_without_current_handles():
    classify = load("check_native_weather_integration").classify

    phase = {
        "name": "covered",
        "began": 1,
        "ended": 2,
        "native": {"layers": {"barbs": {"groups": [], "current": None}}},
    }
    assert classify([], [phase]) == {"covered": None}
    phase["native"]["layers"]["barbs"] = {
        "groups": [{"identity": "A", "handles": [1, 2, 3, 4]}],
        "current": "A",
    }
    assert classify([], [phase]) == {"covered": 0}
    events = [
        {"event": "uploadTexture", "texture": 1, "time": 1.5},
        {"event": "uploadTexture", "texture": 1, "time": 2.5},
        {"event": "uploadTexture", "texture": 5, "time": 1.5},
    ]
    assert classify(events, [phase]) == {"covered": 1}


def test_integration_gate_rejects_missing_and_false_evidence():
    runner = load("gate_runner")
    evaluate = runner.evaluate
    CHECKS = load("check_native_weather_integration").CHECKS

    gate = runner.gates.GATES["native-weather-integration"]
    assert all(row["verdict"] == "fail" for row in evaluate(gate, {}))
    report = {"checks": dict.fromkeys(CHECKS, True)}
    assert all(row["verdict"] == "pass" for row in evaluate(gate, report))
    for name in CHECKS:
        report["checks"][name] = False
        assert sum(row["verdict"] == "fail" for row in evaluate(gate, report)) == 1
        report["checks"][name] = True


def test_integration_tool_uses_standalone_viewport_host():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(load("check_native_weather_integration").run))
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert any(
        node.module == "sailworks.qt.viewportmap"
        and any(alias.name == "ViewportMap" for alias in node.names)
        for node in imports
    )
    assert not any(
        node.module == "sailworks.qt.mapcanvas"
        or any(alias.name == "TrackMap" for alias in node.names)
        for node in imports
    )
    constructors = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "ViewportMap" in constructors
    assert "TrackMap" not in constructors
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr in {"_center_lat", "_center_lon", "_zoom"}
        for node in ast.walk(tree)
    )


def _shared_anchor_host(cls, front, back, *, native=False, pending=None):
    """A host whose scene owns two shared anchor Image slots."""
    x0, y0, x1, y1 = Layer(), Layer(), Layer(), Layer()
    host = SimpleNamespace(
        _native_weather_fields=native,
        _native_pending_camera=pending,
        _barbs=front,
        _barbs_back=back,
        _fill=None,
        _fill_back=None,
        _shared_anchor_items=((x0, y0), (x1, y1)),
        _shared_anchor_urls=[None, None],
        _shared_anchor_front=None,
        _shared_anchor_pending=None,
        _barb_anchor_offset=lambda camera, selection=None: (0.0, 0.0),
        _publication_manager=SimpleNamespace(
            stage_resources=lambda *args, **kwargs: False,
            reject_pending=lambda *args, **kwargs: None,
        ),
        _bind_native_layer=lambda *args, **kwargs: None,
        counters={
            "barbs_selection_publications": 0,
            "barbs_selection_uploads": 0,
            "barbs_field_publications": 0,
            "barbs_field_uploads": 0,
        },
        _wind_opacity=1.0,
        _published=None,
    )
    host._shared_anchor_slot = lambda urls, pending: cls._shared_anchor_slot(
        host, urls, pending
    )
    host._shared_anchor_pool = lambda: cls._shared_anchor_pool(host)
    return host, (x0, y0, x1, y1)


def test_shared_anchor_pool_reuses_the_node_atlas_without_source_writes():
    cls = _viewport_widget_class()
    front = Layer()
    host, (x0, y0, x1, y1) = _shared_anchor_host(cls, front, None)
    field = SimpleNamespace(
        images=(),
        generation="g",
        staged=SimpleNamespace(weight=lambda _: 0),
    )
    selection = SimpleNamespace(shape=(1, 1), images=())
    first = ("image://weather/node-1", "image://weather/x", "image://weather/y")
    moved = ("image://weather/node-2", "image://weather/x", "image://weather/y")
    staged = ("image://weather/node-3", "image://weather/x2", "image://weather/y2")
    camera = SimpleNamespace(stamp=lambda: 0.0)

    cls._publish_barbs(host, field, selection, (), first, camera)
    assert (x0.property("source"), y0.property("source")) == (first[1], first[2])
    assert front.property("atlasSlot") == 0
    assert host._shared_anchor_front == 0
    assert front.property("nodeIdSource") == first[0]
    written = (dict(x0.values), dict(y0.values), dict(x1.values), dict(y1.values))

    # A changed node-id slot under the same anchor camera is the same atlas:
    # both shared Images keep their source, only the indirection image reloads.
    cls._publish_barbs(host, field, selection, (), moved, camera)
    assert (
        dict(x0.values),
        dict(y0.values),
        dict(x1.values),
        dict(y1.values),
    ) == written
    assert front.property("atlasSlot") == 0
    assert front.property("nodeIdSource") == moved[0]

    # A new anchor frame stages the slot the front layer is not displaying.
    cls._publish_barbs(host, field, selection, (), staged, camera)
    assert (x1.property("source"), y1.property("source")) == (staged[1], staged[2])
    assert (dict(x0.values), dict(y0.values)) == (written[0], written[1])
    assert front.property("atlasSlot") == 1
    assert host._shared_anchor_front == 1
    assert host._shared_anchor_pending is None


def test_native_commit_swaps_to_the_pending_slot_and_keeps_the_old_front():
    from unittest.mock import Mock

    cls = _viewport_widget_class()
    front, back = Layer(), Layer()
    front.values["atlasSlot"] = 0
    back.values["atlasSlot"] = 1
    pinned = []

    def pin(urls, retained=()):
        pinned.append((urls, retained))

    host = SimpleNamespace(
        _barbs=front,
        _barbs_back=back,
        _fill=None,
        _fill_back=None,
        _native_front_index=0,
        _native_pending_camera="new time",
        _native_pending_identity="new field",
        _native_pending_selection=None,
        _native_pending_dpr=1.0,
        _native_pending_selection_urls=("image://weather/node-2",),
        _native_contexts={},
        _native_publication_tokens={},
        _publication_manager=SimpleNamespace(commit_pending=Mock()),
        _provider=SimpleNamespace(pin_selection=pin),
        _wind_opacity=1.0,
        _wind_gradient_enabled=False,
        _shared_anchor_items=((Layer(), Layer()), (Layer(), Layer())),
        _shared_anchor_urls=[
            ("image://weather/x", "image://weather/y"),
            ("image://weather/x2", "image://weather/y2"),
        ],
        _shared_anchor_front=0,
        _shared_anchor_pending=1,
    )
    host._adopt_shared_anchor_front = lambda: cls._adopt_shared_anchor_front(host)
    host._clear_barb_zoom_preview = lambda: cls._clear_barb_zoom_preview(host)

    cls._commit_native_weather(host)

    assert host._barbs is back and host._barbs_back is front
    assert host._shared_anchor_front == 1
    assert host._shared_anchor_pending is None
    # The slot that no longer displays keeps its loaded pair; only the swap
    # changes which slot the front layer samples.
    assert host._barbs.property("atlasSlot") == 1
    assert host._barbs_back.property("atlasSlot") == 0
    # The provider keeps the new front and the pool's other slot servable.
    assert pinned == [
        (("image://weather/node-2",), ("image://weather/x", "image://weather/y"))
    ]


def test_clearing_weather_sources_releases_the_shared_pool():
    from PySide6.QtCore import QUrl

    cls = _viewport_widget_class()
    # A partially initialised pool must release the slot it owns: the provider
    # that backed both slots is gone, so leaving a loaded pair bound would keep
    # retired textures servable.  A complete pool clears both slots.
    for slot_count in (1, 2):
        pairs = [(Layer(), Layer()) for _ in range(slot_count)]
        items = tuple(pairs) + (None,) * (2 - slot_count)
        urls = [("image://weather/x", "image://weather/y")] + [None] * (slot_count - 1)
        host = SimpleNamespace(
            _barbs=None,
            _barbs_back=None,
            _fill=None,
            _fill_back=None,
            _native_weather_fields=False,
            _shared_anchor_items=items,
            _shared_anchor_urls=urls,
            _shared_anchor_front=0,
            _shared_anchor_pending=None,
        )
        for x_item, y_item in pairs:
            x_item.values["source"] = "image://weather/x"
            y_item.values["source"] = "image://weather/y"
        host._shared_anchor_pool = lambda h=host: cls._shared_anchor_pool(h)
        host._clear_shared_anchors = lambda h=host: cls._clear_shared_anchors(h)

        cls._clear_weather_sources(host)

        for x_item, y_item in pairs:
            assert x_item.property("source") == QUrl()
            assert y_item.property("source") == QUrl()
        assert host._shared_anchor_urls == [None, None]
        assert host._shared_anchor_front is None
        assert host._shared_anchor_pending is None
