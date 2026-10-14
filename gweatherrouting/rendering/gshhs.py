# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

import logging
import math
import time
from collections import OrderedDict

import numpy as np

try:
    import shapely
except ImportError:
    shapely = None
from osgeo import ogr

from gweatherrouting.common import resource_path
from gweatherrouting.core.core import LinePointValidityProvider
from gweatherrouting.core.mapviewport import MapPoint
from .chartlayer import ChartLayer

logger = logging.getLogger("gweatherrouting")

from .vectordrawer.simplechartdrawer import SimpleChartDrawer


class GSHHSVectorChart(ChartLayer, LinePointValidityProvider):
    def __init__(self, path, settings_manager, metadata=None):
        super().__init__(path, "vector", settings_manager, metadata)

        self.last_time = {"c": 0, "l": 0, "i": 0, "h": 0, "f": 0}
        self.force_downscale = False

        self.drawer = SimpleChartDrawer(settings_manager)
        drv = ogr.GetDriverByName("ESRI Shapefile")
        self.vector_files = {}
        for x in ["f", "h", "i", "l", "c"]:
            for y in [1]:  # ,2,3,4,5,6]:
                f = drv.Open(
                    path + "/GSHHS_shp/" + x + "/GSHHS_" + x + "_L" + str(y) + ".shp"
                )
                if f is not None:
                    self.vector_files[x + str(y)] = f

        self.lpvFile = drv.Open(path + "/GSHHS_shp/h/GSHHS_h_L1.shp")

        f = open(
            resource_path("gweatherrouting", "data/countries_en.txt"),
            "r",
        )
        self.countries = list(
            filter(
                lambda x: len(x) == 4, map(lambda x: x.split(";"), f.read().split("\n"))
            )
        )
        f.close()

    def on_register(self, on_tick_handler=None):
        pass

    def _routing_source(self, layer_index, feature):
        key = (layer_index, feature.GetFID())
        if key not in self._routing_sources:
            geometry = feature.GetGeometryRef().Clone()
            if not geometry.IsValid():
                geometry = geometry.MakeValid()
            if geometry is None:
                raise ValueError("Unable to repair routing shoreline geometry")
            self._routing_sources[key] = geometry
            if len(self._routing_sources) > 64:
                self._routing_sources.popitem(last=False)
        self._routing_sources.move_to_end(key)
        return self._routing_sources[key]

    def _routing_land(self, west, south, east, north):
        """Cache exact local shoreline geometry, repairing invalid source polygons."""
        # Tile bounds make consecutive integration queries share the same clip.
        bounds = (
            math.floor(west * 4) / 4 - 0.001,
            math.floor(south * 4) / 4 - 0.001,
            math.ceil(east * 4) / 4 + 0.001,
            math.ceil(north * 4) / 4 + 0.001,
        )
        if not hasattr(self, "_routing_clips"):
            self._routing_clips: OrderedDict[tuple, tuple] = OrderedDict()
            self._routing_sources: OrderedDict[tuple, ogr.Geometry] = OrderedDict()
        if bounds in self._routing_clips:
            self._routing_clips.move_to_end(bounds)
            return self._routing_clips[bounds]
        left, bottom, right, top = bounds
        clip = ogr.CreateGeometryFromWkt(
            f"POLYGON (({left} {bottom}, {right} {bottom}, {right} {top}, "
            f"{left} {top}, {left} {bottom}))"
        )
        geometries = []
        for i in range(self.lpvFile.GetLayerCount()):
            layer = self.lpvFile.GetLayerByIndex(i)
            layer.SetSpatialFilterRect(*bounds)
            layer.ResetReading()
            for feature in layer:
                geometry = self._routing_source(i, feature).Intersection(clip)
                if geometry is None:
                    raise ValueError("Unable to clip routing shoreline geometry")
                if not geometry.IsEmpty():
                    geometries.append((geometry, geometry.CreatePreparedGeometry()))
        self._routing_clips[bounds] = tuple(geometries)
        if len(self._routing_clips) > 64:
            self._routing_clips.popitem(last=False)
        return self._routing_clips[bounds]

    def _native_routing_land(self, land):
        """Convert each immutable clipped polygon once for native batch predicates."""
        if not hasattr(self, "_routing_native_clips"):
            self._routing_native_clips: OrderedDict = OrderedDict()
        key = id(land)
        if key not in self._routing_native_clips:
            polygons = []
            for geometry, _ in land:
                native = shapely.from_wkb(bytes(geometry.ExportToWkb()))
                shapely.prepare(native)
                polygons.append((*geometry.GetEnvelope(), native))
            # Retain land itself so its identity cannot be reused while cached.
            self._routing_native_clips[key] = (land, tuple(polygons))
            if len(self._routing_native_clips) > 64:
                self._routing_native_clips.popitem(last=False)
        self._routing_native_clips.move_to_end(key)
        return self._routing_native_clips[key][1]

    def points_validity(self, latlons):
        """Screen polygon envelopes in arrays, then use exact containment."""
        if not len(latlons):
            return []
        points = np.asarray(latlons, dtype=float)
        latitudes, longitudes = points[:, 0], points[:, 1]
        land = self._routing_land(
            longitudes.min(), latitudes.min(), longitudes.max(), latitudes.max()
        )
        valid = np.ones(len(points), dtype=bool)
        if shapely is not None and getattr(self, "use_native_geometry", True):
            for west, east, south, north, native in self._native_routing_land(land):
                possible = np.flatnonzero(
                    valid
                    & (longitudes >= west)
                    & (longitudes <= east)
                    & (latitudes >= south)
                    & (latitudes <= north)
                )
                valid[possible] &= ~shapely.contains_xy(
                    native, longitudes[possible], latitudes[possible]
                )
            return valid.tolist()
        for geometry, prepared in land:
            west, east, south, north = geometry.GetEnvelope()
            possible = np.flatnonzero(
                valid
                & (longitudes >= west)
                & (longitudes <= east)
                & (latitudes >= south)
                & (latitudes <= north)
            )
            for i in possible:
                point = ogr.Geometry(ogr.wkbPoint)
                point.AddPoint(float(longitudes[i]), float(latitudes[i]))
                if (prepared or geometry).Contains(point):
                    valid[i] = False
        return valid.tolist()

    def lines_validity(self, latlons):
        """Screen envelopes without weakening the exact segment intersection."""
        if not len(latlons):
            return []
        lines = np.asarray(latlons, dtype=float)
        left = np.minimum(lines[:, 1], lines[:, 3])
        right = np.maximum(lines[:, 1], lines[:, 3])
        bottom = np.minimum(lines[:, 0], lines[:, 2])
        top = np.maximum(lines[:, 0], lines[:, 2])
        land = self._routing_land(left.min(), bottom.min(), right.max(), top.max())
        valid = np.ones(len(lines), dtype=bool)
        if shapely is not None and getattr(self, "use_native_geometry", True):
            segments = None
            for west, east, south, north, native in self._native_routing_land(land):
                possible = np.flatnonzero(
                    valid
                    & (right >= west)
                    & (left <= east)
                    & (top >= south)
                    & (bottom <= north)
                )
                if not len(possible):
                    continue
                if segments is None:
                    segments = shapely.linestrings(lines.reshape(-1, 2, 2)[:, :, ::-1])
                valid[possible] &= ~shapely.intersects(native, segments[possible])
            return valid.tolist()
        for geometry, prepared in land:
            west, east, south, north = geometry.GetEnvelope()
            possible = np.flatnonzero(
                valid
                & (right >= west)
                & (left <= east)
                & (top >= south)
                & (bottom <= north)
            )
            for i in possible:
                lat1, lon1, lat2, lon2 = lines[i]
                line = ogr.Geometry(ogr.wkbLineString)
                line.AddPoint(float(lon1), float(lat1))
                line.AddPoint(float(lon2), float(lat2))
                if (prepared or geometry).Intersects(line):
                    valid[i] = False
        return valid.tolist()

    def do_draw(self, gpsmap, cr):
        bounding_geometry = self.get_bounding_geometry(gpsmap)

        # Get the correct quality for zoom level
        scale = gpsmap.get_scale()
        if scale > 5000:
            q = "c"
        elif scale > 500:  # or self.last_time['i'] > 1.0:
            q = "l"
        elif scale > 200 or self.force_downscale:  # or self.last_time['h'] > 1.0
            q = "i"
        elif scale >= 15 or self.last_time["f"] > 0.25:
            q = "h"
        elif scale < 15:
            q = "f"

        t = time.time()
        self.drawer.draw(gpsmap, cr, self.vector_files[q + "1"], bounding_geometry)
        self.last_time[q] = int(time.time() - t)

        if scale < 4500:
            for c in self.countries:
                # TODO: check if x[2],x[3] is inside the bounding_geometry

                # render country name
                cr.set_source_rgba(0, 0, 0, 0.8)
                cr.set_font_size(9)
                x, y = gpsmap.convert_geographic_to_screen(
                    MapPoint.new_degrees(float(c[2]), float(c[3]))
                )
                cr.move_to(x, y)
                cr.show_text(c[1])

    def do_render(self, gpsmap):
        pass

    def do_busy(self):
        return False

    def do_button_press(self, gpsmap, gdkeventbutton):
        return False
