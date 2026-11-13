"""Retained routing geometry and picking model.

This is the headless half of the retained routing overlay: it turns a
schema-v1 result into stable polylines with identity, keeps style changes away
from geometry, and answers picking queries over the displayed geometry. It
never invents topology: engine contour order and open/closed state are kept and
polylines that would cross the antimeridian are split rather than joined.

Schema v1 stores traces at calculation scope rather than per layer, so trace
visibility is modelled explicitly and is not inferred from layer visibility.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from math import isfinite
from typing import Dict, Iterable, Optional, Tuple

from ..core.geo.routing import (
    DEFAULT_ISOCHRONE_COLOUR_MODE,
    normalize_isochrone_colour_by,
)
from ..core.marine.routingresult import RoutingResult


@dataclass(frozen=True)
class Polyline:
    """One drawable, topologically independent piece of routing geometry."""

    object_id: str
    calculation_id: str
    layer_id: str
    kind: str
    points: Tuple[Tuple[float, float], ...]
    closed: bool
    epoch_s: Optional[int]

    @property
    def revision_payload(self) -> bytes:
        parts = [self.object_id, self.layer_id, self.kind, str(self.closed)]
        parts.extend(f"{lat!r},{lon!r}" for lat, lon in self.points)
        return "\x1f".join(parts).encode("utf-8")


@dataclass(frozen=True)
class LayerInfo:
    layer_id: str
    epoch_s: int
    reverse: bool
    contour_ids: Tuple[str, ...]


@dataclass
class _Style:
    visible: Dict[str, bool] = field(default_factory=dict)
    selected_layer_id: Optional[str] = None
    thickness: Dict[str, float] = field(default_factory=dict)
    traces_visible: bool = True
    contours_visible: bool = True


def _split_world_wrap(
    points: Iterable[Tuple[float, float]],
) -> Tuple[Tuple[Tuple[float, float], ...], ...]:
    chunks = []
    current = []
    for point in points:
        if current and abs(point[1] - current[-1][1]) > 180.0:
            chunks.append(tuple(current))
            current = []
        current.append(point)
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


class RoutingOverlayModel:
    """Geometry, identity and picking for one routing calculation."""

    @classmethod
    def from_result_file(cls, path) -> "RoutingOverlayModel":
        """Load an offline schema-v1 result and retain its geometry."""
        from pathlib import Path

        text = Path(path).read_text(encoding="utf-8")
        return cls(RoutingResult.from_json(text))

    @classmethod
    def from_result_json(cls, text: str) -> "RoutingOverlayModel":
        return cls(RoutingResult.from_json(text))

    def __init__(self, result: RoutingResult):
        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        self._result = result
        self._calculation_id = result.calculation_id
        self._layers: Tuple[LayerInfo, ...] = tuple(
            LayerInfo(
                layer.layer_id,
                layer.time_epoch_s,
                layer.reverse,
                tuple(contour.contour_id for contour in layer.contours),
            )
            for layer in result.layers
        )
        polylines = []
        for layer in result.layers:
            for contour in layer.contours:
                points = tuple((point.lat, point.lon) for point in contour.points)
                for chunk_index, chunk in enumerate(_split_world_wrap(points)):
                    object_id = (
                        contour.contour_id
                        if chunk_index == 0
                        else f"{contour.contour_id}#{chunk_index}"
                    )
                    polylines.append(
                        Polyline(
                            object_id,
                            self._calculation_id,
                            layer.layer_id,
                            "contour",
                            chunk,
                            contour.closed and chunk_index == 0,
                            layer.time_epoch_s,
                        )
                    )
        for trace in result.traces:
            points = tuple((point.lat, point.lon) for point in trace.route)
            for chunk_index, chunk in enumerate(_split_world_wrap(points)):
                object_id = (
                    trace.trace_id
                    if chunk_index == 0
                    else f"{trace.trace_id}#{chunk_index}"
                )
                polylines.append(
                    Polyline(
                        object_id,
                        self._calculation_id,
                        "",
                        "trace",
                        chunk,
                        False,
                        None,
                    )
                )
        self._polylines = tuple(polylines)
        self._trace_ids = tuple(trace.trace_id for trace in result.traces)
        self._trace_endpoints = tuple(
            (trace.trace_id, trace.endpoint.lat, trace.endpoint.lon)
            for trace in result.traces
        )
        self._selection = None
        self._hover_trace_id: Optional[str] = None
        self._geometry_revision = 1
        self._style = _Style()
        self._style_revision = 0
        self._digest = self._compute_digest()

    @property
    def result(self) -> RoutingResult:
        return self._result

    @property
    def calculation_id(self) -> str:
        return self._calculation_id

    @property
    def geometry_revision(self) -> int:
        return self._geometry_revision

    @property
    def style_revision(self) -> int:
        return self._style_revision

    @property
    def geometry_digest(self) -> str:
        return self._digest

    @property
    def layers(self) -> Tuple[LayerInfo, ...]:
        return self._layers

    @property
    def polylines(self) -> Tuple[Polyline, ...]:
        return self._polylines

    @property
    def trace_ids(self) -> Tuple[str, ...]:
        return self._trace_ids

    @property
    def capabilities(self):
        """The result's own capability record, never an inferred substitute."""
        return self._result.capabilities

    @property
    def trace_times(self) -> str:
        return self._result.capabilities.trace_times

    @property
    def feature_states(self) -> Dict[str, str]:
        features = self._result.capabilities.features
        return {} if features is None else dict(features)

    @property
    def geometry_state(self) -> str:
        """Explicit drawable state: drawn, hidden, empty or absent."""
        if any(
            (polyline.kind == "trace" and self._style.traces_visible)
            or (polyline.kind == "contour" and self.contour_visible(polyline.layer_id))
            for polyline in self._polylines
        ):
            return "drawn"
        if self._polylines:
            return "hidden"
        if self._layers or self._trace_ids:
            return "empty"
        return "absent"

    def trace_is_visible(self, trace_id: str) -> bool:
        """A trace is selectable only while it is in the displayed geometry."""
        return self._style.traces_visible and trace_id in self._trace_ids

    def set_layer_visible(self, layer_id: str, visible: bool) -> None:
        self._require_layer(layer_id)
        if self._style.visible.get(layer_id) == visible:
            return
        self._style.visible[layer_id] = visible
        self._style_revision += 1

    def is_layer_visible(self, layer_id: str) -> bool:
        return self._style.visible.get(layer_id, True)

    @property
    def contours_visible(self) -> bool:
        """Independent isochrone display flag; layer state is left intact."""
        return self._style.contours_visible

    def set_contours_visible(self, visible: bool) -> None:
        if not isinstance(visible, bool):
            raise TypeError("contour visibility must be a boolean")
        if self._style.contours_visible == visible:
            return
        self._style.contours_visible = visible
        self._style_revision += 1

    def contour_visible(self, layer_id: str) -> bool:
        """Whether a contour layer is actually drawn right now."""
        return self._style.contours_visible and self.is_layer_visible(layer_id)

    def set_traces_visible(self, visible: bool) -> None:
        if not isinstance(visible, bool):
            raise TypeError("trace visibility must be a boolean")
        if self._style.traces_visible == visible:
            return
        self._style.traces_visible = visible
        if not visible:
            # A hidden trace must not stay hovered and become pinnable.
            self._hover_trace_id = None
        self._style_revision += 1

    @property
    def selection(self):
        return self._selection

    def attach_selection(self, selection) -> None:
        """Share the calculation-scoped selection used for pinned traces."""
        self._selection = selection

    @property
    def hover_trace_id(self) -> Optional[str]:
        return self._hover_trace_id

    def set_hover_trace(self, trace_id: Optional[str]) -> None:
        """Hover preview only: never changes selection or geometry."""
        if trace_id is not None and trace_id not in self._trace_ids:
            raise KeyError(f"unknown routing trace: {trace_id}")
        if trace_id is not None and not self.trace_is_visible(trace_id):
            trace_id = None
        if self._hover_trace_id == trace_id:
            return
        self._hover_trace_id = trace_id
        self._style_revision += 1

    def pick_trace_near(
        self, latitude: float, longitude: float, max_degrees: float = 0.5
    ) -> Optional[str]:
        """Nearest retained trace endpoint within a bounded pick radius."""
        if not self._style.traces_visible:
            return None
        best = None
        for trace_id, lat, lon in self._trace_endpoints:
            distance = (lat - latitude) ** 2 + (lon - longitude) ** 2
            if distance > max_degrees * max_degrees:
                continue
            candidate = (distance, trace_id)
            if best is None or candidate < best:
                best = candidate
        return None if best is None else best[1]

    @property
    def traces_visible(self) -> bool:
        return self._style.traces_visible

    def set_selected_layer(self, layer_id: Optional[str]) -> None:
        if layer_id is not None:
            self._require_layer(layer_id)
        self._style.selected_layer_id = layer_id
        self._style_revision += 1

    @property
    def selected_layer_id(self) -> Optional[str]:
        return self._style.selected_layer_id

    def set_thickness(self, kind: str, width: float) -> None:
        if kind not in {"contour", "trace"}:
            raise ValueError("thickness kind must be contour or trace")
        if not isfinite(width) or width <= 0:
            raise ValueError("thickness must be a positive number")
        self._style.thickness[kind] = width
        self._style_revision += 1

    def visible_polylines(self) -> Tuple[Polyline, ...]:
        return tuple(
            polyline
            for polyline in self._polylines
            if (polyline.kind == "trace" and self._style.traces_visible)
            or (polyline.kind == "contour" and self.contour_visible(polyline.layer_id))
        )

    def nearest_layer(self, epoch_s: int) -> Optional[str]:
        """Nearest displayed layer; ties choose the earlier layer."""
        best = None
        for layer in self._layers:
            if not self.is_layer_visible(layer.layer_id):
                continue
            distance = abs(layer.epoch_s - epoch_s)
            if best is None or distance < best[0]:
                best = (distance, layer.layer_id)
        return None if best is None else best[1]

    def pick_trace(self, latitude: float, longitude: float) -> Optional[str]:
        """Nearest retained trace endpoint among displayed traces."""
        if not self._style.traces_visible:
            return None
        best = None
        for trace_id, lat, lon in self._trace_endpoints:
            distance = (lat - latitude) ** 2 + (lon - longitude) ** 2
            candidate = (distance, trace_id)
            if best is None or candidate < best:
                best = candidate
        return None if best is None else best[1]

    def _require_layer(self, layer_id: str) -> None:
        if all(layer.layer_id != layer_id for layer in self._layers):
            raise KeyError(f"unknown routing layer: {layer_id}")

    def _compute_digest(self) -> str:
        digest = hashlib.sha256()
        for polyline in self._polylines:
            digest.update(polyline.revision_payload)
            digest.update(b"\x1e")
        return digest.hexdigest()


