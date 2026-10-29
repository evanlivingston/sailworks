"""Stable geographic seed paths, separate from the legacy flat renderer API."""

from dataclasses import dataclass
import math

import numpy as np

from .streamlines import (
    MAX_STEPS,
    STEP,
    WindGrid,
    density_parameters,
    _columns_to_arrays,
    spatially_thin_seeds,
)
from .windpainter import barb_bucket_colors


@dataclass(frozen=True)
class PathSegment:
    speed: float
    land: bool
    arrow: bool


@dataclass(frozen=True)
class StreamlinePath:
    seed_id: tuple[int, int, int]
    vertices: tuple[tuple[float, float], ...]
    segments: tuple[PathSegment, ...]


def _world_geo(view, x, y):
    size = view.TILE_SIZE * 2.0**view._zoom
    lon = (x / size * 360.0) - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / size))))
    return lat, lon


def _screen_geo(view, x, y):
    wx = view._lon_to_x(view._center_lon) + x - view.width / 2
    wy = view._lat_to_y(view._center_lat) + y - view.height / 2
    return _world_geo(view, wx, wy)


def _seeds(view, density):
    spacing, seed_limit, _ = density_parameters(density)
    cell = spacing
    cx, cy = view._lon_to_x(view._center_lon), view._lat_to_y(view._center_lat)
    left, top = cx - view.width / 2, cy - view.height / 2
    right, bottom = cx + view.width / 2, cy + view.height / 2
    ix0, ix1 = math.floor(left / cell), math.floor(right / cell)
    iy0, iy1 = math.floor(top / cell), math.floor(bottom / cell)
    candidates = []
    for iy in range(iy0, iy1 + 1):
        for ix in range(ix0, ix1 + 1):
            wx, wy = (ix + 0.5) * cell, (iy + 0.5) * cell
            sx, sy = wx - left, wy - top
            if 0 <= sx < view.width and 0 <= sy < view.height:
                candidates.append(((round(view._zoom, 6), iy, ix), sx, sy))
    capped = len(candidates) > seed_limit
    if capped:
        keep = np.linspace(0, len(candidates) - 1, seed_limit, dtype=np.int64)
        candidates = [candidates[index] for index in keep]
    candidates = spatially_thin_seeds(
        candidates, lambda item: (item[0][2], item[0][1]), capped=capped
    )
    candidates.sort(
        key=lambda item: (item[1] - view.width / 2) ** 2
        + (item[2] - view.height / 2) ** 2
    )
    return candidates


def trace_streamline_paths(
    view, columns, on_ground, cancelled=lambda: False, density="standard"
):
    """Build deterministic geographic paths with bounded shared budgets.

    ``on_ground`` remains in the signature for compatibility, but is not called.
    Streamlines are drawn over land.
    """
    if cancelled():
        return None
    columns = _columns_to_arrays(columns)
    if (
        len(columns) < 2
        or len(columns[0][1]) < 2
        or any(not np.array_equal(c[1], columns[0][1]) for c in columns)
    ):
        return None
    grid = WindGrid(view, columns)
    _spacing, seed_limit, segment_limit = density_parameters(density)
    paths, used = [], 0
    for seed_id, sx, sy in _seeds(view, density)[:seed_limit]:
        if cancelled():
            return None
        start = _screen_geo(view, sx, sy)
        halves = []
        for sign in (1, -1):
            x, y = sx, sy
            part = []
            visited = {(round(x / STEP), round(y / STEP))}
            for step in range(MAX_STEPS):
                if cancelled() or used >= segment_limit:
                    break
                vel = grid.velocity(x, y)
                if vel is None:
                    break
                dx, dy, _ = vel
                mid = grid.velocity(x + sign * dx * STEP / 2, y + sign * dy * STEP / 2)
                if mid is None:
                    break
                dx, dy, speed = mid
                nx, ny = x + sign * dx * STEP, y + sign * dy * STEP
                if (
                    not (0 <= nx < view.width and 0 <= ny < view.height)
                    or grid.velocity(nx, ny) is None
                ):
                    break
                key = (round(nx / STEP), round(ny / STEP))
                if key in visited:
                    break
                visited.add(key)
                a, b = _screen_geo(view, x, y), _screen_geo(view, nx, ny)
                part.append((a, b, speed, step % 20 == 10))
                used += 1
                x, y = nx, ny
            halves.append(part)
        ordered = [(b, a, speed, arrow) for a, b, speed, arrow in reversed(halves[1])]
        ordered.extend(halves[0])
        if not ordered:
            continue
        vertices = [ordered[0][0]] + [segment[1] for segment in ordered]
        if vertices[len(halves[1])] != start:
            raise AssertionError("backward and forward paths must join at their seed")
        if cancelled():
            return None
        metadata = tuple(
            PathSegment(
                float(speed),
                False,
                bool(arrow),
            )
            for a, b, speed, arrow in ordered
        )
        paths.append(StreamlinePath(seed_id, tuple(vertices), metadata))
        if used >= segment_limit:
            break
    return tuple(paths)


def paths_to_flat(paths):
    """Convert ordered paths to the legacy seven-array geographic format."""
    lat1, lon1, lat2, lon2, speeds, land, arrows = [], [], [], [], [], [], []
    for path in paths or ():
        for index, meta in enumerate(path.segments):
            a, b = path.vertices[index : index + 2]
            lat1.append(a[0])
            lon1.append(a[1])
            lat2.append(b[0])
            lon2.append(b[1])
            speeds.append(meta.speed)
            land.append(meta.land)
            arrows.append(meta.arrow)
    arrays = (
        np.asarray(lat1),
        np.asarray(lon1),
        np.asarray(lat2),
        np.asarray(lon2),
        np.asarray(speeds),
        np.asarray(land, dtype=bool),
        np.asarray(arrows, dtype=bool),
    )
    for array in arrays:
        array.setflags(write=False)
    return arrays


def paths_to_render_buffers(paths, over_land):
    """Return immutable world-space line and arrow-source buffers by speed.

    Each bucket maps to ``(segments, arrow_segments)`` arrays of shape Nx4,
    holding normalized Mercator endpoints. Projection and viewport clipping
    are deliberately left to the renderer.
    """
    grouped = {}
    bucket_count = len(barb_bucket_colors())
    for path in paths or ():
        for index, meta in enumerate(path.segments):
            if not over_land and meta.land:
                continue
            a, b = path.vertices[index : index + 2]
            x1, y1 = _normalized_mercator(a)
            x2, y2 = _normalized_mercator(b)
            bucket = max(0, min(int(meta.speed // 2), bucket_count - 1))
            main, arrows = grouped.setdefault(bucket, ([], []))
            segment = (x1, y1, x2, y2)
            main.append(segment)
            if meta.arrow:
                arrows.append(segment)

    buffers = {}
    for bucket, (main, arrows) in grouped.items():
        line_array = np.asarray(main, dtype=np.float64).reshape((-1, 4))
        arrow_array = np.asarray(arrows, dtype=np.float64).reshape((-1, 4))
        line_array.setflags(write=False)
        arrow_array.setflags(write=False)
        buffers[bucket] = (line_array, arrow_array)
    return buffers


def _normalized_mercator(point):
    lat, lon = point
    lat = max(-85.0511287798066, min(85.0511287798066, lat))
    x = ((lon + 180.0) / 360.0) % 1.0
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0
    return x, y
