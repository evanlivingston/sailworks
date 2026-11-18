"""Thick-line geometry for the ENC mesh slice (split out of :mod:`encmesh`).

The stroke mesh is deliberately backend-neutral: plain ``(N, 2)`` triangle
soups in ``float64`` source coordinates.  Two rules live here because they are
easy to get wrong in the builder:

* Width is never assumed screen-constant.  A source-unit ``stroke_width`` is a
  caller-chosen approximation; a caller that needs a constant screen width
  passes ``stroke_pixels`` plus the same ``projection`` the backend will use,
  and each segment's source-unit width is derived from that projection.
* Region clipping happens *after* width expansion.  A segment that crosses the
  region edge is clipped as an expanded quad, so its width cannot bleed
  outside the region and cannot be silently redrawn narrower.

Nothing here is wired into the default renderer or the viewport.
"""

from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import GeometryCollection, LineString, MultiLineString, Polygon

_TRIANGLE_TOLERANCE = 1e-6


class UnsupportedGeometry(ValueError):
    """A feature this slice cannot draw correctly; never silently approximated."""


def _lines(geometry) -> tuple:
    """Lineal parts of a geometry, in source order."""
    if geometry is None or geometry.is_empty:
        return ()
    if isinstance(geometry, LineString):
        return (geometry,)
    if isinstance(geometry, MultiLineString):
        return tuple(geometry.geoms)
    if hasattr(geometry, "geoms"):
        parts = []
        for part in geometry.geoms:
            parts.extend(_lines(part))
        return tuple(parts)
    return ()


def _triangulate_piece(piece) -> np.ndarray:
    """Exact constrained triangulation of one clipped stroke piece.

    The triangles are extracted and filtered with one vectorised pass instead of
    a per-triangle geometry loop: a clipped stroke quad yields few triangles, so
    per-object Python and GEOS access used to dominate the clip stage.
    """
    target = piece if shapely.is_valid(piece) else shapely.make_valid(piece)
    polygons = []
    if isinstance(target, Polygon):
        polygons = [target]
    elif hasattr(target, "geoms"):
        polygons = [part for part in target.geoms if isinstance(part, Polygon)]
    polygons = [part for part in polygons if not part.is_empty]
    area = sum(part.area for part in polygons)
    if area <= 0.0:
        return np.empty((0, 2), dtype=np.float64)
    if not hasattr(shapely, "constrained_delaunay_triangles"):
        raise UnsupportedGeometry("GEOS lacks constrained Delaunay triangulation")
    triangulation = shapely.constrained_delaunay_triangles(
        polygons[0] if len(polygons) == 1 else GeometryCollection(polygons)
    )
    triangles = np.empty((0, 3, 2), dtype=np.float64)
    if not triangulation.is_empty:
        coordinates = shapely.get_coordinates(triangulation)
        # GEOS polygon rings are closed, so every triangle contributes four
        # pairs; anything else is not a triangle soup and covers nothing.
        if coordinates.shape[0] % 4 == 0:
            triangles = coordinates.reshape(-1, 4, 2)[:, :3]

    a = triangles[:, 0]
    b = triangles[:, 1]
    c = triangles[:, 2]
    doubled = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (
        c[:, 0] - a[:, 0]
    )
    keep = doubled != 0.0  # degenerate sliver, adds no area
    if keep.any():
        centroid_x = (a[:, 0] + b[:, 0] + c[:, 0]) / 3.0
        centroid_y = (a[:, 1] + b[:, 1] + c[:, 1]) / 3.0
        keep &= shapely.contains_xy(target, centroid_x, centroid_y)
    kept = triangles[keep]
    covered = float(np.abs(doubled[keep]).sum()) / 2.0
    if abs(covered - area) > _TRIANGLE_TOLERANCE * max(1.0, area):
        raise UnsupportedGeometry(
            "clipped stroke triangulation does not match the piece area "
            f"({covered:.6g} of {area:.6g})"
        )
    return kept.reshape(-1, 2)


def _polygons_of(geometry) -> tuple:
    if isinstance(geometry, Polygon):
        return (geometry,)
    if hasattr(geometry, "geoms"):
        parts = []
        for part in geometry.geoms:
            parts.extend(_polygons_of(part))
        return tuple(parts)
    return ()