# Oldest to newest. Deep blue, blue, cyan, green, yellow, orange, red: the same
# ordering the eye reads as "later in the forecast". Kept here rather than in
# QML so the payload, legend and tests share one definition.
ISOCHRONE_TIME_GRADIENT = (
    (0.02, 0.05, 0.35),
    (0.00, 0.30, 0.85),
    (0.00, 0.68, 0.75),
    (0.35, 0.82, 0.35),
    (0.95, 0.85, 0.15),
    (0.95, 0.50, 0.05),
    (0.65, 0.05, 0.05),
)


def _gradient_colour(position: float) -> list:
    """Sample ``ISOCHRONE_TIME_GRADIENT``; 0 is oldest, 1 is newest."""
    position = min(1.0, max(0.0, float(position)))
    stops = ISOCHRONE_TIME_GRADIENT
    scaled = position * (len(stops) - 1)
    index = min(int(scaled), len(stops) - 2)
    fraction = scaled - index
    first, second = stops[index], stops[index + 1]
    return [
        round(first[channel] + (second[channel] - first[channel]) * fraction, 4)
        for channel in range(3)
    ]


def _epoch_label(epoch_s: int, show_date: bool, show_seconds: bool = False) -> str:
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(int(epoch_s), tz=timezone.utc)
    clock = moment.strftime("%H:%M:%SZ" if show_seconds else "%H:%MZ")
    return moment.strftime("%m-%d ") + clock if show_date else clock


