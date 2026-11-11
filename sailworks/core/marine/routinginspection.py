"""Isochrone inspection over a versioned routing result.

Reads only values recorded in the result: layer times, delivered-leg readouts
and retained traces. Nothing here reloads a forecast or invents a value; an
absent field is reported as missing rather than as a reassuring zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .routingresult import RoutingResult

READOUT_FIELDS = (
    "heading_degrees",
    "course_through_water_degrees",
    "course_over_ground_degrees",
    "speed_through_water_knots",
    "speed_over_ground_knots",
    "true_wind_speed_knots",
    "true_wind_angle_degrees",
    "wind_gust_knots",
    "estimated_fuel_litres",
    "propulsion_mode",
    "tack",
    "stationary_wait",
    "warning_count",
)


@dataclass(frozen=True)
class LayerSummary:
    layer_id: str
    time_utc: str
    time_epoch_s: int
    offset_s: int
    reverse: bool
    contour_count: int


@dataclass(frozen=True)
class TraceInspection:
    trace_id: str
    endpoint: Tuple[float, float]
    positions: Tuple[Tuple[float, float], ...]
    times: Optional[Tuple[int, ...]]


@dataclass(frozen=True)
class DiagnosticState:
    """One explicit, user-visible state derived from recorded result metadata."""

    code: str
    state: str


@dataclass(frozen=True)
class InspectionContext:
    """Selection saved with a referenced result and restored offline.

    The context only names recorded identifiers; restoring it never loads a
    forecast, resamples geometry or recalculates a route. Identifiers missing
    from the referenced artifact are reported by :meth:`apply` instead of being
    silently substituted.
    """

    calculation_id: str
    layer_id: Optional[str] = None
    trace_id: Optional[str] = None
    leg_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.calculation_id, str) or not self.calculation_id:
            raise ValueError("inspection context needs a calculation id")
        for name in ("layer_id", "trace_id", "leg_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string or null")

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "calculation_id": self.calculation_id,
            "layer_id": self.layer_id,
            "trace_id": self.trace_id,
            "leg_id": self.leg_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Optional["InspectionContext"]:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("inspection context must be an object")
        calculation_id = value.get("calculation_id")
        if not isinstance(calculation_id, str) or not calculation_id:
            raise ValueError("inspection context needs a calculation id")
        return cls(
            calculation_id,
            value.get("layer_id"),
            value.get("trace_id"),
            value.get("leg_id"),
        )

    @classmethod
    def from_selection(cls, selection: Any) -> "InspectionContext":
        return cls(
            selection.calculation_id,
            selection.layer_id,
            selection.trace_id,
            selection.leg_id,
        )

    def _known(self, result: RoutingResult) -> Dict[str, bool]:
        layers = {layer.layer_id for layer in result.layers}
        traces = {trace.trace_id for trace in result.traces}
        legs = {leg.leg_id for leg in result.legs}
        return {
            "layer_id": self.layer_id is None or self.layer_id in layers,
            "trace_id": self.trace_id is None or self.trace_id in traces,
            "leg_id": self.leg_id is None or self.leg_id in legs,
        }

    def missing(self, result: RoutingResult) -> Tuple[str, ...]:
        """Identifiers in this context that the artifact no longer records."""
        if self.calculation_id != result.calculation_id:
            return ("calculation_id", "layer_id", "trace_id", "leg_id")
        known = self._known(result)
        return tuple(
            name for name in ("layer_id", "trace_id", "leg_id") if not known[name]
        )

    def apply(self, selection: Any) -> Tuple[str, ...]:
        """Apply the recorded identifiers, skipping and reporting missing ones."""
        if self.calculation_id != selection.calculation_id:
            raise ValueError("inspection context belongs to another calculation")
        result = selection._result
        known = self._known(result)
        missing = []
        if self.layer_id is not None:
            if known["layer_id"]:
                selection.select_layer(self.layer_id)
            else:
                missing.append("layer_id")
        if self.trace_id is not None:
            if known["trace_id"]:
                selection.select_trace(self.trace_id)
            else:
                missing.append("trace_id")
        if self.leg_id is not None:
            if known["leg_id"]:
                selection.select_leg(self.leg_id)
            else:
                missing.append("leg_id")
        return tuple(missing)


_STATUS_DETAIL = {
    "complete": "complete: a route was delivered",
    "incomplete": "incomplete: the search ended without a complete route",
    "failed": "failed: no route was delivered",
    "cancelled": "cancelled: the calculation stopped before completion",
    "preview": "preview: provisional geometry, not a calculated route",
    "resource_limit_reached": (
        "resource exhausted: the search stopped at a configured resource limit"
    ),
    "internal_error": "internal error: the engine reported an internal failure",
}
_RESOURCE_MARKERS = (
    "resource_limit",
    "resource limit",
    "resource_exhaust",
    "tour_limit",
    "time_limit",
    "budget_exhaust",
    "limit_reached",
    "exhausted",
)


def _resource_evidence(value: Any) -> Optional[str]:
    """Report a real resource-limit marker from recorded native diagnostics."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                label = str(key).casefold()
                if any(marker in label for marker in _RESOURCE_MARKERS):
                    return str(key)
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str):
            label = item.casefold()
            if any(marker in label for marker in _RESOURCE_MARKERS):
                return item[:120]
    return None