_WIDTH_SAMPLES = 4  # scale samples per polyline, spread along its arc length


def _sampled_widths(coordinates, lengths, stroke_pixels, projection, samples=None):
    """Width per segment from a few projected sample segments.

    The exact width is ``stroke_pixels * length / hypot(dlon * sx, dlat * sy)``
    where ``sx`` and ``sy`` are the projection's screen scale per degree of
    longitude and of latitude.  Because a degree of longitude is shorter than a
    degree of latitude, that ratio depends on a segment's heading as well as on
    its position, so one per-half scale cannot reproduce it.  Instead the
    polyline's own segments are sampled at four points along its arc length and
    ``sx``, ``sy`` are fitted from them by least squares -- exact for a polyline
    of a single heading, and exact per axis for an axis-aligned sample.

    ``None`` means "no usable sample" (fewer segments than samples, a sample
    collapsed in screen space, or a fit with no positive solution); the caller
    then projects every segment exactly.
    """
    count = lengths.shape[0]
    if count < _WIDTH_SAMPLES:
        return None
    cumulative = np.concatenate((np.zeros(1), np.cumsum(lengths)))
    total = float(cumulative[-1])
    if total <= 0.0:
        return None
    picks = np.array(
        [
            min(
                int(np.searchsorted(cumulative, total * fraction, side="right")) - 1,
                count - 1,
            )
            for fraction in (1 / 8, 3 / 8, 5 / 8, 7 / 8)
        ],
        dtype=np.intp,
    )
    indices = np.stack((picks, picks + 1), axis=1).ravel()
    if samples is None:
        samples = np.asarray(projection(coordinates[indices]), dtype=np.float64)
    else:
        samples = np.asarray(samples[indices], dtype=np.float64)
    if samples.shape != (indices.size, 2):
        raise ValueError("projection must preserve the (N, 2) shape")
    span = coordinates[indices[1::2]] - coordinates[indices[::2]]
    screen = samples[1::2] - samples[::2]
    screen_squared = screen[:, 0] ** 2 + screen[:, 1] ** 2
    if not np.all(screen_squared > 0.0):
        return None
    # screen^2 = dlon^2 * sx^2 + dlat^2 * sy^2 is linear in the squared scales.
    lon_squared = span[:, 0] ** 2
    lat_squared = span[:, 1] ** 2
    a11 = float(np.sum(lon_squared**2))
    a12 = float(np.sum(lon_squared * lat_squared))
    a22 = float(np.sum(lat_squared**2))
    b1 = float(np.sum(lon_squared * screen_squared))
    b2 = float(np.sum(lat_squared * screen_squared))
    determinant = a11 * a22 - a12 * a12
    if determinant > 0.0:
        lon_scale_squared = (a22 * b1 - a12 * b2) / determinant
        lat_scale_squared = (a11 * b2 - a12 * b1) / determinant
    else:
        # Every sample has the same heading, so one isotropic scale is all the
        # samples can determine; it is exact for a polyline of that heading.
        isotropic = float(np.sum(screen_squared)) / float(
            np.sum(lon_squared + lat_squared)
        )
        lon_scale_squared = lat_scale_squared = isotropic
    if not (lon_scale_squared > 0.0 and lat_scale_squared > 0.0):
        return None
    lon_scale = float(np.sqrt(lon_scale_squared))
    lat_scale = float(np.sqrt(lat_scale_squared))
    screen_lengths = np.hypot(
        (coordinates[1:, 0] - coordinates[:-1, 0]) * lon_scale,
        (coordinates[1:, 1] - coordinates[:-1, 1]) * lat_scale,
    )
    if not np.all(screen_lengths > 0.0):
        return None
    return float(stroke_pixels) * lengths / screen_lengths


