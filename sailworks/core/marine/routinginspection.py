"""Isochrone inspection over a versioned routing result.

Reads only values recorded in the result: layer times, delivered-leg readouts
and retained traces. Nothing here reloads a forecast or invents a value; an
absent field is reported as missing rather than as a reassuring zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from sailworks.core.geo.mapshapes import avoid_zones_digest_from_native

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
    "wave_height_metres",
    "wave_period_seconds",
    "wave_direction_from_degrees",
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
class AvoidZoneSummary:
    """One frozen polygon exactly as the calculation recorded it."""

    zone_id: str
    name: str
    vertex_count: Optional[int]


@dataclass(frozen=True)
class AvoidZonesEvidence:
    """Frozen avoid-zone constraints saved with a result, not the live scene.

    ``active`` reports only that the recorded calculation applied the frozen
    polygon set. It never asserts that a zone changed the route: that causal
    claim needs a comparison or replay that no result field records.
    """

    digest: str
    active: Optional[bool]
    zones: Tuple[AvoidZoneSummary, ...]
    consistency: Tuple[str, ...]
    causality: str


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
    "no_feasible_route": "no feasible route: no path within the configured bounds",
    "resource_limit_reached": (
        "resource exhausted: the search stopped at a configured resource limit"
    ),
    "internal_error": "internal error: the engine reported an internal failure",
    "unsatisfiable_rounding": (
        "unsatisfiable rounding: the mark side or exit bearing was not reached"
    ),
    "wave_data_required": (
        "wave data required: the selected wave source does not cover the "
        "route window; no route was calculated"
    ),
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


_ZONE_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ZONE_CAUSALITY = (
    "no reroute is claimed: the result records no comparison or replay of the "
    "same request without these zones"
)


def _zone_summaries(value: Any, problems: list) -> Tuple[AvoidZoneSummary, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        problems.append("recorded zone set is not a list")
        return ()
    zones = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            problems.append(f"zone descriptor {index} is not an object")
            continue
        zone_id = item.get("id")
        name = item.get("name", "")
        count = item.get("vertex_count")
        if not isinstance(zone_id, str) or not zone_id:
            problems.append(f"zone descriptor {index} has no id")
            continue
        if not isinstance(name, str):
            problems.append(f"zone descriptor {index} has a non-text name")
            name = ""
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or count < 0
        ):
            problems.append(f"zone descriptor {index} has an invalid vertex count")
            count = None
        zones.append(AvoidZoneSummary(zone_id, name, count))
    return tuple(zones)


def _fingerprint_problems(
    diagnostics: Mapping[str, Any],
    digest: str,
    zones: Tuple[AvoidZoneSummary, ...],
) -> list:
    """Cross-check the recorded descriptors against the request fingerprints."""
    fingerprints = diagnostics.get("input_fingerprints")
    if not isinstance(fingerprints, Mapping):
        return []
    problems = []
    recorded_digest = fingerprints.get("avoid_zones_digest")
    if recorded_digest is not None and digest and recorded_digest != digest:
        problems.append(
            "input fingerprint digest does not match the recorded zone digest"
        )
    listed = fingerprints.get("avoid_zones")
    if listed is None:
        return problems
    if not isinstance(listed, (list, tuple)):
        problems.append("input fingerprint zone list is not a list")
        return problems
    expected = [(zone.zone_id, zone.name, zone.vertex_count) for zone in zones]
    actual = []
    for index, item in enumerate(listed):
        if not isinstance(item, Mapping):
            problems.append(f"input fingerprint zone {index} is not an object")
            return problems
        actual.append((item.get("id"), item.get("name", ""), item.get("vertex_count")))
    if actual != expected:
        problems.append(
            "input fingerprint zone set differs from the recorded zone descriptors"
        )
    _fingerprint_geometry_problems(listed, digest, problems)
    return problems


def _fingerprint_geometry_problems(
    listed: list,
    digest: str,
    problems: list,
) -> None:
    """Re-hash the stored fingerprint rings against the saved zone digest.

    Descriptor comparison cannot see a geometry edit that keeps the same zone
    ids, names and vertex counts, so the stored payload is canonicalised and
    hashed the way the calculation hashed it. A fingerprint that predates
    ordered vertex payloads carries no geometry to re-hash; it is left to the
    descriptor and digest checks rather than reported as altered.
    """
    if not digest:
        return
    payload = []
    for index, item in enumerate(listed):
        if not isinstance(item, Mapping):
            return
        if "vertices" not in item:
            return
        vertices = item["vertices"]
        if not isinstance(vertices, (list, tuple)):
            problems.append(
                f"input fingerprint zone {index} has a non-list vertex payload"
            )
            return
        payload.append(
            {
                "id": item.get("id"),
                "name": item.get("name", ""),
                "vertices": vertices,
            }
        )
    if not payload:
        return
    try:
        recomputed = avoid_zones_digest_from_native(payload)
    except (TypeError, ValueError):
        problems.append(
            "input fingerprint zone geometry could not be re-hashed against "
            "the recorded zone digest"
        )
        return
    if recomputed != digest:
        problems.append(
            "input fingerprint zone geometry does not match the recorded zone digest"
        )


def avoid_zone_evidence(result: RoutingResult) -> Optional[AvoidZonesEvidence]:
    """Frozen zone evidence from a saved result, or None for a no-zone result.

    Only values recorded in the artifact are read. Nothing here consults the
    current scene, so a zone added or edited after the calculation cannot be
    mistaken for the set that was applied.
    """
    if not isinstance(result, RoutingResult):
        raise TypeError("result must be a RoutingResult")
    diagnostics = result.outcome.diagnostics or {}
    if not isinstance(diagnostics, Mapping):
        return None
    keys = ("avoid_zones", "avoid_zones_digest", "avoid_zones_active")
    if not any(key in diagnostics for key in keys):
        return None
    problems = []
    zones = _zone_summaries(diagnostics.get("avoid_zones"), problems)
    digest = diagnostics.get("avoid_zones_digest")
    if digest is None:
        problems.append("zone digest was not recorded")
        digest = ""
    elif not isinstance(digest, str) or not _ZONE_DIGEST.fullmatch(digest):
        problems.append("zone digest is not 64 lowercase hexadecimal characters")
        digest = "" if not isinstance(digest, str) else digest
    active = diagnostics.get("avoid_zones_active")
    if active is None:
        problems.append("zone active flag was not recorded")
        active = None
    elif not isinstance(active, bool):
        problems.append("zone active flag is not a boolean")
        active = None
    if active and not zones:
        problems.append("zones are marked active but no descriptor was recorded")
    if zones and active is False:
        problems.append("zone descriptors were recorded but are marked inactive")
    problems.extend(_fingerprint_problems(diagnostics, digest, zones))
    return AvoidZonesEvidence(digest, active, zones, tuple(problems), _ZONE_CAUSALITY)


def _avoid_zone_states(result: RoutingResult) -> Tuple[DiagnosticState, ...]:
    """Disclose the frozen zone set and refuse any unproven causal claim."""
    evidence = avoid_zone_evidence(result)
    if evidence is None:
        return ()
    labels = ", ".join(
        f"{zone.name or zone.zone_id} [{zone.zone_id}]" for zone in evidence.zones
    )
    count = len(evidence.zones)
    if evidence.active is True:
        detail = f"{count} frozen zone constraint(s) applied: {labels}"
    elif evidence.active is False:
        detail = f"{count} frozen zone constraint(s) recorded but not active: {labels}"
    else:
        detail = (
            f"{count} frozen zone constraint(s) recorded; "
            f"applied state not proven: {labels}"
        )
    if evidence.digest:
        detail += f"; digest {evidence.digest}"
    states = [DiagnosticState("avoid zones", detail)]
    states.append(DiagnosticState("avoid zone effect", evidence.causality))
    if evidence.active and result.status != "complete":
        states.append(
            DiagnosticState(
                "zone constraint",
                f"no route delivered with {count} active avoid-zone constraint(s); "
                "zone causation not proven (no comparison or replay evidence recorded)",
            )
        )
    if evidence.consistency:
        states.append(
            DiagnosticState("avoid zone evidence", "; ".join(evidence.consistency))
        )
    return tuple(states)


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
            "no_feasible_route": (
                "no feasible route: no path within the configured bounds"
            ),
            "resource_limit_reached": (
                "resource exhausted: stopped at a configured resource limit"
            ),
            "internal_error": "internal error: engine reported an internal failure",
            "unsatisfiable_rounding": (
                "unsatisfiable rounding: the mark side or exit bearing was "
                "not reached"
            ),
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
    states.extend(_wave_provenance_states(result))
    states.extend(_avoid_zone_states(result))
    states.append(DiagnosticState("trace times", str(result.capabilities.trace_times)))
    return tuple(states)


def _wave_provenance_states(result: RoutingResult) -> Tuple[DiagnosticState, ...]:
    """Disclose real wave heights versus the legacy wind-based proxy."""
    diagnostics = result.outcome.diagnostics or {}
    provenance = diagnostics.get("wave_provenance")
    if not isinstance(provenance, Mapping):
        return ()
    real = provenance.get("uses_real_wave_heights") is True
    proxy = provenance.get("uses_wind_proxy") is True
    dataset = provenance.get("dataset_identity") or provenance.get("model_identity")
    if real:
        source = f"real wave heights from {dataset}" if dataset else "real wave heights"
    elif proxy:
        source = "none (legacy wind-based speed reduction, no wave heights)"
    else:
        source = "none (no wave heights and no wind proxy)"
    if real and proxy:
        source += "; speed still uses the legacy wind proxy"
    states = [DiagnosticState("wave source", source)]
    if real:
        units = provenance.get("units") or provenance.get("unit") or {}
        if isinstance(units, Mapping) and units:
            text = " · ".join(f"{name} {unit}" for name, unit in sorted(units.items()))
        else:
            text = "height m · period s · direction degrees coming from"
        states.append(DiagnosticState("wave units", text))
    return tuple(states)


class RoutingInspector:
    """Layer list, nearest-layer linking and trace inspection for one result."""

    def __init__(self, result: RoutingResult):
        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        self._result = result

    def avoid_zones(self) -> Optional[AvoidZonesEvidence]:
        """Frozen zone evidence saved with this result, never the live scene."""
        return avoid_zone_evidence(self._result)

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

    def nearest_layer(
        self,
        epoch_s: int,
        is_visible: Optional[Callable[[str], bool]] = None,
    ) -> Optional[LayerSummary]:
        """Nearest displayed layer; ties choose the earlier layer."""
        best = None
        for layer in self.layers():
            if is_visible is not None and not is_visible(layer.layer_id):
                continue
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