def _resource_state(result: RoutingResult) -> Optional[DiagnosticState]:
    """Resource exhaustion only when the native status or a marker records it.

    A delivered route is never labelled resource-exhausted: recorded budgets
    often echo configured limits, and a complete status means a route was
    delivered within them.
    """
    if result.status == "complete":
        return None
    evidence = _resource_evidence(result.outcome.diagnostics)
    if result.status == "resource_limit_reached":
        detail = "exhausted: native status resource_limit_reached"
        if evidence is not None:
            detail += f" (recorded marker {evidence!r})"
        return DiagnosticState("resource", detail)
    if evidence is not None:
        return DiagnosticState("resource", f"exhausted: recorded marker {evidence!r}")
    return None


def _validation_state(result: RoutingResult) -> DiagnosticState:
    validation = result.outcome.validation
    if validation.state == "passed":
        detail = (
            "passed (independent)"
            if validation.independent
            else "passed (not independent)"
        )
        return DiagnosticState("validation", detail)
    if validation.state == "failed":
        return DiagnosticState("validation", "failed: validation rejected this route")
    return DiagnosticState(
        "validation", "unknown: no independent validation verdict was recorded"
    )


def _geometry_state(result: RoutingResult) -> DiagnosticState:
    capabilities = result.capabilities
    features = dict(capabilities.features or {})
    state = features.get("inspection_geometry")
    if state == "sampled":
        if capabilities.validation_mode == "sampled":
            detail = (
                f"sampled ({capabilities.sample_count} samples at "
                f"{capabilities.cadence} s cadence)"
            )
        else:
            detail = "sampled"
        return DiagnosticState(
            "inspection_geometry",
            detail + "; inspection geometry is bounded and may be sampled",
        )
    if state == "complete":
        return DiagnosticState(
            "inspection_geometry", "complete; inspection geometry is bounded"
        )
    if state == "absent":
        return DiagnosticState(
            "inspection_geometry",
            "absent: no inspection geometry was recorded for this result",
        )
    if state == "unsupported":
        return DiagnosticState(
            "inspection_geometry", "unsupported by the recording engine"
        )
    return DiagnosticState("inspection_geometry", "not reported by this result")


def diagnostics(result: RoutingResult) -> Tuple[DiagnosticState, ...]:
    """Distinct visible states for search, resources, validation and geometry."""
    if not isinstance(result, RoutingResult):
        raise TypeError("result must be a RoutingResult")
    states = [
        DiagnosticState("status", _STATUS_DETAIL.get(result.status, result.status))
    ]
    if result.status != "complete":
        search_detail = {
            "incomplete": "incomplete: no complete route was delivered",
            "failed": "failed: no route was delivered",
            "cancelled": "cancelled: stopped before completion",
            "preview": "preview: provisional search output, not a calculated route",
            "resource_limit_reached": (
                "resource exhausted: stopped at a configured resource limit"
            ),
            "internal_error": "internal error: engine reported an internal failure",
        }.get(result.status, f"search ended: {result.status}")
        states.append(DiagnosticState("search", search_detail))
    resource = _resource_state(result)
    if resource is not None:
        states.append(resource)
    states.append(_validation_state(result))
    states.append(_geometry_state(result))
    features = dict(result.capabilities.features or {})
    for name in ("waves", "shoreline", "depth", "chart_hazards"):
        if name in features:
            states.append(DiagnosticState(name, str(features[name])))
    states.append(DiagnosticState("trace times", str(result.capabilities.trace_times)))
    return tuple(states)


class RoutingInspector:
    """Layer list, nearest-layer linking and trace inspection for one result."""

    def __init__(self, result: RoutingResult):
        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        self._result = result

    def layers(self) -> Tuple[LayerSummary, ...]:
        if not self._result.layers:
            return ()
        start = self._result.layers[0].time_epoch_s
        return tuple(
            LayerSummary(
                layer.layer_id,
                layer.time_utc,
                layer.time_epoch_s,
                layer.time_epoch_s - start,
                layer.reverse,
                len(layer.contours),
            )
            for layer in self._result.layers
        )

    def nearest_layer(self, epoch_s: int) -> Optional[LayerSummary]:
        """Layer nearest a forecast instant; ties choose the earlier layer."""
        best = None
        for layer in self.layers():
            distance = abs(layer.time_epoch_s - epoch_s)
            if best is None or distance < best[0]:
                best = (distance, layer)
        return None if best is None else best[1]

    def trace_at(self, latitude: float, longitude: float) -> Optional[TraceInspection]:
        """Nearest retained trace endpoint, with predecessor positions and times."""
        best = None
        for trace in self._result.traces:
            distance = (trace.endpoint.lat - latitude) ** 2 + (
                trace.endpoint.lon - longitude
            ) ** 2
            candidate = (distance, trace.trace_id)
            if best is None or candidate < best[0]:
                best = (candidate, trace)
        if best is None:
            return None
        trace = best[1]
        return TraceInspection(
            trace.trace_id,
            (trace.endpoint.lat, trace.endpoint.lon),
            tuple((point.lat, point.lon) for point in trace.route),
            trace.times,
        )

    def leg_readout(self, leg_id: str) -> Dict[str, Any]:
        """Recorded readout for one delivered leg, with explicit missing fields."""
        for leg in self._result.legs:
            if leg.leg_id != leg_id:
                continue
            recorded = dict(leg.readout or {})
            missing = tuple(name for name in READOUT_FIELDS if name not in recorded)
            return {
                "leg_id": leg.leg_id,
                "start_utc": leg.start_utc,
                "end_utc": leg.end_utc,
                "values": recorded,
                "missing": missing,
            }
        raise KeyError(f"unknown routing leg: {leg_id}")

    def leg_readouts(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(self.leg_readout(leg.leg_id) for leg in self._result.legs)