def _exact_widths(coordinates, lengths, stroke_pixels, projection, samples=None):
    """Source-unit width per segment, projected exactly on every segment.

    The scale is measured on the segment itself, so the width is exact for the
    viewport projection.  It costs one projection of every vertex, which is why
    the sampled fit is tried first; this is the fallback when that fit cannot be
    made (short polylines, a segment collapsed in screen space).
    """
    if coordinates.shape[0] < 2:
        return np.zeros(0, dtype=np.float64)
    if samples is None:
        samples = np.asarray(projection(coordinates), dtype=np.float64)
    else:
        samples = np.asarray(samples, dtype=np.float64)
    if samples.shape != (coordinates.shape[0], 2):
        raise ValueError("projection must preserve the (N, 2) shape")
    screen = samples[1:] - samples[:-1]
    screen_lengths = np.hypot(screen[:, 0], screen[:, 1])
    if not np.all(screen_lengths > 0.0):
        raise ValueError("projection collapses a segment to a point")
    return float(stroke_pixels) * lengths / screen_lengths


def _stroke_widths(
    coordinates, lengths, stroke_width, stroke_pixels, projection, samples=None
):
    """Source-unit width per segment, or a screen-constant width via projection.

    With ``stroke_pixels`` the exact answer divides the pixel width by the
    projection's local scale for every segment, which costs a projection of
    every vertex and dominated the cold stroke stages.  So a polyline is
    projected at a few sample segments spread along its arc length and the
    per-axis screen scales are fitted from them by least squares; the fit is
    exact for a polyline of a single heading and exact per axis for
    axis-aligned samples.  A polyline the fit cannot be made from -- fewer
    segments than samples, a sample collapsed in screen space, or no positive
    solution -- falls back to the exact per-segment projection, so the
    approximation never changes the reported failure mode.
    """
    if stroke_pixels is None:
        return np.full(lengths.shape[0], float(stroke_width), dtype=np.float64)
    widths = _sampled_widths(coordinates, lengths, stroke_pixels, projection, samples)
    if widths is not None:
        return widths
    return _exact_widths(coordinates, lengths, stroke_pixels, projection, samples)


def _prepare_region(region):
    """Region prepared in place for repeated ``covers`` tests, or ``None``.

    ``shapely.prepare`` prepares the geometry in place and returns a truthiness
    flag (Shapely >= 2.2) or ``None`` (Shapely 2.0); either way ``region``
    itself carries the index afterwards and ``covers`` stays exact.  When the
    build cannot prepare a geometry, or the function is absent, ``None`` is
    returned so the caller keeps the original clip path instead of relying on
    an acceleration that is not there.
    """
    prepare = getattr(shapely, "prepare", None)
    if prepare is None:
        return None
    try:
        prepare(region)
    except Exception:  # noqa: BLE001 - an unavailable index must not change output
        return None
    # If a build prepares nothing, the plain geometry still answers ``covers``
    # exactly, just without the cached index: correctness never depends on it.
    return region


def _clip_stroke_triangles(triangles, region, quad_count, prepared=None) -> np.ndarray:
    """Clip expanded stroke quads against ``region``, then re-triangulate them.

    Clipping happens on the expanded quad, so the result can never extend
    outside ``region`` even when the source segment crosses its edge.

    ``prepared`` is the region prepared once per stroke call, so the expanded
    quads can be tested with ``covers`` in one vectorised predicate.  A covered
    quad lies wholly inside the region: its existing two-triangle soup already
    covers exactly the clipped area, so the intersection and the
    re-triangulation are skipped.  ``covers`` rather than ``contains`` is used
    so a quad that only touches the region boundary takes the same path; the
    clipped area is identical either way.
    """
    if quad_count == 0:
        return np.empty((0, 2), dtype=np.float64)
    covered = None
    if prepared is not None:
        coordinates = np.asarray(triangles, dtype=np.float64)
        bounds = shapely.box(
            float(coordinates[:, 0].min()),
            float(coordinates[:, 1].min()),
            float(coordinates[:, 0].max()),
            float(coordinates[:, 1].max()),
        )
        try:
            # Every quad lies inside the stroke's own bounding box, so a
            # covered box proves the whole line interior in one predicate.
            if shapely.covers(region, bounds):
                return np.array(coordinates, copy=True)
            corners = coordinates.reshape(quad_count, 6, 2)[:, [0, 1, 2, 5], :]
            covered = np.asarray(
                shapely.covers(region, shapely.polygons(corners)), dtype=bool
            )
        except Exception:  # noqa: BLE001 - the index is an optimisation only
            covered = None
    if covered is not None and covered.all():
        return np.array(triangles, dtype=np.float64, copy=True)

    clipped = []
    for index in range(quad_count):
        quad = triangles[index * 6 : index * 6 + 6]
        if covered is not None and covered[index]:
            clipped.append(quad)
            continue
        polygon = Polygon([quad[0], quad[1], quad[2], quad[5]])
        if not polygon.is_valid:
            polygon = shapely.make_valid(polygon)
        if polygon.is_empty or polygon.area <= 0.0:
            continue
        piece = polygon.intersection(region)
        if piece.is_empty:
            continue
        try:
            vertices = _triangulate_piece(piece)
        except UnsupportedGeometry:
            # A negligible sliver at the region edge adds no area; anything
            # larger is a real failure and must not be dropped silently.
            if piece.area > 1e-9:
                raise
            continue
        if vertices.shape[0]:
            clipped.append(vertices)
    if not clipped:
        return np.empty((0, 2), dtype=np.float64)
    return np.concatenate(clipped)