def isochrone_time_colours(layers):
    """Colour layers from their recorded epoch, oldest to newest.

    Schema-v1 ``Layer`` records always carry a usable epoch, so every displayed
    layer gets a gradient position. Positions are index-based, so equal epochs
    still spread across the gradient and a lone visible layer gets a
    mid-gradient colour rather than the oldest extreme.
    """
    usable = sorted((layer.epoch_s, layer.layer_id) for layer in layers)
    last = len(usable) - 1
    return {
        layer_id: _gradient_colour(index / last if last else 0.5)
        for index, (_epoch, layer_id) in enumerate(usable)
    }


def isochrone_time_legend(layers, colours) -> dict:
    """Gradient legend entries oldest to newest, with distinguishable labels."""
    from datetime import datetime, timezone

    usable = sorted(
        (layer.epoch_s, layer.layer_id) for layer in layers if layer.layer_id in colours
    )
    days = {
        datetime.fromtimestamp(epoch, tz=timezone.utc).date()
        for epoch, _layer_id in usable
    }
    show_date = len(days) > 1
    labels = [_epoch_label(epoch, show_date) for epoch, _layer_id in usable]
    # Layers in the same minute need seconds; identical seconds get an index.
    if len(set(labels)) < len(labels):
        labels = [_epoch_label(epoch, show_date, True) for epoch, _layer_id in usable]
    counters = {}
    entries = []
    for label, (_epoch, layer_id) in zip(labels, usable):
        counters[label] = counters.get(label, 0) + 1
        if counters[label] > 1:
            label = f"{label} #{counters[label]}"
        entries.append([label, list(colours[layer_id])])
    return {"title": "Isochrones (UTC)", "entries": entries}


