"""Run a saved route headlessly and render isochrones for style review.

Example:
    .venv/bin/python tools/isochrone_preview.py \
        --data-dir "$HOME/Library/Application Support/sailworks" \
        --output /tmp/isochrone-preview.png

The tool uses the route's stored waypoints, departure, WeatherNext artifact,
and polar. It draws the production contour renderer on a plain chart, once
with closed loops and once with the leading-arc trim, and writes a side-by-side
PNG. No Qt window is opened.
"""

import argparse
import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import cairo
import numpy as np
import weatherrouting

from sailworks.core.grib import Grib
from sailworks.core.isochrones import (
    ContourRun,
    IsochroneContours,
    IsochroneSlice,
    collect_contours,
)
from sailworks.core.marine.windcontours import contour_observing_router
from sailworks.core.mapviewport import MapPoint
from sailworks.core.routingjob import isochrone_step_hours
from sailworks.rendering.isochrones import IsochroneFrame, render_isochrones


class PreviewView:
    """A small equirectangular camera with the renderer's expected methods."""

    def __init__(self, bounds, width, height):
        south, west, north, east = bounds
        self.width = width
        self.height = height
        self._south = south
        self._west = west
        self._north = north
        self._east = east
        self._latitude_scale = max(
            abs(math.cos(math.radians((south + north) / 2.0))), 1e-9
        )

    def convert_geographic_to_screen_batch(self, latitudes, longitudes):
        latitudes = np.asarray(latitudes, dtype=float)
        longitudes = np.asarray(longitudes, dtype=float)
        xs = (longitudes - self._west) / (self._east - self._west) * self.width
        ys = (self._north - latitudes) / (self._north - self._south) * self.height
        return xs, ys

    def convert_geographic_to_screen(self, point):
        latitude, longitude = point.get_degrees()
        return (
            (longitude - self._west) / (self._east - self._west) * self.width,
            (self._north - latitude) / (self._north - self._south) * self.height,
        )


def load_route(data_dir, name):
    payload = json.loads((data_dir / "routing.json").read_text())
    routes = payload.get("data", {}).get("elements", [])
    if name:
        routes = [route for route in routes if route.get("name") == name]
    if not routes:
        raise SystemExit("No saved route with routing provenance was found")
    route = routes[0]
    options = route.get("provenance", {}).get("options", {})
    waypoints = [tuple(point) for point in options.get("waypoints", ())]
    if not waypoints:
        raise SystemExit("The saved route has no waypoint list")
    return route, options, waypoints


def artifact_path(data_dir, route, override):
    if override:
        return Path(override)
    binding = route.get("provenance", {}).get("weather_binding", {})
    digest = binding.get("artifact", {}).get("sha256")
    if not digest:
        raise SystemExit("No WeatherNext artifact recorded; pass --artifact")
    return data_dir / "forecast-analysis" / "artifacts" / digest


def run_route(data_dir, route, options, waypoints, adaptive, max_steps):
    artifact = artifact_path(data_dir, route, options.get("_artifact"))
    polar_name = route.get("name", "routing").removeprefix("routing-")
    polar = weatherrouting.Polar(str(data_dir / "polars" / f"{polar_name}.pol"))
    departure = datetime.fromisoformat(options["departure"]).replace(
        tzinfo=timezone.utc
    )
    algorithm = contour_observing_router(
        weatherrouting.list_routing_algorithms()[0]["class"]
    )
    routing = weatherrouting.Routing(
        algorithm,
        polar,
        waypoints[1:],
        Grib.parse(str(artifact)),
        departure,
        start_position=waypoints[0],
    )
    routing.algorithm.capture_contours = True
    request = SimpleNamespace(isochrone_step="adaptive" if adaptive else "hourly")
    begin = time.perf_counter()
    result = None
    steps = 0
    while not routing.end and steps < max_steps:
        hours = isochrone_step_hours(request, result)
        result = routing.step(timedelta=hours)
        steps += 1
    elapsed = time.perf_counter() - begin
    return routing, elapsed, steps


def trim_run(run, origin):
    """Keep the leading arc of a closed run, away from the leg start."""
    if not run.closed or len(run.points) < 4:
        return run
    scale = max(abs(math.cos(math.radians(origin[0]))), 1e-9)
    ox, oy = origin[1] * scale, origin[0]
    distances = [
        math.hypot(point[1] * scale - ox, point[0] - oy) for point in run.points
    ]
    count = len(run.points)
    first = min(range(count), key=lambda index: distances[index])
    far_side = [
        index
        for index in range(count)
        if min((index - first) % count, (first - index) % count) >= count // 4
    ]
    if not far_side:
        return run
    second = min(far_side, key=lambda index: distances[index])

    def arc(start, end):
        if start <= end:
            return list(range(start, end + 1))
        return list(range(start, count)) + list(range(0, end + 1))

    forward = arc(first, second)
    backward = arc(second, first)
    chosen = (
        forward
        if sum(distances[index] for index in forward) / len(forward)
        >= sum(distances[index] for index in backward) / len(backward)
        else backward
    )
    return ContourRun(
        tuple(run.points[index] for index in chosen),
        False,
        tuple(run.sources[index] for index in chosen) if run.sources else (),
    )


