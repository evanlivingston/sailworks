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
        _native_pending_identity="A",
        _update_native_state=lambda: None,
    )
    host._native_item = lambda role, slot: cls._native_item(host, role, slot)
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
        _native_pending_selection_urls=(),
        _provider=SimpleNamespace(pin_selection=lambda urls: None),
    )
    host._native_item = lambda role, slot: cls._native_item(host, role, slot)
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
        _translate_selection_anchors=lambda selection, camera, urls: urls,
        counters={"barbs_selection_publications": 0, "barbs_selection_uploads": 0},
        _wind_opacity=1.0,
    )
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
        _native_publishing=False,
        _weather_display_camera="old time",
        _native_contexts=contexts,
        _provider=SimpleNamespace(pin_selection=pinned.append),
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
    host._show_state = lambda state: setattr(host, "_state", state)
    front.values["ready"] = front_fill.values["ready"] = True
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
    assert pinned == [("image://weather/new",)]


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
