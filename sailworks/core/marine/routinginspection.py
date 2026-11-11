"""Isochrone inspection over a versioned routing result.

Reads only values recorded in the result: layer times, delivered-leg readouts
and retained traces. Nothing here reloads a forecast or invents a value; an
absent field is reported as missing rather than as a reassuring zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

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
