"""Measure offscreen base-chart composition, without a Qt event loop."""

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time

import cairo
import numpy as np
from osgeo import ogr

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sailworks.core.mapviewport import MercatorProjection
from sailworks.core.storage import DATA_DIR
from sailworks.qt.basecharts import BaseChartRenderer, BaseSource
from sailworks.rendering.overlayrenderer import RenderJob
from sailworks.rendering.style import Style


class Viewport(MercatorProjection):
    _center_lat = 47.6
    _center_lon = -122.4
    _zoom = 8
    width = 912
    height = 812

    def __init__(self, center_lat=47.6, center_lon=-122.4, zoom=8):
        self._center_lat = center_lat
        self._center_lon = center_lon
        self._zoom = zoom

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height


class TileViewport(Viewport):
    """A square world-pixel rectangle; its centre reproduces its exact bounds."""

    def __init__(self, zoom, size, left, top):
        super().__init__(zoom=zoom)
        self.width = self.height = size
        self._center_lon = self._x_to_lon(left + size / 2)
        self._center_lat = self._y_to_lat(top + size / 2)


def selected_tile(viewport, size, dx=0):
    x = math.floor(viewport._lon_to_x(viewport._center_lon) / size) + dx
    y = math.floor(viewport._lat_to_y(viewport._center_lat) / size)
    return TileViewport(viewport._zoom, size, x * size, y * size)


def grid_layout(width, height, size, pan=0):
    """Viewport-aligned tiles and integer paint offsets after an eastward pan."""
    origin = width - math.ceil(width / size) * size
    columns = math.ceil((width - origin + pan) / size)
    rows = math.ceil(height / size)
    return [
        (x, y, origin + x * size - pan, y * size)
        for y in range(rows)
        for x in range(columns)
        if origin + (x + 1) * size > pan
    ]


def report(mode, times, details=""):
    print(
        f"mode={mode}{details} n={len(times)} min={min(times):.3f} ms "
        f"median={statistics.median(times):.3f} ms mean={statistics.mean(times):.3f} ms"
    )


def render(renderer, source, viewport, factor):
    return renderer._render(RenderJob((), viewport, ((source,),), factor, "dark"))


def composite(tiles, layout, factor):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 912 * factor, 812 * factor)
    surface.set_device_scale(factor, factor)
    cr = cairo.Context(surface)
    for x, y, dx, dy in layout:
        cr.set_source_surface(tiles[x, y], dx, dy)
        cr.paint()
    surface.flush()


def measure_grid(renderer, source, viewport, factor, size, iterations):
    cold_layout = grid_layout(viewport.width, viewport.height, size)
    warm_layout = grid_layout(viewport.width, viewport.height, size, 50)
    left = (
        viewport._lon_to_x(viewport._center_lon)
        - viewport.width / 2
        + viewport.width
        - math.ceil(viewport.width / size) * size
    )
    top = viewport._lat_to_y(viewport._center_lat) - viewport.height / 2
    cold, warm, full = [], [], []
    for _ in range(iterations):
        tiles = {}
        start = time.perf_counter()
        for x, y, _, _ in cold_layout:
            tile = TileViewport(viewport._zoom, size, left + x * size, top + y * size)
            tiles[x, y] = render(renderer, source, tile, factor)
        composite(tiles, cold_layout, factor)
        cold.append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        for x, y, _, _ in warm_layout:
            if (x, y) not in tiles:
                tile = TileViewport(
                    viewport._zoom, size, left + x * size, top + y * size
                )
                tiles[x, y] = render(renderer, source, tile, factor)
        composite(tiles, warm_layout, factor)
        warm.append((time.perf_counter() - start) * 1000)
        panned = Viewport(
            viewport._center_lat,
            viewport._x_to_lon(viewport._lon_to_x(viewport._center_lon) + 50),
            viewport._zoom,
        )
        start = time.perf_counter()
        render(renderer, source, panned, factor)
        full.append((time.perf_counter() - start) * 1000)
    new_count = len(
        {(x, y) for x, y, _, _ in warm_layout} - {(x, y) for x, y, _, _ in cold_layout}
    )
    details = f" tile_size={size} zoom={viewport._zoom:g} visible={len(cold_layout)} newly_exposed={new_count}"
    report("grid-cold", cold, details)
    report("grid-warm-pan", warm, details + f" pan_visible={len(warm_layout)}")
    report("grid-fullframe", full, details)