def _dashed_parts(geometry, dash, projection):
    """Split every lineal part into the 'on' runs of a screen-space dash.

    ``dash`` is ``(on, off)`` in pixels.  The pattern is measured on the
    projected line, so it stays the same on screen at any zoom, and the cuts
    are mapped back to source distance through the line's own length ratio.

    Returns a tuple of ``(N, 2)`` coordinate arrays, or ``None`` when the dash
    pattern is inert (the caller then uses the geometry's own lines).  The run
    cut points come from ``line_interpolate_point`` and the interior vertices
    from the source coordinate sequence, so every emitted coordinate is the
    same value ``shapely.ops.substring`` produced: the dash geometry, and the
    triangles built from it, are unchanged.  Building coordinate arrays instead
    of one ``LineString`` per run avoids thousands of per-run Python/GEOS
    substring calls on a dense outline, which dominated the stroke stage.
    """
    on, off = (float(dash[0]), float(dash[1]))
    if on <= 0.0 or off < 0.0:
        return None
    lines = []
    starts = []
    ends = []
    coordinates = []
    cumulative = []
    for line in _lines(geometry):
        points = shapely.get_coordinates(line)
        if points.shape[0] < 2:
            continue
        screen = np.asarray(projection(points), dtype=np.float64)
        screen_length = float(np.hypot(*np.diff(screen, axis=0).T).sum())
        source_length = float(line.length)
        if screen_length <= 0.0 or source_length <= 0.0:
            continue
        ratio = source_length / screen_length
        segment_lengths = np.hypot(
            points[1:, 0] - points[:-1, 0], points[1:, 1] - points[:-1, 1]
        )
        distances = np.concatenate((np.zeros(1), np.cumsum(segment_lengths)))
        position = 0.0
        while position < source_length:
            end = min(position + on * ratio, source_length)
            lines.append(line)
            starts.append(position)
            ends.append(end)
            coordinates.append(points)
            cumulative.append(distances)
            position = end + off * ratio
    if not lines:
        return ()
    source = np.asarray(lines, dtype=object)
    start_xy = shapely.get_coordinates(
        shapely.line_interpolate_point(source, np.asarray(starts, dtype=np.float64))
    )
    end_xy = shapely.get_coordinates(
        shapely.line_interpolate_point(source, np.asarray(ends, dtype=np.float64))
    )
    parts = []
    for index in range(len(lines)):
        points = coordinates[index]
        distances = cumulative[index]
        inner = points[(distances > starts[index]) & (distances < ends[index])]
        piece = np.concatenate(
            (start_xy[index][None, :], inner, end_xy[index][None, :]), axis=0
        )
        # A cut exactly on a vertex can repeat it; the substring result holds
        # each vertex once, and the repeated point would only make a
        # zero-length segment the width expansion already drops.
        keep = np.ones(piece.shape[0], dtype=bool)
        keep[1:] = np.any(piece[1:] != piece[:-1], axis=1)
        piece = piece[keep]
        if piece.shape[0] >= 2:
            parts.append(piece)
    return tuple(parts)


