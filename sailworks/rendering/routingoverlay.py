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
    fill_visible: bool = False
    outlines_visible: bool = False


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
        self._boundary_cache = None
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
    def boundary_polylines(self) -> Tuple[Polyline, ...]:
        """Contour strokes for the single-colour painter, hull first.

        The retained item cannot read the payload's ``frontiers``, so it needs
        the same precedence as data: one closed ring per hull for a layer the
        hull stage can reduce, and that layer's unchanged engine polylines
        otherwise. Trace polylines are excluded, so their geometry, ids and
        picking are untouched. ``polylines`` keeps the engine geometry, so this
        is a display choice and never a rewrite of the calculation.
        """
        rings = self._layer_hull_rings()
        strokes: list = []
        for layer in self._layers:
            layer_rings = rings.get(layer.layer_id) or ()
            if not layer_rings:
                strokes.extend(
                    polyline
                    for polyline in self._polylines
                    if polyline.kind == "contour"
                    and polyline.layer_id == layer.layer_id
                )
                continue
            for index, ring in enumerate(_ordered_rings(layer_rings)):
                strokes.append(
                    Polyline(
                        f"{layer.layer_id}:hull:{index}",
                        self._calculation_id,
                        layer.layer_id,
                        "contour",
                        tuple(ring) + (tuple(ring[0]),),
                        True,
                        layer.epoch_s,
                    )
                )
        return tuple(strokes)

    def _layer_hull_rings(self) -> Dict[str, tuple]:
        """Hull rings per layer, cached by geometry revision.

        The rings depend on the calculation geometry only, so a style toggle
        reuses them; new geometry either bumps the revision or arrives as a new
        model.
        """
        if self._boundary_cache is not None and (
            self._boundary_cache[0] == self._geometry_revision
        ):
            return self._boundary_cache[1]
        rings = {}
        for layer in self._layers:
            points = [
                point
                for polyline in self._polylines
                if polyline.kind == "contour" and polyline.layer_id == layer.layer_id
                for point in polyline.points
            ]
            rings[layer.layer_id] = layer_hulls(points) if points else ()
        self._boundary_cache = (self._geometry_revision, rings)
        return rings

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

    @property
    def fill_visible(self) -> bool:
        """Nested isochrone band fill; the payload reads it by default."""
        return self._style.fill_visible

    def set_fill_visible(self, visible: bool) -> None:
        if not isinstance(visible, bool):
            raise TypeError("fill visibility must be a boolean")
        if self._style.fill_visible == visible:
            return
        self._style.fill_visible = visible
        self._style_revision += 1

    @property
    def outlines_visible(self) -> bool:
        """Hull boundary strokes; independent of the fill it may accompany."""
        return self._style.outlines_visible

    def set_outlines_visible(self, visible: bool) -> None:
        if not isinstance(visible, bool):
            raise TypeError("outline visibility must be a boolean")
        if self._style.outlines_visible == visible:
            return
        self._style.outlines_visible = visible
        self._style_revision += 1

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


# The time ramp lives in the shared metric registry so the payload, legend and
# tests share one definition; it is re-exported here for existing callers.
from .palettes import (  # noqa: E402 - re-export beside the gradient helpers
    ISOCHRONE_FILL_ALPHA,
    ISOCHRONE_FILL_MAX_CONTOURS,
    ISOCHRONE_FILL_MAX_POINTS,
    ISOCHRONE_HULL_MAX_HULLS,
    ISOCHRONE_HULL_MAX_POINTS,
    ISOCHRONE_TIME_GRADIENT,
    ISOCHRONE_UNIFORM_COLOUR,
    gradient_colour,
    metric_label,
)

from .isohull import hull_area, layer_hulls


from .daynight import day_night_colour, day_night_legend

from .isobands import band_colour, build_bands


def _gradient_colour(position: float) -> list:
    """Sample the registered time gradient; 0 is oldest, 1 is newest."""
    return gradient_colour("layer_time", position)


