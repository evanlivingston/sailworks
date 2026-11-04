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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sailworks.core.mapviewport import MercatorProjection
from sailworks.core.storage import DATA_DIR
from sailworks.qt.basecharts import BaseChartRenderer, BaseSource
from sailworks.rendering.overlayrenderer import RenderJob


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
                    lon + 0.16 * math.cos(i * math.tau / 80),
                    lat + 0.12 * math.sin(i * math.tau / 80),
                ]
                for i in range(80)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", action="store_true")
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