def _selected_trace_ids(model: RoutingOverlayModel):
    """Trace ids the shared selection currently pins, if any."""
    selection = getattr(model, "selection", None)
    if selection is None:
        return ()
    trace_id = getattr(selection, "trace_id", None)
    return () if trace_id is None else (trace_id,)


def routing_overlay_data(
    model: RoutingOverlayModel,
    isochrone_colour_by: str = DEFAULT_ISOCHRONE_COLOUR_MODE,
) -> dict:
    """QML-ready payload built only from engine-provided routing geometry.

    Segments are consecutive engine points; no point is joined to another
    polyline, so disconnected contours stay disconnected and a dateline
    crossing stays split. ``frontiers`` mirrors the shape the existing viewport
    isochrone overlay already consumes, so the native layer can replace the
    proximity-joined preview without changing the QML consumer.

    ``isochrone_colour_by="time"`` adds a gradient colour to each visible layer
    and frontier plus a legend keyed to the layers' recorded epochs. The
    uniform payload is unchanged.
    """

    if not isinstance(model, RoutingOverlayModel):
        raise TypeError("model must be a RoutingOverlayModel")
    mode = normalize_isochrone_colour_by(isochrone_colour_by)
    visible_layers = [
        layer for layer in model.layers if model.contour_visible(layer.layer_id)
    ]
    layer_colours = {}
    if mode == "time":
        layer_colours = isochrone_time_colours(visible_layers)
    layers = []
    frontiers = []
    for layer in visible_layers:
        segments = []
        closed = []
        for polyline in model.polylines:
            if polyline.kind != "contour" or polyline.layer_id != layer.layer_id:
                continue
            segments.extend(
                [list(first), list(second)]
                for first, second in zip(polyline.points, polyline.points[1:])
            )
            closed.append(polyline.closed)
        layer_payload = {
            "id": layer.layer_id,
            "time": layer.epoch_s,
            "reverse": layer.reverse,
            "segments": segments,
            "contour_count": len(closed),
            "closed": closed,
        }
        frontier_payload = {
            "segments": segments,
            "points": [],
            "layer_id": layer.layer_id,
            "time": layer.epoch_s,
            "reverse": layer.reverse,
            "selected": layer.layer_id == model.selected_layer_id,
        }
        if layer.layer_id in layer_colours:
            colour = layer_colours[layer.layer_id]
            layer_payload["colour"] = list(colour)
            frontier_payload["colour"] = list(colour)
        layers.append(layer_payload)
        frontiers.append(frontier_payload)
    traces = []
    if model.trace_ids and model.traces_visible:
        for polyline in model.polylines:
            if polyline.kind != "trace":
                continue
            traces.append(
                {
                    "id": polyline.object_id,
                    "hover": polyline.object_id == model.hover_trace_id,
                    "selected": polyline.object_id in _selected_trace_ids(model),
                    "segments": [
                        [list(first), list(second)]
                        for first, second in zip(polyline.points, polyline.points[1:])
                    ],
                }
            )
    payload = {
        "schema_version": 1,
        "calculation_id": model.calculation_id,
        "geometry_revision": model.geometry_revision,
        "style_revision": model.style_revision,
        "geometry_state": model.geometry_state,
        "trace_times": model.trace_times,
        "features": model.feature_states,
        "layers": layers,
        "selected_layer_id": model.selected_layer_id,
        "hover_trace_id": model.hover_trace_id,
        "frontiers": frontiers,
        "traces": traces,
        "path": [],
        "exclusions": [],
    }
    if mode == "time":
        # Every schema-v1 layer carries a recorded epoch, so time is always
        # representable and is never downgraded to a uniform colour.
        payload["isochrone_colour_by"] = "time"
        payload["legend"] = isochrone_time_legend(visible_layers, layer_colours)
    return payload