def isochrone_canvas_coloured(mode: str) -> bool:
    """Whether contours are painted on the canvas rather than the retained item.

    The retained routing item draws one colour, so every mode with its own
    palette is drawn per layer or per point on the canvas and the retained item
    keeps only the trace web. Uniform stays with the retained item, which is
    what keeps its payload and appearance unchanged.
    """
    return normalize_isochrone_colour_by(mode) != DEFAULT_ISOCHRONE_COLOUR_MODE


def isochrone_contours_on_canvas(
    mode: str, fill_visible: bool = False, outlines_visible: bool = False
) -> bool:
    """Whether contours must be painted by the canvas, not the retained item.

    A coloured mode needs one colour per layer or per point, and the nested
    fill or the hull outlines need each shape's own polygon. All are canvas-
    only, and the retained item can draw none of them, so it must suppress its
    contours rather than drawing a single-colour copy underneath.
    """
    return (
        bool(fill_visible) or bool(outlines_visible) or isochrone_canvas_coloured(mode)
    )


def _polygon_area(points) -> float:
    """Shoelace area in projected degrees, used only to order the fills.

    Delegates to the hull helper so the fill ordering and the hull ordering
    cannot disagree about which shape is larger.
    """
    return hull_area(points)


def _ordered_rings(rings) -> list:
    """Hull rings largest first, with a deterministic tie-break.

    The canvas boundary and the retained painter order through this one helper,
    so the two paths cannot disagree about which ring covers which.
    """
    return sorted(rings, key=lambda ring: (-hull_area(ring), ring[0]))


def _hull_boundary_segments(rings) -> list:
    """Closed stroke segments around each hull ring, largest ring first.

    A hull is a closed ring, so the edge from its final vertex back to its first
    is part of the boundary and is emitted too; without it the stroke would stop
    one edge short of closing. Every emitted point is a hull vertex, so the
    boundary invents no geometry beyond the envelope the points already imply.
    """
    ordered = _ordered_rings(rings)
    return [
        [list(first), list(second)]
        for ring in ordered
        for first, second in zip(ring, ring[1:] + ring[:1])
    ]


def _mode_colour(layer, points, mode: str, layer_colours) -> list:
    """Registry RGB for one shape under the active isochrone colour mode.

    Uniform uses the mode's own base colour and time uses the layer's gradient
    position, matching the frontier that encloses the shape. Day/night uses the
    shape's representative point: one shape cannot cross a terminator, so a
    centroid (the mean vertex position) is used because it describes the
    interior the shape actually covers, unlike the first vertex on the edge.
    """
    if mode == "time":
        base = layer_colours.get(layer.layer_id)
        if base is None:
            return []
        return list(base)[:3]
    if mode == "day_night":
        latitude = sum(point[0] for point in points) / len(points)
        longitude = sum(point[1] for point in points) / len(points)
        return list(day_night_colour(layer.epoch_s, (latitude, longitude)))[:3]
    return list(ISOCHRONE_UNIFORM_COLOUR)[:3]


