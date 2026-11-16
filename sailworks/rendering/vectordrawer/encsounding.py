"""Backend-neutral SOUNDG sounding labels for the opt-in GPU scene path.

The Cairo drawer renders soundings in
:meth:`S57ChartDrawer._render_soundings`: at display scale 100 or shallower it
formats each finite point's depth as ``f"{depth:.1f}"``, culls points outside
the viewport, and keeps only the first point in every 32x16 pixel screen box.
The text is drawn at ``(x + 2, y + 2)`` with the baseline at that point.

This module reproduces that *selection*, not the rasterisation.  It is
Qt-free and takes already-projected screen coordinates, so the GPU adapter can
rerun the gate and the box dedupe once per camera from cached source points:
the result must not be baked into cached geometry.  :class:`SoundingLabel` is
the layout contract a glyph atlas renders.

Source extraction parses the cell's SOUNDG WKB directly (a Z multipoint) and
stays dependency-free apart from NumPy.  The mesh slice still reports sounding
text as unsupported; this slice is the text half.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

import numpy as np

#: The drawer hides soundings when the display scale exceeds 100.
SCALE_LIMIT = 100.0

#: Screen-space dedupe box, in logical pixels, matching the Cairo path.
BOX_WIDTH = 32
BOX_HEIGHT = 16

#: The Cairo path draws at ``(x + 2, y + 2)``, baseline-left.
LABEL_OFFSET = 2.0
ANCHOR = "baseline-left"

SOUNDING_CLASS = "SOUNDG"

#: Shared empty source points; frozen dataclasses may hold it as a default.
EMPTY_SOUNDINGS = np.empty((0, 3), dtype=np.float64)


@dataclass(frozen=True)
class SoundingLabel:
    """One laid-out sounding: baseline-start position, text and anchor."""

    x: float
    y: float
    text: str
    anchor: str = ANCHOR


def format_depth(depth) -> str:
    """The drawer's sounding format, exactly ``f"{depth:.1f}"``."""
    return f"{float(depth):.1f}"