def synthetic(path):
    features = []
    for row in range(8):
        for col in range(12):
            lon, lat = -125 + col * 0.45, 46 + row * 0.4
            points = [
                [
                    lon + 0.16 * math.cos(i * math.tau / 4),
                    lat + 0.12 * math.sin(i * math.tau / 4),
                ]
                for i in range(4)
            ]
            points.append(points[0])
            features.append(
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Polygon", "coordinates": [points]},
                }
            )
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def extract(source, viewport):
    """Load one production-selected quality, preserving feature/polygon order."""
    scale = viewport.get_scale()
    quality = (
        "c"
        if scale > 5000
        else "l" if scale > 500 else "i" if scale > 200 else "h" if scale >= 15 else "f"
    )
    path = (
        source.path
        if source.kind == "simple"
        else str(Path(source.path) / "GSHHS_shp" / quality / f"GSHHS_{quality}_L1.shp")
    )
    dataset = ogr.Open(path)
    if dataset is None:
        raise ValueError(f"Cannot open {path}")
    features, bounds = [], []

    def polygons(geom):
        name = geom.GetGeometryName()
        if name == "POLYGON":
            rings = []
            for i in range(geom.GetGeometryCount()):
                points = np.asarray(
                    geom.GetGeometryRef(i).GetPoints(), dtype=np.float64
                )
                if len(points):
                    rings.append((points[:, 0].copy(), points[:, 1].copy()))
            return [rings] if rings else []
        if name in ("MULTIPOLYGON", "GEOMETRYCOLLECTION"):
            return [
                p
                for i in range(geom.GetGeometryCount())
                for p in polygons(geom.GetGeometryRef(i))
            ]
        return []

    for layer in dataset:
        for feature in layer:
            geom = feature.GetGeometryRef()
            if geom is not None and not geom.IsEmpty():
                parts = polygons(geom)
                if parts:
                    features.append(parts)
                    bounds.append(geom.GetEnvelope())
    return features, np.asarray(bounds, dtype=np.float64).reshape(-1, 4), quality


def clip_ring(points, width, height):
    """Sutherland-Hodgman, vectorizing each half-plane pass."""
    for axis, limit, lower in (
        (0, 0, True),
        (0, width, False),
        (1, 0, True),
        (1, height, False),
    ):
        if not len(points):
            break
        previous = np.roll(points, 1, axis=0)
        inside = points[:, axis] >= limit if lower else points[:, axis] <= limit
        prior_inside = np.roll(inside, 1)
        crossing = inside != prior_inside
        indices = np.flatnonzero(crossing)
        intersection = previous[indices] + (
            (limit - previous[indices, axis])
            / (points[indices, axis] - previous[indices, axis])
        )[:, None] * (points[indices] - previous[indices])
        counts = crossing.astype(int) + inside.astype(int)
        out = np.empty((counts.sum(), 2), dtype=np.float64)
        offsets = np.cumsum(counts) - counts
        out[offsets[indices]] = intersection
        indices = np.flatnonzero(inside)
        out[offsets[indices] + crossing[indices].astype(int)] = points[indices]
        points = out
    return points


def numpy_render(data, viewport, factor):
    features, bounds, _ = data
    timing = dict(cull=0.0, project=0.0, clip=0.0, draw=0.0)
    start = time.perf_counter()
    top_left, bottom_right = viewport.get_bbox()
    north, west = top_left.get_degrees()
    south, east = bottom_right.get_degrees()
    selected = np.flatnonzero(
        (bounds[:, 0] <= east)
        & (bounds[:, 1] >= west)
        & (bounds[:, 2] <= north)
        & (bounds[:, 3] >= south)
    )
    timing["cull"] += time.perf_counter() - start
    start = time.perf_counter()
    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32, viewport.width * factor, viewport.height * factor
    )
    surface.set_device_scale(factor, factor)
    cr = cairo.Context(surface)
    palette = Style.chart_palettes["dark"]
    palette.sea.apply(cr)
    cr.rectangle(0, 0, viewport.width, viewport.height)
    cr.stroke_preserve()
    cr.fill()
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    timing["draw"] += time.perf_counter() - start
    clipped = ring_count = 0
    for index in selected:
        for polygon in features[index]:
            paths = []
            for lons, lats in polygon:
                start = time.perf_counter()
                xs, ys = viewport.convert_geographic_to_screen_batch(lats, lons)
                points = np.column_stack((xs, ys))
                timing["project"] += time.perf_counter() - start
                start = time.perf_counter()
                ring_count += 1
                needs_clip = np.any(
                    (xs < 0) | (xs > viewport.width) | (ys < 0) | (ys > viewport.height)
                )
                if needs_clip:
                    clipped += 1
                    points = clip_ring(points, viewport.width, viewport.height)
                timing["clip"] += time.perf_counter() - start
                if len(points):
                    paths.append(points)
            start = time.perf_counter()
            cr.new_path()
            for points in paths:
                cr.move_to(*points[0])
                for x, y in points[1:]:
                    cr.line_to(x, y)
                cr.close_path()
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            palette.land_fill.apply(cr)
            cr.fill_preserve()
            palette.land_stroke.apply(cr)
            cr.stroke()
            timing["draw"] += time.perf_counter() - start
    start = time.perf_counter()
    surface.flush()
    timing["draw"] += time.perf_counter() - start
    return surface, timing, clipped, ring_count