def _fill_colour(layer, points, mode: str, layer_colours) -> list:
    """Band colour for one closed ring, as registry RGBA at fill alpha."""
    rgb = _mode_colour(layer, points, mode, layer_colours)
    return [] if not rgb else rgb + [ISOCHRONE_FILL_ALPHA]


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
    return {"title": metric_label("layer_time"), "entries": entries}


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
    fill_visible: Optional[bool] = None,
    outlines_visible: Optional[bool] = None,
) -> dict:
    """QML-ready payload built only from engine-provided routing geometry.

    Segments are consecutive engine points; no point is joined to another
    polyline, so disconnected contours stay disconnected and a dateline
    crossing stays split. ``frontiers`` mirrors the shape the existing viewport
    isochrone overlay already consumes, so the native layer can replace the
    proximity-joined preview without changing the QML consumer.

    A visible layer whose points the hull stage can reduce strokes its hull
    envelope instead of every engine arm, so the default isochrone reads as the
    reachable boundary rather than a starburst. ``frontiers[].source`` says
    which geometry a frame shows: ``"hull"`` or ``"engine"``. The hull is
    derived from the same points and adds none, and ``layers[].segments`` keeps
    the raw ordered chains, so the calculation data, contour order and
    open/closed state are untouched. A layer the hull stage cannot reduce
    (collinear, fewer than three distinct points, or a ring wider than the
    antimeridian guard) keeps its engine segments rather than being given an
    invented chord, and its ``source`` stays ``"engine"``.

    ``isochrone_colour_by="time"`` adds a gradient colour to each visible layer
    and frontier plus a legend keyed to the layers' recorded epochs.
    ``"day_night"`` colours each frontier segment from its leading point, so a
    contour that crosses the terminator shows both, plus a categorical legend.
    The uniform payload is unchanged.

    ``fill_visible=True`` adds ``bands``: one closed ring per paired contour
    between consecutive visible layers that both lack a hull. A visible layer
    with a hull is filled by that hull and splits the band run, so the layers
    on either side of it are never paired across the gap. Already resampled
    and direction-aligned, so the fill is continuous whether or not a contour
    is closed.
    Bands are the primary fill; ``fills`` stays as the fallback for closed
    contours of a layer with no band coverage, for example a single visible
    layer. Only engine contours marked closed reach ``fills``, because an open
    interior would leak across land. Both arrays are omitted entirely when the
    fill is off, so the default payload keeps its shape. A dense result is
    capped, and ``bands_note`` discloses how many bands were left out.

    Per-layer hulls supersede the raw chains for layers that can produce one.
    Every visible layer's contour points are clustered and each cluster is
    reduced to a convex hull, so the solver's all-heading expansion no longer
    reads as a starburst; the hull is also the default frontier stroke, which is
    why it is built even with both toggles off. ``outlines`` carries one closed
    ring per hull, largest area first, for the painter to stroke, and is emitted
    only when the outlines toggle asked for it. ``fills`` carries the same rings
    as closed shapes when the fill is on. An outline strokes exactly the ring
    the frontier boundary already strokes, so turning the toggle on emphasises
    that boundary instead of painting a second shape.

    Precedence, highest first: hull fill/outline for a layer with a hull, then
    the gated band strip for a layer with no hull, then the closed-contour
    fallback for a layer with neither. A band never covers a hulled layer, and
    a contour fill never duplicates one, so no shape paints the same area
    twice. Bands therefore remain a fallback for the degenerate layers only.

    ``outlines_visible=True`` emits the hull rings even with the fill off, so
    the two toggles stay independent: Fill paints hull interiors, Outlines
    strokes hull boundaries. Both are omitted unless the contour eye is on.
    Hull caps are separate from the closed-contour caps; a capped payload
    discloses it in ``fills_note`` or ``outlines_note``.

    The flag is owned by the model, exactly like contour and trace visibility,
    so the payload follows a model toggle with no geometry rebuild. ``None``
    reads that model flag; a caller that already resolved the flag elsewhere may
    still pass an explicit boolean, which wins for this call only.
    """

    if not isinstance(model, RoutingOverlayModel):
        raise TypeError("model must be a RoutingOverlayModel")
    if fill_visible is None:
        fill_visible = model.fill_visible
    else:
        fill_visible = bool(fill_visible)
    if outlines_visible is None:
        outlines_visible = model.outlines_visible
    else:
        outlines_visible = bool(outlines_visible)
    hulls_visible = fill_visible or outlines_visible
    mode = normalize_isochrone_colour_by(isochrone_colour_by)
    visible_layers = [
        layer for layer in model.layers if model.contour_visible(layer.layer_id)
    ]
    layer_colours = {}
    if mode == "time":
        layer_colours = isochrone_time_colours(visible_layers)
    per_point = mode == "day_night"
    layers = []
    frontiers = []
    fill_shapes = []
    band_inputs = []
    hull_shapes = []
    # The hull stage is the same one ``boundary_polylines`` uses, so reuse its
    # revision-keyed cache instead of recomputing every hull each frame.
    hull_rings = model._layer_hull_rings()
    band_runs = []
    for layer in visible_layers:
        segments = []
        closed = []
        band_contours = []
        for polyline in model.polylines:
            if polyline.kind != "contour" or polyline.layer_id != layer.layer_id:
                continue
            segments.extend(
                [list(first), list(second)]
                for first, second in zip(polyline.points, polyline.points[1:])
            )
            closed.append(polyline.closed)
            if fill_visible and len(polyline.points) >= 2:
                band_contours.append(polyline.points)
            if fill_visible and polyline.closed and len(polyline.points) >= 3:
                fill_shapes.append((layer, polyline.points, polyline.object_id))
        # Every visible layer is hulled, not only a filled or outlined one: the
        # envelope is also the default boundary stroke. The hull is a display
        # approximation of the engine's ordered chains, which the layer payload
        # keeps unchanged.
        layer_rings = hull_rings.get(layer.layer_id) or ()
        if hulls_visible and layer_rings:
            hull_shapes.extend((layer, ring) for ring in layer_rings)
        # A hulled layer is filled by its hull, so it must not also be banded:
        # bands stay the fallback for layers the hull stage could not reduce.
        # A hulled layer also ends a run: the layers on either side are not
        # consecutive visible layers, so their contours must not be paired
        # across the layer the fill skipped.
        if fill_visible and not layer_rings:
            band_inputs.append(
                {
                    "layer_id": layer.layer_id,
                    "epoch_s": layer.epoch_s,
                    "contours": band_contours,
                }
            )
        elif band_inputs:
            band_runs.append(band_inputs)
            band_inputs = []
        layer_payload = {
            "id": layer.layer_id,
            "time": layer.epoch_s,
            "reverse": layer.reverse,
            "segments": segments,
            "contour_count": len(closed),
            "closed": closed,
        }
        boundary = _hull_boundary_segments(layer_rings)
        frontier_payload = {
            # A hulled layer is stroked by its envelope; a hull-less layer keeps
            # the engine chains rather than receiving an invented chord.
            "segments": boundary or segments,
            "source": "hull" if boundary else "engine",
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
        if per_point:
            # A layer spans one instant but many longitudes, so the frontier
            # carries one colour per segment instead of one per layer.
            frontier_payload["segment_colours"] = [
                list(day_night_colour(layer.epoch_s, segment[0]))
                for segment in frontier_payload["segments"]
            ]
        layers.append(layer_payload)
        frontiers.append(frontier_payload)
    if band_inputs:
        band_runs.append(band_inputs)
    bands = []
    bands_note = None
    banded_layers = set()
    if fill_visible:
        rings_total = 0
        capped_total = 0
        skipped_total = 0
        for run in band_runs:
            # A single layer has no frontier to pair with, and a run never
            # contains the hulled layer that split it.
            if len(run) < 2:
                continue
            built = build_bands(run)
            rings_total += built["rings"]
            capped_total += built["capped_rings"]
            skipped_total += built["skipped"]
            for band in built["bands"]:
                colour = band_colour(band, mode, layer_colours)
                if not colour:
                    continue
                bands.append(
                    {
                        **band,
                        "points": [list(point) for point in band["points"]],
                        "colour": colour,
                    }
                )
                banded_layers.add(band["layer_id"])
                banded_layers.add(band["prev_layer_id"])
        if capped_total:
            bands_note = f"{len(bands)} of {rings_total} layer bands drawn (capped)"
        elif bands and skipped_total:
            # The gate dropped a pair that did not look like the same front;
            # say so rather than leaving an unexplained gap in the fill.
            band_word = "band" if len(bands) == 1 else "bands"
            pair_word = "pair" if skipped_total == 1 else "pairs"
            bands_note = (
                f"{len(bands)} {band_word} ({skipped_total} {pair_word} skipped)"
            )
    outlines = []
    hull_fills = []
    hull_note = None
    if hull_shapes:
        # Largest first so each smaller hull covers its parent's interior and
        # leaves only the band between consecutive layers visible. The layer id
        # breaks an equal-area tie deterministically.
        ordered = sorted(
            hull_shapes, key=lambda item: (-hull_area(item[1]), item[0].layer_id)
        )
        kept = []
        used = 0
        for layer, ring in ordered:
            if len(kept) >= ISOCHRONE_HULL_MAX_HULLS:
                break
            # The largest hull is always kept: an over-budget single hull is
            # better than an empty layer the user asked to fill or outline.
            if kept and used + len(ring) > ISOCHRONE_HULL_MAX_POINTS:
                break
            kept.append((layer, ring))
            used += len(ring)
        if len(kept) < len(ordered):
            hull_note = (
                f"{len(kept)} of {len(ordered)} isochrone hulls drawn "
                "(largest first)"
            )
        for layer, ring in kept:
            colour = _mode_colour(layer, ring, mode, layer_colours)
            if not colour:
                continue
            points = [list(point) for point in ring]
            # Outlines are stroked only for the toggle that asked for them; the
            # fill is a separate toggle and must not smuggle the stroke in.
            if outlines_visible:
                outlines.append(
                    {
                        "layer_id": layer.layer_id,
                        "epoch_s": layer.epoch_s,
                        "points": points,
                        "colour": list(colour),
                        "area": hull_area(ring),
                    }
                )
            if fill_visible:
                hull_fills.append(
                    {
                        "layer_id": layer.layer_id,
                        "points": points,
                        "colour": list(colour) + [ISOCHRONE_FILL_ALPHA],
                        "closed": True,
                    }
                )
    hulled_layers = {layer.layer_id for layer, _ring in hull_shapes}
    fills = []
    legacy_fills = []
    if fill_visible:
        # A layer covered by its hull or by bands is already filled, so its
        # closed contours would paint the same area twice. Closed-contour fills
        # are the last fallback, for a layer neither stage could cover.
        fill_shapes = [
            shape
            for shape in fill_shapes
            if shape[0].layer_id not in banded_layers
            and shape[0].layer_id not in hulled_layers
        ]
        # Largest first so each smaller contour covers its parent's interior,
        # leaving only the annular band between consecutive frontiers visible.
        # Object id breaks an equal-area tie deterministically.
        fill_shapes.sort(key=lambda item: (-_polygon_area(item[1]), item[2]))
        fill_points = 0
        for layer, points, _object_id in fill_shapes:
            if len(legacy_fills) >= ISOCHRONE_FILL_MAX_CONTOURS:
                break
            # The largest contour is always kept: an over-budget single fill is
            # better than an empty layer the user asked to fill.
            if legacy_fills and fill_points + len(points) > ISOCHRONE_FILL_MAX_POINTS:
                break
            colour = _fill_colour(layer, points, mode, layer_colours)
            if not colour:
                continue
            legacy_fills.append(
                {
                    "layer_id": layer.layer_id,
                    "points": [list(point) for point in points],
                    "colour": colour,
                    "closed": True,
                }
            )
            fill_points += len(points)
    fills = hull_fills + legacy_fills
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
    if fills:
        payload["fills"] = fills
        total_closed = len(fill_shapes)
        if hull_note:
            payload["fills_note"] = hull_note
        elif total_closed > len(legacy_fills):
            # A dense result is capped so the frame stays responsive; the
            # caption states what was left out instead of silently drawing a
            # partial picture that looks complete.
            payload["fills_note"] = (
                f"{len(legacy_fills)} of {total_closed} closed contours filled "
                "(largest first)"
            )
    if outlines:
        payload["outlines"] = outlines
        if hull_note and not fill_visible:
            payload["outlines_note"] = hull_note
    if bands:
        payload["bands"] = bands
    if bands_note:
        payload["bands_note"] = bands_note
    if mode == "time":
        # Every schema-v1 layer carries a recorded epoch, so time is always
        # representable and is never downgraded to a uniform colour.
        payload["isochrone_colour_by"] = "time"
        payload["legend"] = isochrone_time_legend(visible_layers, layer_colours)
    elif mode == "day_night":
        payload["isochrone_colour_by"] = "day_night"
        payload["legend"] = day_night_legend()
    return payload
