"""Routing alternatives and saved results come from the controller's collection."""

from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import sailworks.core.marine.routinginspection as routinginspection
from sailworks.qt.window import PreviewWindow


def _route(name, *, has_result=True, calculation_id=None):
    summary = {} if calculation_id is None else {"calculation_id": calculation_id}
    return SimpleNamespace(
        name=name,
        result_ref=object() if has_result else None,
        result_summary=summary,
        inspection_context={},
    )


def _window(collection, **extra):
    controller = SimpleNamespace(
        core=SimpleNamespace(routingManager=collection),
        routing=SimpleNamespace(),
    )
    window = SimpleNamespace(controller=controller)
    # Production methods call ``self._routing_alternatives()``; bind the real
    # helper so the stand-in exercises it instead of only its own attributes.
    window._routing_alternatives = MethodType(
        PreviewWindow._routing_alternatives, window
    )
    for key, value in extra.items():
        setattr(window, key, value)
    return window


def test_routing_alternatives_reads_controller_collection():
    saved = _route("alpha", calculation_id="deadbeef")
    unsaved = _route("beta", has_result=False)
    window = _window(SimpleNamespace(elements=[saved, unsaved]))

    # The window itself must not be the lookup root; only controller.core is.
    assert not hasattr(window, "core")
    assert PreviewWindow._routing_alternatives(window) == (saved,)


def test_refresh_routing_alternatives_reads_controller_collection():
    saved = _route("alpha", calculation_id="deadbeef")
    window = _window(
        SimpleNamespace(elements=[saved]),
        routing_inspector=Mock(),
    )

    PreviewWindow._refresh_routing_alternatives(window, select_route=saved)

    assert window._routing_alternative_routes == (saved,)
    window.routing_inspector.set_alternatives.assert_called_once_with(
        ["alpha · deadbeef"], 0
    )


def test_show_saved_routing_route_loads_from_controller_collection(monkeypatch):
    route = _route("alpha", calculation_id="deadbeef")
    result = object()
    model = object()
    manager = Mock()
    manager.load_result.return_value = result
    window = _window(
        manager,
        routing_inspector=Mock(),
        routing_inspector_dock=Mock(),
        _select_saved_route=Mock(),
        _present_routing_result=Mock(),
    )
    window.controller.routing = SimpleNamespace(
        show_native_result=Mock(return_value=model)
    )
    context = object()
    fake_context = type(
        "FakeInspectionContext",
        (),
        {"from_dict": staticmethod(lambda data: context)},
    )
    monkeypatch.setattr(routinginspection, "InspectionContext", fake_context)

    PreviewWindow._show_saved_routing_route(window, route)

    manager.load_result.assert_called_once_with(route)
    window.controller.routing.show_native_result.assert_called_once_with(result)
    window._select_saved_route.assert_called_once_with(route)
    window._present_routing_result.assert_called_once_with(
        model, route=route, context=context, context_error=None
    )
    window.routing_inspector.show_error.assert_not_called()
