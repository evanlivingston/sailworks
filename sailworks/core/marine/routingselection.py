"""One calculation-scoped selection shared by routing surfaces.

Map, layer list, inspector and plots all read and write this object, so a
selection always belongs to exactly one calculation and one layer. Changing the
forecast time only moves the selected layer; it never touches the result,
geometry or recorded readouts.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from .routingresult import RoutingResult


class RoutingSelection:
    """Selection state for one routing calculation."""

    def __init__(self, result: RoutingResult):
        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        self._result = result
        self._layer_id: Optional[str] = None
        self._trace_id: Optional[str] = None
        self._leg_id: Optional[str] = None
        self._listeners: List[Callable[[], None]] = []

    @property
    def calculation_id(self) -> str:
        return self._result.calculation_id

    @property
    def layer_id(self) -> Optional[str]:
        return self._layer_id

    @property
    def trace_id(self) -> Optional[str]:
        return self._trace_id

    @property
    def leg_id(self) -> Optional[str]:
        return self._leg_id

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def select_layer(self, layer_id: str) -> None:
        if all(layer.layer_id != layer_id for layer in self._result.layers):
            raise KeyError(f"unknown routing layer: {layer_id}")
        if self._layer_id == layer_id:
            return
        self._layer_id = layer_id
        self._notify()

    def select_trace(self, trace_id: Optional[str]) -> None:
        if trace_id is not None and all(
            trace.trace_id != trace_id for trace in self._result.traces
        ):
            raise KeyError(f"unknown routing trace: {trace_id}")
        if self._trace_id == trace_id:
            return
        self._trace_id = trace_id
        self._notify()

    def select_leg(self, leg_id: str) -> None:
        if all(leg.leg_id != leg_id for leg in self._result.legs):
            raise KeyError(f"unknown routing leg: {leg_id}")
        if self._leg_id == leg_id:
            return
        self._leg_id = leg_id
        self._notify()

    def clear(self) -> None:
        if self._layer_id is None and self._trace_id is None and self._leg_id is None:
            return
        self._layer_id = None
        self._trace_id = None
        self._leg_id = None
        self._notify()

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()
