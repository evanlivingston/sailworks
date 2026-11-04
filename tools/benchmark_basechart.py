"""Measure offscreen base-chart composition, without a Qt event loop."""

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time

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
    parser.add_argument("--zoom", type=float, default=8)
    parser.add_argument("--center-lat", type=float, default=47.6)
    parser.add_argument("--center-lon", type=float, default=-122.4)
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("iterations must be positive")
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