def stroke_lines(
    geometry,
    stroke_width=None,
    *,
    stroke_pixels=None,
    projection=None,
    region=None,
    dash=None,
) -> np.ndarray:
    """Thick-line triangles for every lineal part, as an ``(N, 2)`` triangle list.

    Either ``stroke_width`` (source coordinate units, a caller-chosen
    approximation) or ``stroke_pixels`` plus ``projection`` (screen-constant
    width, sized per segment from the projection) must be supplied.  Each
    segment becomes two triangles so the result is a pure ``DrawTriangles``
    soup with no index buffer.

    When ``region`` is given, each expanded quad is intersected with it after
    the width expansion, so a stroke crossing the edge of the region is
    clipped and cannot bleed outside it.  Coordinates remain ``float64``.
    """
    if (stroke_width is None) == (stroke_pixels is None):
        raise ValueError("supply exactly one of stroke_width or stroke_pixels")
    if stroke_pixels is not None:
        if projection is None:
            raise ValueError("stroke_pixels requires a projection")
        if not float(stroke_pixels) > 0.0:
            raise ValueError("stroke_pixels must be positive")
    elif not float(stroke_width) > 0.0:
        raise ValueError("stroke_width must be positive source units")

    parts = None
    if dash is not None and projection is not None:
        parts = _dashed_parts(geometry, dash, projection)
    if parts is None:
        parts = tuple(shapely.get_coordinates(line) for line in _lines(geometry))

    clip_geometry = None
    prepared_region = None
    if region is not None:
        clip_geometry = (
            region if shapely.is_valid(region) else shapely.make_valid(region)
        )
        prepared_region = _prepare_region(clip_geometry)

    chunks = []
    kept_parts = []
    for coordinates in parts:
        if coordinates.shape[0] < 2:
            continue
        delta = coordinates[1:] - coordinates[:-1]
        lengths = np.hypot(delta[:, 0], delta[:, 1])
        keep = lengths > 0.0
        if not keep.any():
            continue
        # Keep the vertex sequence of the non-degenerate segments: vertex 0
        # always, then the end of every kept segment.  A dropped zero-length
        # segment contributes no geometry, so the polyline is unchanged.
        points = coordinates[np.concatenate((np.ones(1, dtype=bool), keep))]
        kept_parts.append((points, lengths[keep]))
    if not kept_parts:
        return np.empty((0, 2), dtype=np.float64)

    # The width of a screen-constant stroke is measured on the projected
    # polyline, so every part needs its vertices projected.  One projection of
    # all parts replaces one numpy round trip per part; the projection is
    # elementwise, so each part reads the same values the per-part call
    # produced.  Parts are sliced back out in the same order.
    projected = None
    if stroke_pixels is not None:
        stacked = np.concatenate([points for points, _ in kept_parts], axis=0)
        projected = np.asarray(projection(stacked), dtype=np.float64)
        if projected.shape != stacked.shape:
            raise ValueError("projection must preserve the (N, 2) shape")

    offset = 0
    for points, lengths in kept_parts:
        samples = None
        if projected is not None:
            samples = projected[offset : offset + points.shape[0]]
            offset += points.shape[0]
        starts = points[:-1]
        ends = points[1:]
        delta = ends - starts
        widths = _stroke_widths(
            points, lengths, stroke_width, stroke_pixels, projection, samples
        )
        unit = delta / lengths[:, None]
        normal = np.column_stack((-unit[:, 1], unit[:, 0])) * (widths / 2.0)[:, None]
        near_start = starts + normal
        near_end = ends + normal
        far_end = ends - normal
        far_start = starts - normal
        triangles = np.empty((starts.shape[0] * 6, 2), dtype=np.float64)
        triangles[0::6] = near_start
        triangles[1::6] = near_end
        triangles[2::6] = far_end
        triangles[3::6] = near_start
        triangles[4::6] = far_end
        triangles[5::6] = far_start
        if clip_geometry is not None:
            triangles = _clip_stroke_triangles(
                triangles, clip_geometry, starts.shape[0], prepared_region
            )
            if triangles.shape[0] == 0:
                continue
        chunks.append(triangles)
    if not chunks:
        return np.empty((0, 2), dtype=np.float64)
    return np.concatenate(chunks)