def arc_contours(contours, waypoints):
    slices = []
    for item in contours.slices:
        origin = waypoints[min(item.leg_id, len(waypoints) - 1)]
        slices.append(
            IsochroneSlice(
                item.leg_id,
                item.step,
                item.valid_time,
                tuple(trim_run(run, origin) for run in item.runs),
                item.representation,
                item.frontier,
            )
        )
    return IsochroneContours(tuple(slices), contours.version, contours.representation)


def metrics(contours):
    from shapely.geometry import LineString

    runs = crossings = reversals = 0
    for item in contours.slices:
        for run in item.runs:
            if not run.points:
                continue
            runs += 1
            points = run.points + run.points[:1] if run.closed else run.points
            if len(points) < 2:
                continue
            if not LineString([(lon, lat) for lat, lon in points]).is_simple:
                crossings += 1
            for a, b, c in zip(points, points[1:], points[2:]):
                ax = (b[1] - a[1]) * math.cos(math.radians(a[0]))
                ay = b[0] - a[0]
                bx = (c[1] - b[1]) * math.cos(math.radians(b[0]))
                by = c[0] - b[0]
                if math.hypot(ax, ay) < 1e-9 or math.hypot(bx, by) < 1e-9:
                    continue
                if (ax * bx + ay * by) / (
                    math.hypot(ax, ay) * math.hypot(bx, by)
                ) < -0.8:
                    reversals += 1
    return dict(runs=runs, crossings=crossings, reversals=reversals)


def bounds_for(path, contours, fit="contours"):
    latitudes = [point.pos[0] for point in path]
    longitudes = [point.pos[1] for point in path]
    if fit == "contours":
        for item in contours.slices:
            for run in item.runs:
                for latitude, longitude in run.points:
                    latitudes.append(latitude)
                    longitudes.append(longitude)
    south, north = min(latitudes), max(latitudes)
    west, east = min(longitudes), max(longitudes)
    margin_latitude = max((north - south) * 0.08, 0.02)
    margin_longitude = max((east - west) * 0.08, 0.02)
    return (
        south - margin_latitude,
        west - margin_longitude,
        north + margin_latitude,
        east + margin_longitude,
    )


def draw(out_path, width, height, path, contours, bounds, label):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    context.set_source_rgb(0.90, 0.94, 0.97)
    context.paint()
    view = PreviewView(bounds, width, height)
    context.set_line_width(0.5)
    context.set_source_rgba(0.4, 0.5, 0.6, 0.25)
    step = 0.5
    south, west, north, east = bounds
    latitude = math.ceil(south / step) * step
    while latitude < north:
        _, y = view.convert_geographic_to_screen(MapPoint.new_degrees(latitude, west))
        context.move_to(0, y)
        context.line_to(width, y)
        latitude += step
    longitude = math.ceil(west / step) * step
    while longitude < east:
        x, _ = view.convert_geographic_to_screen(MapPoint.new_degrees(south, longitude))
        context.move_to(x, 0)
        context.line_to(x, height)
        longitude += step
    context.stroke()
    frame = IsochroneFrame((), tuple(path), (), contours, None)
    render_isochrones(view, context, frame, threading.Event())
    context.set_source_rgb(0.1, 0.1, 0.1)
    context.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    context.set_font_size(22)
    context.move_to(20, 34)
    context.show_text(label)
    surface.write_to_png(str(out_path))
    return surface


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--route")
    parser.add_argument("--artifact")
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/isochrone-preview.png")
    )
    parser.add_argument("--adaptive", action="store_true", default=True)
    parser.add_argument("--hourly", dest="adaptive", action="store_false")
    parser.add_argument("--fit", choices=("contours", "route"), default="contours")
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--height", type=int, default=1000)
    args = parser.parse_args()

    route, options, waypoints = load_route(args.data_dir, args.route)
    options["_artifact"] = args.artifact
    routing, elapsed, steps = run_route(
        args.data_dir, route, options, waypoints, args.adaptive, args.max_steps
    )
    contours = collect_contours(routing.algorithm)
    arcs = arc_contours(contours, waypoints)
    bounds = bounds_for(routing.path, contours, args.fit)
    loops_path = args.output.with_name(args.output.stem + "-loops.png")
    arcs_path = args.output.with_name(args.output.stem + "-arcs.png")
    loops = draw(
        loops_path,
        args.width,
        args.height,
        routing.path,
        contours,
        bounds,
        "Closed loops",
    )
    arc_surface = draw(
        arcs_path,
        args.width,
        args.height,
        routing.path,
        arcs,
        bounds,
        "Leading arcs",
    )
    combined = cairo.ImageSurface(cairo.FORMAT_ARGB32, args.width * 2, args.height)
    context = cairo.Context(combined)
    context.set_source_surface(loops, 0, 0)
    context.paint()
    context.set_source_surface(arc_surface, args.width, 0)
    context.paint()
    combined.write_to_png(str(args.output))
    print(
        json.dumps(
            dict(
                steps=steps,
                seconds=round(elapsed, 2),
                slices=len(contours.slices),
                loops=metrics(contours),
                arcs=metrics(arcs),
                output=str(args.output),
            )
        )
    )


if __name__ == "__main__":
    main()
