"""Compare marine field sampling with/without the retired shoreline predicate.

The baseline mask is retained here only as an experiment oracle. It is not
imported by or used in the application rendering path.
"""

import argparse
import hashlib
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from gweatherrouting.core.mapviewport import MapPoint, MapViewport
from gweatherrouting.core.marine.fields import FieldSelection, FieldStore
from gweatherrouting.qt.gribviewport import ViewportCamera
from gweatherrouting.rendering.marinepresentation import (
    MarineSources,
    current_grid_spacing,
)
from gweatherrouting.rendering.marinesnapshot import (
    CurrentArrow,
    MarineSnapshot,
    current_triangles,
    sample_currents,
)


def _legacy_land_mask(camera, data_dir):
    from osgeo import ogr
    from shapely import STRtree, from_wkb, make_valid
    from shapely.geometry import shape

    from gweatherrouting.core.utils import COUNTRY_SHAPES

    polygons = []
    detailed = False
    view = camera.view()
    north, west = view.convert_screen_to_geographic(0, 0).get_degrees()
    south, east = view.convert_screen_to_geographic(
        camera.width, camera.height
    ).get_degrees()
    for quality in ("f", "h", "i", "l", "c"):
        path = (
            data_dir
            / "gshhs"
            / "GSHHS_shp"
            / quality
            / ("GSHHS_" + quality + "_L1.shp")
        )
        if not path.exists():
            continue
        source = ogr.Open(str(path))
        if source is None:
            continue
        detailed = True
        layer = source.GetLayer(0)
        for shift in (-360, 0, 360):
            left, right = max(-180, west - shift), min(180, east - shift)
            if left >= right:
                continue
            layer.SetSpatialFilterRect(left, south, right, north)
            layer.ResetReading()
            polygons.extend(
                from_wkb(bytes(feature.GetGeometryRef().ExportToWkb()))
                for feature in layer
            )
        break
    if not detailed:
        polygons = [shape(feature["geometry"]) for feature in COUNTRY_SHAPES]
    tree = STRtree(
        [polygon if polygon.is_valid else make_valid(polygon) for polygon in polygons]
    )

    def on_land(latitude, longitude):
        from shapely.geometry import Point

        point = Point((longitude + 180) % 360 - 180, latitude)
        return bool(len(tree.query(point, predicate="intersects")))

    return on_land


def _anchors(camera, density):
    level = max(0, math.floor(camera.zoom + 0.5))
    cells = max(
        1,
        round(MapViewport.TILE_SIZE * 2**level / current_grid_spacing(density)),
    )
    world_pixels = MapViewport.TILE_SIZE * 2**camera.zoom
    center_x = (camera.center_lon + 180) / 360
    latitude = max(-MapViewport.MAX_LAT, min(MapViewport.MAX_LAT, camera.center_lat))
    center_y = (1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2
    left = center_x - camera.width / (2 * world_pixels)
    right = center_x + camera.width / (2 * world_pixels)
    top = max(0, center_y - camera.height / (2 * world_pixels))
    bottom = min(1, center_y + camera.height / (2 * world_pixels))
    columns = tuple(
        dict.fromkeys(
            column % cells
            for column in range(math.ceil(left * cells), math.floor(right * cells) + 1)
        )
    )
    return tuple(
        (
            math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / cells)))),
            column / cells * 360 - 180,
        )
        for row in range(math.ceil(top * cells), math.floor(bottom * cells) + 1)
        for column in columns
    )


def _masked_baseline(sources, instant, camera, anchors, on_land):
    selection = FieldSelection(sources.fields)
    arrows = []
    for latitude, longitude in anchors:
        if on_land(latitude, longitude):
            continue
        result = selection.sample(latitude, longitude, instant)
        if result.status == "available":
            arrows.append(CurrentArrow(latitude, longitude, *result.values))
    return MarineSnapshot(
        sources, instant, tuple(arrows), "available" if arrows else "no valid data"
    )


def _p95(values):
    return sorted(values)[max(0, math.ceil(0.95 * len(values)) - 1)]


