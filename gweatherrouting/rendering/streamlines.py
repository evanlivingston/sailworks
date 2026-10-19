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


def _columns_to_arrays(columns):
    """Convert tuple-format columns to array-columns if needed.

    Tuple columns: list of columns, each column is list of
    (dir, speed, (lat, lon)) or (lat, lon, value) tuples.
    Array columns: list of (lon, lats_array, dirs_array, speeds_array) or
    (lon, lats_array, values_array).
    """
    if not columns:
        return columns
    first = columns[0]
    if isinstance(first, tuple) and len(first) == 4 and hasattr(first[1], "shape"):
        # Already array format
        return columns
    # Detect wind vs pressure by checking first point structure
    first_col = columns[0]
    if isinstance(first_col[0], tuple) and len(first_col[0]) == 3:
        # Could be (dir, speed, (lat, lon)) wind or (lat, lon, value) pressure
        first_point = first_col[0]
        if isinstance(first_point[2], tuple):
            # Wind: (dir, speed, (lat, lon))
            columns_out = []
            for col in columns:
                lons_col = [p[2][1] for p in col]
                lats_col = [p[2][0] for p in col]
                dirs_col = [p[0] for p in col]
                speeds_col = [p[1] for p in col]
                lon = lons_col[0]
                columns_out.append(
                    (
                        float(lon),
                        np.array(lats_col, dtype=np.float64),
                        np.array(dirs_col, dtype=np.float64),
                        np.array(speeds_col, dtype=np.float64),
                    )
                )
            return columns_out
        else:
            # Pressure: (lat, lon, value)
            pressure_columns = []
            for col in columns:
                lons_col = [p[1] for p in col]
                lats_col = [p[0] for p in col]
                vals_col = [p[2] for p in col]
                lon = lons_col[0]
                pressure_columns.append(
                    (
                        float(lon),
                        np.array(lats_col, dtype=np.float64),
                        np.array(vals_col, dtype=np.float64),
                    )
                )
            return pressure_columns
    return columns


class WindGrid:
    """Bilinear earth-relative velocity on projected forecast-node positions."""

    def __init__(self, view, columns):
        columns = _columns_to_arrays(columns)
        if not columns:
            self.xs = []
            self.ys = []
            self.u = np.array([], dtype=np.float64).reshape(0, 0)
            self.v = np.array([], dtype=np.float64).reshape(0, 0)
            self.valid_x = np.array([], dtype=bool)
            self.valid_y = np.array([], dtype=bool)
            return
        # Columns: (lon, lats_array, dirs_array, speeds_array). All columns
        # share the same lat vector (enforced by trace_streamlines).
        lats = columns[0][1]
        lons = np.array([col[0] for col in columns], dtype=np.float64)
        xs, _ = view.convert_geographic_to_screen_batch(np.zeros(len(lons)), lons)
        _, ys = view.convert_geographic_to_screen_batch(lats, np.zeros(len(lats)))
        self.xs = xs.tolist()
        self.ys = ys[::-1].tolist()
        dirs_matrix = np.stack([col[2] for col in columns])
        speeds_matrix = np.stack([col[3] for col in columns])
        directions = np.radians(dirs_matrix.T[::-1])
        speeds = speeds_matrix.T[::-1]
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


def _cell(x, y, cell_size):
    """Grid cell holding a screen point."""
    return int(x // cell_size), int(y // cell_size)


def _seed_positions(view, spacing, max_seeds):
    """Seed points covering the view, centred first when the budget is capped."""
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
    return seeds


def _integrate_seed(
    grid,
    view,
    seed,
    cell_size,
    cancelled,
    max_segments,
    occupied,
    segments,
    speeds,
    arrows,
):
    """Integrate one seed in both directions; returns the cells it visited."""
    sx, sy = seed
    local = {_cell(sx, sy, cell_size)}
    for sign in (1, -1):
        x, y, previous = sx, sy, _cell(sx, sy, cell_size)
        for step in range(MAX_STEPS):
            if cancelled() or len(segments) >= max_segments:
                break
            velocity = grid.velocity(x, y)
            if velocity is None:
                break
            dx, dy, _speed = velocity
            midpoint = grid.velocity(x + sign * dx * STEP / 2, y + sign * dy * STEP / 2)
            if midpoint is None:
                break
            dx, dy, speed = midpoint
            nx, ny = x + sign * dx * STEP, y + sign * dy * STEP
            if not (0 <= nx < view.width and 0 <= ny < view.height):
                break
            key = _cell(nx, ny, cell_size)
            if key in occupied or (key in local and key != previous):
                break
            if grid.velocity(nx, ny) is None:
                break
            segments.append((x, y, nx, ny) if sign > 0 else (nx, ny, x, y))
            speeds.append(speed)
            arrows.append(step % 20 == 10)
            local.add(key)
            x, y, previous = nx, ny, key
    return local


def _project_segments(view, screen, speeds, arrows, on_ground, cancelled):
    """Project traced segments to geographic coordinates and collect land flags."""
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
    land: list[bool] = []
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


def trace_streamlines(
    view, columns, on_ground, cancelled=lambda: False, density="standard"
):
    """Trace both directions with midpoint integration; never access live GUI state.

    The input must share a latitude vector. Missing cells and coordinate gaps
    stop integration. Four-pixel segments carry geographic coordinates, speed,
    a land flag and occasional direction chevrons. All budgets are hard bounds.
    """
    columns = _columns_to_arrays(columns)
    if len(columns) < 2 or len(columns[0][1]) < 2:
        return None
    # Columns: (lon, lats_array, dirs_array, speeds_array). All must share
    # the same lat vector for streamline integration.
    lats = columns[0][1]
    if any(not np.array_equal(col[1], lats) for col in columns):
        return None
    grid = WindGrid(view, columns)
    spacing, max_seeds, max_segments = density_parameters(density)
    seeds = _seed_positions(view, spacing, max_seeds)
    occupied: set[tuple[int, int]] = set()
    segments: list[tuple[float, float, float, float]] = []
    speeds: list[float] = []
    arrows: list[bool] = []
    cell_size = spacing / 2

    for sx, sy in seeds:
        if cancelled():
            return None
        if _cell(sx, sy, cell_size) in occupied:
            continue
        local = _integrate_seed(
            grid,
            view,
            (sx, sy),
            cell_size,
            cancelled,
            max_segments,
            occupied,
            segments,
            speeds,
            arrows,
        )
        occupied.update(local)
        if len(segments) >= max_segments:
            break
    if not segments or cancelled():
        return None
    screen = np.asarray(segments)
    return _project_segments(view, screen, speeds, arrows, on_ground, cancelled)


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
    geometry: dict[int, tuple[list[list[float]], list, list]] = {}
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