def verify(renderer, source, data, viewport, factor):
    reference = render(renderer, source, viewport, factor)
    candidate, _, _, _ = numpy_render(data, viewport, factor)
    a = np.frombuffer(reference.get_data(), dtype=np.uint8).reshape(
        viewport.height * factor, viewport.width * factor, 4
    )
    b = np.frombuffer(candidate.get_data(), dtype=np.uint8).reshape(a.shape)
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return np.any(delta != 0, axis=2).mean(), np.any(delta > 8, axis=2).mean()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--numpy", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--gshhs", type=Path, default=Path(DATA_DIR) / "gshhs")
    parser.add_argument("-n", "--iterations", type=int, default=10)
    parser.add_argument("--factor", type=int, choices=(1, 2), default=2)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pan", dest="mode", action="store_const", const="pan")
    mode.add_argument("--static", dest="mode", action="store_const", const="static")
    parser.set_defaults(mode="pan")
    parser.add_argument("--zoom", type=float, default=8.0)
    parser.add_argument("--center-lat", type=float, default=47.6)
    parser.add_argument("--center-lon", type=float, default=-122.4)
    measurement = parser.add_mutually_exclusive_group()
    measurement.add_argument("--tile", action="store_true")
    measurement.add_argument("--grid", action="store_true")
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument("--tile-dx", type=int, default=0)
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("iterations must be positive")
    if args.tile_size < 1:
        parser.error("tile size must be positive")
    if (args.tile or args.grid) and not args.zoom.is_integer():
        parser.error("tile and grid modes require integer zoom")
    with tempfile.TemporaryDirectory() as temporary:
        if args.synthetic:
            path = Path(temporary) / "synthetic.geojson"
            synthetic(path)
            source = BaseSource("simple", str(path))
        else:
            missing = [
                q
                for q in "fhilc"
                if not (args.gshhs / "GSHHS_shp" / q / f"GSHHS_{q}_L1.shp").is_file()
            ]
            if missing:
                print(
                    f"GSHHS data not installed at {args.gshhs} (missing: {', '.join(missing)}); use --synthetic",
                    file=sys.stderr,
                )
                return 1
            source = BaseSource(
                "gshhs", str(args.gshhs), initial_times=tuple((q, 0.0) for q in "fhilc")
            )
        renderer = BaseChartRenderer(lambda *args: None)
        viewport = Viewport(args.center_lat, args.center_lon, args.zoom)
        if args.verify and not args.numpy:
            parser.error("--verify requires --numpy")
        if args.numpy:
            if args.grid or args.tile:
                parser.error("--numpy does not support --grid or --tile")
            start = time.perf_counter()
            data = extract(source, viewport)
            extraction = (time.perf_counter() - start) * 1000
            print(
                f"mode=numpy-extract quality={data[2]} features={len(data[0])} n=1 min={extraction:.3f} ms median={extraction:.3f} ms mean={extraction:.3f} ms"
            )
            if args.verify:
                different, over8 = verify(renderer, source, data, viewport, args.factor)
                print(
                    f"mode=numpy-verify differing_fraction={different:.9f} over8_fraction={over8:.9f}"
                )
                return 0
            stages = {key: [] for key in ("cull", "project", "clip", "draw")}
            totals = []
            clipped = rings = 0
            pan_degrees = 50 * 360 / (Viewport.TILE_SIZE * 2**args.zoom)
            for iteration in range(args.iterations):
                if args.mode == "pan":
                    lon = (args.center_lon + iteration * pan_degrees + 180) % 360 - 180
                    viewport = Viewport(args.center_lat, lon, args.zoom)
                start = time.perf_counter()
                _, timing, nclip, nring = numpy_render(data, viewport, args.factor)
                totals.append((time.perf_counter() - start) * 1000)
                clipped += nclip
                rings += nring
                for key in stages:
                    stages[key].append(timing[key] * 1000)
            for key, times in stages.items():
                report(f"numpy-{key}", times)
            report(
                "numpy-total",
                totals,
                f" pan_mode={args.mode} zoom={args.zoom:g} clipped_fraction={clipped / rings if rings else 0:.6f} rings={rings}",
            )
            return 0
        if args.grid:
            measure_grid(
                renderer, source, viewport, args.factor, args.tile_size, args.iterations
            )
            return 0
        if args.tile:
            times = []
            for iteration in range(args.iterations):
                dx = args.tile_dx + (iteration if args.mode == "pan" else 0)
                tile = selected_tile(viewport, args.tile_size, dx)
                start = time.perf_counter()
                render(renderer, source, tile, args.factor)
                times.append((time.perf_counter() - start) * 1000)
            report("tile", times, f" tile_size={args.tile_size} zoom={args.zoom:g}")
            return 0
        pan_degrees = 50 * 360 / (Viewport.TILE_SIZE * 2**args.zoom)
        times = []
        for iteration in range(args.iterations):
            if args.mode == "pan":
                lon = (args.center_lon + iteration * pan_degrees + 180) % 360 - 180
                viewport = Viewport(args.center_lat, lon, args.zoom)
            job = RenderJob((), viewport, ((source,),), args.factor, "dark")
            start = time.perf_counter()
            renderer._render(job)
            times.append((time.perf_counter() - start) * 1000)
        print(
            f"mode={args.mode} n={len(times)} min={min(times):.3f} ms median={statistics.median(times):.3f} ms mean={statistics.mean(times):.3f} ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