def _field_fingerprint(fields_root, fields):
    digest = hashlib.sha256()
    for field_id in sorted({field.id for field in fields}):
        for name in ("manifest.json", "arrays.npz"):
            path = fields_root / field_id / name
            digest.update(path.relative_to(fields_root).as_posix().encode())
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
    selection = fields_root / "selection.json"
    if selection.exists():
        digest.update(selection.read_bytes())
    return digest.hexdigest()


def run(args):
    data_dir = Path(args.snapshot).expanduser().resolve()
    fields_root = data_dir / "marine" / "fields"
    field_store = FieldStore(fields_root)
    fields = field_store.snapshot().fields
    if not fields:
        raise RuntimeError("snapshot contains no marine current fields")
    instant = datetime.fromtimestamp(
        max(field.times[-1] for field in fields), timezone.utc
    )
    camera = ViewportCamera(
        args.center_lat,
        args.center_lon,
        args.zoom,
        args.width,
        args.height,
        instant,
    )
    empty = type("EmptySelection", (), {"datasets": (), "sample": lambda *_: None})()
    sources = MarineSources(fields, empty, empty, args.density)
    anchors = _anchors(camera, args.density)

    started = time.perf_counter()
    on_land = _legacy_land_mask(camera, data_dir)
    mask_setup_ms = (time.perf_counter() - started) * 1000

    # One warmup per implementation, then matched repeat counts. Each baseline
    # request reuses the same viewport-specific land tree as the old worker did.
    _masked_baseline(sources, instant, camera, anchors, on_land)
    sample_currents(sources, instant, camera, lambda: False)
    baseline_sample_ms, candidate_sample_ms = [], []
    baseline_vertices_ms, candidate_vertices_ms = [], []
    baseline_count = candidate_count = 0
    for _ in range(args.repeats):
        started = time.perf_counter()
        baseline = _masked_baseline(sources, instant, camera, anchors, on_land)
        baseline_sample_ms.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        baseline_vertices = current_triangles(baseline, camera)
        baseline_vertices_ms.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        candidate = sample_currents(sources, instant, camera, lambda: False)
        candidate_sample_ms.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        candidate_vertices = current_triangles(candidate, camera)
        candidate_vertices_ms.append((time.perf_counter() - started) * 1000)
        baseline_count = len(baseline.arrows)
        candidate_count = len(candidate.arrows)

    baseline_p95 = _p95(baseline_sample_ms)
    candidate_p95 = _p95(candidate_sample_ms)
    return {
        "workload": {
            "field_count": len(fields),
            "field_snapshot_sha256": _field_fingerprint(fields_root, fields),
            "source_time_utc": instant.isoformat(),
            "center_lat": args.center_lat,
            "center_lon": args.center_lon,
            "zoom": args.zoom,
            "anchor_count": len(anchors),
            "width": args.width,
            "height": args.height,
            "density": args.density,
            "repeats": args.repeats,
        },
        "legacy_land_predicate": {
            "setup_ms": mask_setup_ms,
            "sample_p95_ms": baseline_p95,
            "sample_ms": baseline_sample_ms,
            "geometry_p95_ms": _p95(baseline_vertices_ms),
            "available_arrows": baseline_count,
            "geometry_vertices": len(baseline_vertices),
        },
        "qt_quick_unmasked": {
            "sample_p95_ms": candidate_p95,
            "sample_ms": candidate_sample_ms,
            "geometry_p95_ms": _p95(candidate_vertices_ms),
            "available_arrows": candidate_count,
            "geometry_vertices": len(candidate_vertices),
        },
        "sample_latency_improvement": (
            1 - candidate_p95 / baseline_p95 if baseline_p95 else 0
        ),
        "candidate_has_at_least_baseline_arrows": candidate_count >= baseline_count,
        "real_fields_present": bool(fields and candidate_count),
        "warning": (
            "Synthetic geometry timing only; does not measure Qt presentation FPS. "
            "Unmasked output may contain coastal points rejected by the legacy shoreline."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, help="Application data root")
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--center-lat", type=float, default=48.4)
    parser.add_argument("--center-lon", type=float, default=-123.45)
    parser.add_argument("--zoom", type=float, default=8)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument(
        "--density", choices=("sparse", "standard", "dense", "maximum"), default="dense"
    )
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("--repeats must be at least 3")
    result = run(args)
    Path(args.output).write_text(json.dumps(result, indent=2))
    return 0 if result["real_fields_present"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
