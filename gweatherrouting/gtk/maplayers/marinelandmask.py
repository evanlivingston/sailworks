"""Cached shoreline clipping for current glyphs, independent of model masks."""

import json
from collections import OrderedDict
from pathlib import Path

import cairo
import numpy as np
from osgeo import ogr

from gweatherrouting.core.geo.shoreline import ShorelineGeometryCache
from gweatherrouting.core.storage import DATA_DIR
from gweatherrouting.core.utils import COUNTRY_SHAPES


def polygon_path(cr, view, geometry, shift=0):
    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    if kind == ogr.wkbPolygon:
        for ring in geometry:
            points = ring.GetPoints()
            if not points:
                continue
            xs, ys = view.convert_geographic_to_screen_batch(
                np.array([p[1] for p in points]),
                np.array([p[0] + shift for p in points]),
            )
            cr.move_to(xs[0], ys[0])
            for x, y in zip(xs[1:], ys[1:]):
                cr.line_to(x, y)
            cr.close_path()
    elif kind in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        for child in geometry:
            polygon_path(cr, view, child, shift)


class MarineLandMask:
    """Private worker handles; reuse shoreline rasters throughout time playback."""

    def __init__(self, directory=None):
        self.directory = Path(directory or DATA_DIR) / "gshhs" / "GSHHS_shp"
        self._cache = OrderedDict()
        self._sources = ShorelineGeometryCache()

    def surface(self, view):
        key = (view._center_lat, view._center_lon, view._zoom, view.width, view.height)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        surface = cairo.ImageSurface(cairo.FORMAT_A8, view.width, view.height)
        cr = cairo.Context(surface)
        cr.set_source_rgba(1, 1, 1, 1)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        north, west = view.convert_screen_to_geographic(0, 0).get_degrees()
        south, east = view.convert_screen_to_geographic(
            view.width, view.height
        ).get_degrees()
        # Prefer the installed chart's detailed shoreline over country outlines.
        dataset = None
        for quality in ("f", "h", "i", "l", "c"):
            path = self.directory / quality / ("GSHHS_" + quality + "_L1.shp")
            if path.exists():
                dataset = ogr.Open(str(path))
                if dataset is not None:
                    break
        for shift in (-360, 0, 360):
            left, right = max(-180, west - shift), min(180, east - shift)
            if left >= right:
                continue
            bounds = ogr.CreateGeometryFromWkt(
                f"POLYGON (({left} {south}, {right} {south}, {right} {north}, "
                f"{left} {north}, {left} {south}))"
            )
            if dataset is not None:
                layer = dataset.GetLayer(0)
                layer.SetSpatialFilterRect(left, south, right, north)
                layer.ResetReading()
                geometries = (
                    ((str(path), feature.GetFID()), feature.GetGeometryRef().Clone())
                    for feature in layer
                )
            else:
                geometries = (
                    (
                        ("country", index),
                        ogr.CreateGeometryFromJson(json.dumps(feature["geometry"])),
                    )
                    for index, feature in enumerate(COUNTRY_SHAPES)
                    if feature["properties"]["bbox"][0][1] <= right
                    and feature["properties"]["bbox"][1][1] >= left
                    and feature["properties"]["bbox"][0][0] <= north
                    and feature["properties"]["bbox"][1][0] >= south
                )
            for source_key, geometry in geometries:
                polygon_path(
                    cr, view, self._sources.clip(source_key, geometry, bounds), shift
                )
                cr.fill()
        surface.flush()
        self._cache[key] = surface
        while len(self._cache) > 2:
            self._cache.popitem(last=False)
        return surface

    @staticmethod
    def contains(surface, x, y):
        return surface.get_data()[int(y) * surface.get_stride() + int(x)] != 0

    @staticmethod
    def clear(cr, surface):
        cr.save()
        cr.set_operator(cairo.OPERATOR_DEST_OUT)
        cr.set_source_rgba(1, 1, 1, 1)
        cr.mask_surface(surface, 0, 0)
        cr.restore()
