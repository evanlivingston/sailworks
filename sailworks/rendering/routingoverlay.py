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

    def __init__(self, result: RoutingResult):
        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
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
        self._geometry_revision = 1
        self._style = _Style()
        self._style_revision = 0
        self._digest = self._compute_digest()

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

    def set_layer_visible(self, layer_id: str, visible: bool) -> None:
        self._require_layer(layer_id)
        if self._style.visible.get(layer_id) == visible:
            return
        self._style.visible[layer_id] = visible
        self._style_revision += 1

    def is_layer_visible(self, layer_id: str) -> bool:
        return self._style.visible.get(layer_id, True)

    def set_traces_visible(self, visible: bool) -> None:
        if not isinstance(visible, bool):
            raise TypeError("trace visibility must be a boolean")
        if self._style.traces_visible == visible:
            return
        self._style.traces_visible = visible
        self._style_revision += 1

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
            or (polyline.kind == "contour" and self.is_layer_visible(polyline.layer_id))
        )

    def nearest_layer(self, epoch_s: int) -> Optional[str]:
        """Layer nearest a forecast time; ties choose the earlier layer."""
        best = None
        for layer in self._layers:
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


def routing_overlay_data(model: RoutingOverlayModel) -> dict:
    """QML-ready payload built only from engine-provided routing geometry.

    Segments are consecutive engine points; no point is joined to another
    polyline, so disconnected contours stay disconnected and a dateline
    crossing stays split. ``frontiers`` mirrors the shape the existing viewport
    isochrone overlay already consumes, so the native layer can replace the
    proximity-joined preview without changing the QML consumer.
    """

    if not isinstance(model, RoutingOverlayModel):
        raise TypeError("model must be a RoutingOverlayModel")
    layers = []
    frontiers = []
    for layer in model.layers:
        if not model.is_layer_visible(layer.layer_id):
            continue
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
        layers.append(
            {
                "id": layer.layer_id,
                "time": layer.epoch_s,
                "reverse": layer.reverse,
                "segments": segments,
                "contour_count": len(closed),
                "closed": closed,
            }
        )
        frontiers.append({"segments": segments, "points": []})
    traces = []
    if model.trace_ids and model.traces_visible:
        for polyline in model.polylines:
            if polyline.kind != "trace":
                continue
            traces.append(
                {
                    "id": polyline.object_id,
                    "segments": [
                        [list(first), list(second)]
                        for first, second in zip(polyline.points, polyline.points[1:])
                    ],
                }
            )
    return {
        "schema_version": 1,
        "calculation_id": model.calculation_id,
        "geometry_revision": model.geometry_revision,
        "style_revision": model.style_revision,
        "layers": layers,
        "frontiers": frontiers,
        "traces": traces,
        "path": [],
        "exclusions": [],
    }
