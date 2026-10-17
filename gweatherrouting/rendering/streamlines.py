"""Bounded, worker-built static streamlines; foreground projection only."""

from bisect import bisect_right
import math

import numpy as np

from .windpainter import mercator_project_flat, barb_bucket_colors, BARB_DENSITIES

SPACING = 44.0
STEP = 4.0
MAX_SEEDS = 600
MAX_STEPS = 160
MAX_SEGMENTS = 30000


def density_parameters(density):
    """Shared presets, with bounded rather than unlimited 'all' streamlines."""
    spacing = next(
        (value for key, _label, value in BARB_DENSITIES if key == density), SPACING
    )
    spacing = spacing or 12.0
    ratio = SPACING / spacing
    return (
        spacing,
        min(2000, round(MAX_SEEDS * ratio**2)),
        min(60000, round(MAX_SEGMENTS * ratio)),
    )


class WindGrid:
    """Bilinear earth-relative velocity on projected forecast-node positions."""

    def __init__(self, view, columns):
        lats = np.array([point[2][0] for point in columns[0]])
        lons = np.array([column[0][2][1] for column in columns])
        xs, _ = view.convert_geographic_to_screen_batch(np.zeros(len(lons)), lons)
        _, ys = view.convert_geographic_to_screen_batch(lats, np.zeros(len(lats)))
        self.xs = xs.tolist()
        self.ys = ys[::-1].tolist()
        directions = np.radians(
            np.array([[p[0] for p in col] for col in columns]).T[::-1]
        )
        speeds = np.array([[p[1] for p in col] for col in columns]).T[::-1]
        self.u = -speeds * np.sin(directions)
        self.v = speeds * np.cos(directions)  # Screen Y runs southward.
        dx, dy = np.diff(xs), np.diff(ys[::-1])
        self.valid_x = (dx > 0) & (dx <= 1.5 * np.median(dx))
        self.valid_y = (dy > 0) & (dy <= 1.5 * np.median(dy))

    def velocity(self, x, y):
        i, j = bisect_right(self.xs, x) - 1, bisect_right(self.ys, y) - 1
        if not (0 <= i < len(self.xs) - 1 and 0 <= j < len(self.ys) - 1):
            return None
        if not self.valid_x[i] or not self.valid_y[j]:
            return None
        tx = (x - self.xs[i]) / (self.xs[i + 1] - self.xs[i])
        ty = (y - self.ys[j]) / (self.ys[j + 1] - self.ys[j])
        weights = ((1 - tx) * (1 - ty), tx * (1 - ty), (1 - tx) * ty, tx * ty)
        values = []
        for field in (self.u, self.v):
            value = sum(
                weight * field[row, col]
                for weight, row, col in zip(
                    weights, (j, j, j + 1, j + 1), (i, i + 1, i, i + 1)
                )
            )
            values.append(float(value))
        speed = math.hypot(*values)
        if not math.isfinite(speed) or speed < 0.05:
            return None
        return values[0] / speed, values[1] / speed, speed


