"""Immutable marine samples, independent of wind fields and display toolkits."""

from dataclasses import dataclass
from math import asinh, atan, ceil, cos, degrees, floor, pi, radians, sin, sinh, tan

from gweatherrouting.core.mapviewport import MapPoint, MapViewport
from gweatherrouting.core.marine.fields import FieldSelection
from .marinemaplayer import current_grid_spacing, current_style, flow


@dataclass(frozen=True)
class CurrentArrow:
    latitude: float
    longitude: float
    east: float
    north: float
    station: bool = False


@dataclass(frozen=True)
class MarineSnapshot:
    sources: object
    instant: object
    arrows: tuple
    state: str


def sample_currents(sources, instant, camera, cancelled, on_land):
    """Use marine selections unchanged, including their effective-time policy.

    Station coordinates are never thinned or displaced to the field lattice.
    The shoreline predicate is worker-owned; no Quick objects cross this API.
    """
    view = camera.view()
    arrows = []
    selection = FieldSelection(sources.fields)
    if sources.fields:
        # MapViewport's normalized Web Mercator world, not a screen lattice.
        # Half-up zoom quantization keeps levels dyadic: on refinement all old
        # anchors survive, and on coarsening only the intervening points vanish.
        level = max(0, floor(camera.zoom + 0.5))
        cells = max(
            1,
            round(
                MapViewport.TILE_SIZE * 2**level / current_grid_spacing(sources.density)
            ),
        )
        world_pixels = MapViewport.TILE_SIZE * 2**camera.zoom
        cx = (camera.center_lon + 180) / 360
        latitude = max(
            -MapViewport.MAX_LAT, min(MapViewport.MAX_LAT, camera.center_lat)
        )
        cy = (1 - asinh(tan(radians(latitude))) / pi) / 2
        left, right = cx - camera.width / (2 * world_pixels), cx + camera.width / (
            2 * world_pixels
        )
        top, bottom = max(0, cy - camera.height / (2 * world_pixels)), min(
            1, cy + camera.height / (2 * world_pixels)
        )
        # One canonical longitude per world point, even for views wider than
        # the world. Geometry uses the nearest wrapped copy, just like stations.
        columns = tuple(
            dict.fromkeys(
                i % cells for i in range(ceil(left * cells), floor(right * cells) + 1)
            )
        )
        for row in range(ceil(top * cells), floor(bottom * cells) + 1):
            if cancelled():
                return None
            lat = degrees(atan(sinh(pi * (1 - 2 * row / cells))))
            for column in columns:
                lon = column / cells * 360 - 180
                if on_land(lat, lon):
                    continue
                result = selection.sample(lat, lon, instant)
                if result.status == "available":
                    arrows.append(CurrentArrow(lat, lon, *result.values))
    seen = set()
    for dataset in sources.currents.datasets:
        if cancelled():
            return None
        station = dataset.station
        if station.id in seen:
            continue
        seen.add(station.id)
        lon = (
            camera.center_lon
            + (station.longitude - camera.center_lon + 180) % 360
            - 180
        )
        x, y = view.convert_geographic_to_screen(MapPoint(station.latitude, lon))
        if not (0 <= x <= camera.width and 0 <= y <= camera.height):
            continue
        result = sources.currents.sample(station.id, instant)
        if result.status == "available":
            arrows.append(
                CurrentArrow(station.latitude, station.longitude, *result.values, True)
            )
    state = "available" if arrows else "no valid data"
    if not sources.fields and not sources.currents.datasets:
        state = "unavailable"
    return MarineSnapshot(sources, instant, tuple(arrows), state)


def current_triangles(snapshot, camera):
    """Reproject geographic anchors; glyph dimensions remain screen pixels.

    Returns triangles with per-vertex RGBA, suitable for one colored QSG batch.
    Filled shafts/heads match the legacy speed/direction scale. Station rings
    and calm dots are triangulated, not rasterized.
    """
    vertices = []
    view = camera.view()

    def triangle(points, color):
        vertices.extend((*point, *color) for point in points)

    for arrow in snapshot.arrows:
        lon = (
            camera.center_lon + (arrow.longitude - camera.center_lon + 180) % 360 - 180
        )
        x, y = view.convert_geographic_to_screen(MapPoint(arrow.latitude, lon))
        if not (-40 <= x <= camera.width + 40 and -40 <= y <= camera.height + 40):
            continue
        speed, bearing = flow(arrow.east, arrow.north)
        length, rgb = current_style(speed)
        color = tuple(round(c * 255) for c in rgb) + (255,)

        def circle(radius, inner, rgba):
            for i in range(24):
                a, b = i * pi / 12, (i + 1) * pi / 12
                p = (x + radius * cos(a), y + radius * sin(a))
                q = (x + radius * cos(b), y + radius * sin(b))
                r = (x + inner * cos(a), y + inner * sin(a))
                triangle((p, q, r), rgba)
                if inner:
                    s = (x + inner * cos(b), y + inner * sin(b))
                    triangle((q, s, r), rgba)

        if arrow.station:
            circle(8.75, 7.25, (255, 178, 46, 255))
        if speed < 0.05:
            circle(2.5, 0, color)
            continue
        angle = bearing * pi / 180

        def point(px, py):
            return (
                x + px * cos(angle) - py * sin(angle),
                y + px * sin(angle) + py * cos(angle),
            )

        head, shaft = length * 0.25, length * 0.11
        triangle(
            tuple(
                point(*p)
                for p in (
                    (0, -length / 2),
                    (head, -length * 0.05),
                    (-head, -length * 0.05),
                )
            ),
            color,
        )
        a, b, c, d = (
            point(*p)
            for p in (
                (-shaft, -length * 0.05),
                (shaft, -length * 0.05),
                (shaft, length / 2),
                (-shaft, length / 2),
            )
        )
        triangle((a, b, c), color)
        triangle((a, c, d), color)
    return tuple(vertices)


class MarinePointMask:
    """Worker-local vector shoreline test; never creates a Cairo surface.

    Model masks still belong to FieldSelection. Installed detailed shoreline
    polygons take precedence over the country-outline fallback, as before.
    This masks glyph anchors, not tips crossing a shoreline.
    """

    def __init__(self, camera):
        from pathlib import Path
        from osgeo import ogr
        from shapely import from_wkb, make_valid, STRtree
        from shapely.geometry import shape
        from gweatherrouting.core.storage import DATA_DIR
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
                Path(DATA_DIR)
                / "gshhs"
                / "GSHHS_shp"
                / quality
                / ("GSHHS_" + quality + "_L1.shp")
            )
            if not path.exists():
                continue
            source = ogr.Open(str(path))
            if source is not None:
                detailed = True
                layer = source.GetLayer(0)
                for shift in (-360, 0, 360):
                    left, right = max(-180, west - shift), min(180, east - shift)
                    if left >= right:
                        continue
                    layer.SetSpatialFilterRect(left, south, right, north)
                    layer.ResetReading()
                    polygons.extend(
                        from_wkb(bytes(f.GetGeometryRef().ExportToWkb())) for f in layer
                    )
                break
        if not detailed:
            polygons = [shape(f["geometry"]) for f in COUNTRY_SHAPES]
        self.tree = STRtree([p if p.is_valid else make_valid(p) for p in polygons])

    def __call__(self, latitude, longitude):
        from shapely.geometry import Point

        point = Point((longitude + 180) % 360 - 180, latitude)
        return bool(len(self.tree.query(point, predicate="intersects")))