def collect_soundings(
    points,
    *,
    scale=None,
    width,
    height,
    limit: float = SCALE_LIMIT,
    boxes=None,
) -> tuple[SoundingLabel, ...]:
    """Select sounding labels from projected screen points, in source order.

    ``points`` is an iterable of ``(x, y, depth)`` in screen coordinates, in
    source order.  ``scale`` is the camera's display scale; ``None`` applies no
    gate, the same convention as :func:`encmesh.build_scene_mesh`.  The gate,
    the half-open viewport cull and the first-wins 32x16 box dedupe match
    ``S57ChartDrawer._render_soundings``.

    ``boxes`` lets a caller supply the dedupe set (for example to share it with
    a glyph layer); it is mutated.  Omitting it gives a per-call set, which is
    what a per-camera call needs.
    """
    if scale is not None and float(scale) > float(limit):
        return ()
    width = float(width)
    height = float(height)
    seen = set() if boxes is None else boxes
    labels = []
    for x, y, depth in points:
        x = float(x)
        y = float(y)
        depth = float(depth)
        if not (0.0 <= x < width and 0.0 <= y < height):
            continue
        if not math.isfinite(depth):
            continue
        box = (int(x // BOX_WIDTH), int(y // BOX_HEIGHT))
        if box in seen:
            continue
        seen.add(box)
        labels.append(
            SoundingLabel(
                x=x + LABEL_OFFSET,
                y=y + LABEL_OFFSET,
                text=format_depth(depth),
                anchor=ANCHOR,
            )
        )
    return tuple(labels)


def _dimensions(geometry_type: int) -> tuple[int, bool]:
    """``(base type, has Z)`` for ISO or EWKB geometry type words."""
    has_z = bool(geometry_type & 0x80000000)
    base = geometry_type & 0x0FFFFFFF
    if base >= 1000:
        dimensions, base = divmod(base, 1000)
        has_z = has_z or dimensions in (1, 3)
    return base, has_z


def _read_wkb_points(wkb: bytes, offset: int = 0, inherited_z: bool = False):
    """Read the Z points of one Point/MultiPoint WKB at ``offset``.

    Returns ``(points, next_offset)``.  Handles ISO types (1001/1004) and the
    GDAL/OGR EWKB Z flag; a child point without its own flag inherits the
    parent multipoint's dimensionality.
    """
    if offset >= len(wkb):
        raise ValueError("truncated WKB: missing byte order")
    byte_order = wkb[offset]
    if byte_order == 1:
        endian = "<"
    elif byte_order == 0:
        endian = ">"
    else:
        raise ValueError(f"invalid WKB byte order {byte_order!r}")
    offset += 1
    if offset + 4 > len(wkb):
        raise ValueError("truncated WKB: missing geometry type")
    (geometry_type,) = struct.unpack_from(endian + "I", wkb, offset)
    offset += 4
    if geometry_type & 0x20000000:  # EWKB SRID flag carries a following int32
        offset += 4

    base, has_z = _dimensions(geometry_type)
    has_z = has_z or inherited_z

    if base == 1:  # Point
        count = 3 if has_z else 2
        if offset + 8 * count > len(wkb):
            raise ValueError("truncated WKB point")
        values = struct.unpack_from(endian + "d" * count, wkb, offset)
        offset += 8 * count
        if not has_z:
            return [], offset
        return [(values[0], values[1], values[2])], offset

    if base == 4:  # MultiPoint
        if offset + 4 > len(wkb):
            raise ValueError("truncated WKB multipoint")
        (count,) = struct.unpack_from(endian + "I", wkb, offset)
        offset += 4
        points = []
        for _ in range(count):
            child_points, offset = _read_wkb_points(wkb, offset, inherited_z=has_z)
            points.extend(child_points)
        return points, offset

    return [], offset


def wkb_points(wkb) -> tuple[tuple[float, float, float], ...]:
    """The ``(x, y, z)`` points of a Point or MultiPoint WKB blob.

    A two-dimensional input has no depth and yields no soundings; it is not
    invented as zero.
    """
    points, _ = _read_wkb_points(bytes(wkb))
    return tuple(points)


def soundings_from_records(records) -> np.ndarray:
    """Collect source ``(x, y, depth)`` from SOUNDG records, in record order.

    Unreadable sounding WKB is skipped rather than aborting the whole scene;
    the mesh slice already reports sounding text separately, and a single bad
    blob must not blank every other cell's labels.
    """
    chunks: list[tuple[float, float, float]] = []
    for record in records:
        if getattr(record, "source_class", None) != SOUNDING_CLASS:
            continue
        wkb = getattr(record, "wkb", None)
        if not wkb:
            continue
        try:
            points = wkb_points(wkb)
        except (ValueError, struct.error):
            continue
        chunks.extend(points)
    if not chunks:
        return EMPTY_SOUNDINGS
    return np.asarray(chunks, dtype=np.float64).reshape(-1, 3)


def project_points(points, projection, origin=None) -> np.ndarray:
    """Project source ``(N, 3)`` sounding points to ``(N, 2)`` screen points.

    ``projection`` receives float64 source coordinates after ``origin`` is
    subtracted, exactly like the mesh adapter, so screen-space dedupe sees the
    same camera the triangles do.  A ``None`` projection returns the input
    coordinates unchanged (a backend that already works in screen space).
    Depth is deliberately not projected.
    """
    array = np.asarray(points, dtype=np.float64)
    if array.size == 0:
        return np.empty((0, 2), dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 3:
        raise ValueError("sounding points must be an (N, 3) array")
    source = array[:, :2]
    if origin is not None:
        source = source - np.asarray(origin, dtype=np.float64).reshape(1, 2)
    if projection is None:
        return np.asarray(source, dtype=np.float64)
    screen = np.asarray(projection(source), dtype=np.float64)
    if screen.shape != (array.shape[0], 2):
        raise ValueError("projection must return an (N, 2) array")
    return screen
