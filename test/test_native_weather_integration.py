"""Native layer binding policy without starting a scene graph."""

from types import SimpleNamespace

import pytest

from gweatherrouting.qt.gribviewport import _viewport_widget_class
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
    from gweatherrouting.qt import controller, gpuweathertextures

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
    from gweatherrouting.qt import __main__ as launcher, gribviewport

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


def test_native_weather_swap_keeps_front_until_pending_layers_are_ready():
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
        _wind_opacity=0.88,
        _wind_style="barbs",
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
        node.module == "gweatherrouting.qt.viewportmap"
        and any(alias.name == "ViewportMap" for alias in node.names)
        for node in imports
    )
    assert not any(
        node.module == "gweatherrouting.qt.mapcanvas"
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