def trace_streamlines(
    view, columns, on_ground, cancelled=lambda: False, density="standard"
):
    """Trace both directions with midpoint integration; never access live GUI state.

    The input must share a latitude vector. Missing cells and coordinate gaps
    stop integration. Four-pixel segments carry geographic coordinates, speed,
    a land flag and occasional direction chevrons. All budgets are hard bounds.
    """
    if len(columns) < 2 or len(columns[0]) < 2:
        return None
    lats = tuple(point[2][0] for point in columns[0])
    if any(tuple(point[2][0] for point in col) != lats for col in columns):
        return None
    grid = WindGrid(view, columns)
    spacing, max_seeds, max_segments = density_parameters(density)
    seeds = [
        (x, y)
        for y in np.arange(spacing / 2, view.height, spacing)
        for x in np.arange(spacing / 2, view.width, spacing)
    ]
    if len(seeds) > max_seeds:
        seeds = [seeds[i] for i in np.linspace(0, len(seeds) - 1, max_seeds, dtype=int)]
    # Prioritize the visible center, not the overscan margins, when capped.
    seeds.sort(
        key=lambda point: (point[0] - view.width / 2) ** 2
        + (point[1] - view.height / 2) ** 2
    )
    occupied = set()
    segments, speeds, arrows = [], [], []
    cell_size = spacing / 2

    def cell(x, y):
        return int(x // cell_size), int(y // cell_size)

    for sx, sy in seeds:
        if cancelled():
            return None
        if cell(sx, sy) in occupied:
            continue
        local = {cell(sx, sy)}
        for sign in (1, -1):
            x, y, previous = sx, sy, cell(sx, sy)
            for step in range(MAX_STEPS):
                if cancelled() or len(segments) >= max_segments:
                    break
                velocity = grid.velocity(x, y)
                if velocity is None:
                    break
                dx, dy, _speed = velocity
                midpoint = grid.velocity(
                    x + sign * dx * STEP / 2, y + sign * dy * STEP / 2
                )
                if midpoint is None:
                    break
                dx, dy, speed = midpoint
                nx, ny = x + sign * dx * STEP, y + sign * dy * STEP
                if not (0 <= nx < view.width and 0 <= ny < view.height):
                    break
                key = cell(nx, ny)
                if key in occupied or (key in local and key != previous):
                    break
                if grid.velocity(nx, ny) is None:
                    break
                segments.append((x, y, nx, ny) if sign > 0 else (nx, ny, x, y))
                speeds.append(speed)
                arrows.append(step % 20 == 10)
                local.add(key)
                x, y, previous = nx, ny, key
        occupied.update(local)
        if len(segments) >= max_segments:
            break
    if not segments or cancelled():
        return None
    screen = np.asarray(segments)
    tile = 256 * 2.0**view._zoom
    cx, cy = view._lon_to_x(view._center_lon), view._lat_to_y(view._center_lat)

    def geographic(x, y):
        lon = (cx + x - view.width / 2) / tile * 360 - 180
        lat = np.degrees(
            np.arctan(np.sinh(math.pi * (1 - 2 * (cy + y - view.height / 2) / tile)))
        )
        return lat, lon

    lat1, lon1 = geographic(screen[:, 0], screen[:, 1])
    lat2, lon2 = geographic(screen[:, 2], screen[:, 3])
    mid_lat, mid_lon = geographic(
        (screen[:, 0] + screen[:, 2]) / 2, (screen[:, 1] + screen[:, 3]) / 2
    )
    land = []
    for lat, lon in zip(mid_lat, mid_lon):
        if cancelled():
            return None
        land.append(on_ground(float(lat), float(lon)))
    result = (
        lat1,
        lon1,
        lat2,
        lon2,
        np.asarray(speeds),
        np.asarray(land),
        np.asarray(arrows),
    )
    for array in result:
        array.setflags(write=False)
    return result


def geometry_for_view(flat, center_lat, center_lon, zoom, width, height, over_land):
    """Cheap foreground reprojection using the existing CPU/GPU line format."""
    if flat is None:
        return {}
    lat1, lon1, lat2, lon2, speeds, land, arrows = flat
    x1, y1 = mercator_project_flat(
        lat1, lon1, center_lat, center_lon, zoom, width, height
    )
    x2, y2 = mercator_project_flat(
        lat2, lon2, center_lat, center_lon, zoom, width, height
    )
    visible = (
        (np.maximum(x1, x2) >= -8)
        & (np.minimum(x1, x2) <= width + 8)
        & (np.maximum(y1, y2) >= -8)
        & (np.minimum(y1, y2) <= height + 8)
    )
    if not over_land:
        visible &= ~land
    geometry = {}
    buckets = np.clip((speeds // 2).astype(int), 0, len(barb_bucket_colors()) - 1)
    segments = np.column_stack((x1, y1, x2, y2))
    for bucket in np.unique(buckets[visible]):
        selected = visible & (buckets == bucket)
        lines = segments[selected].tolist()
        heads = selected & arrows
        if heads.any():
            ax, ay, bx, by = x1[heads], y1[heads], x2[heads], y2[heads]
            length = np.maximum(np.hypot(bx - ax, by - ay), 1e-12)
            dx, dy = (bx - ax) / length, (by - ay) / length
            lines.extend(
                np.column_stack(
                    (bx, by, bx - 5 * dx + 2.5 * dy, by - 5 * dy - 2.5 * dx)
                ).tolist()
            )
            lines.extend(
                np.column_stack(
                    (bx, by, bx - 5 * dx - 2.5 * dy, by - 5 * dy + 2.5 * dx)
                ).tolist()
            )
        geometry[int(bucket)] = (lines, [], [])
    return geometry
