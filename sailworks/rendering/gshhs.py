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
import time
from collections import OrderedDict

try:
    import shapely
except ImportError:  # pragma: no cover - optional acceleration only
    shapely = None
from osgeo import ogr

from sailworks.common import resource_path
from sailworks.core.core import LinePointValidityProvider
from sailworks.core.geo.shoreline import ShorelineValidity
from sailworks.core.mapviewport import MapPoint
from .chartlayer import ChartLayer

logger = logging.getLogger("sailworks")

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

        self.shoreline = ShorelineValidity(path)
        # Kept for the chart-collection completeness check.
        self.lpvFile = self.shoreline.lpvFile

        f = open(
            resource_path("sailworks", "data/countries_en.txt"),
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

    @property
    def shoreline(self):
        """Lazily build the handle-local predicate for test/legacy instances."""
        value = self.__dict__.get("_shoreline")
        if value is None:
            value = ShorelineValidity.__new__(ShorelineValidity)
            value.path = getattr(self, "path", "")
            value.use_native_geometry = getattr(self, "use_native_geometry", True)
            value.lpvFile = self.lpvFile
            value._routing_clips = OrderedDict()
            value._routing_sources = OrderedDict()
            value._routing_native_clips = OrderedDict()
            self._shoreline = value
        return value

    @shoreline.setter
    def shoreline(self, value):
        self._shoreline = value

    def _validity_backend(self):
        self.shoreline.use_native_geometry = shapely is not None and getattr(
            self, "use_native_geometry", True
        )
        return self.shoreline

    def points_validity(self, latlons):
        return self._validity_backend().points_validity(latlons)

    def lines_validity(self, latlons, clearance_distance_m=None):
        if clearance_distance_m is None:
            clearance_distance_m = getattr(self, "coastline_clearance_distance_m", None)
        return self._validity_backend().lines_validity(latlons, clearance_distance_m)

    def validity_descriptor(self):
        return ("gshhs", self.path)

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

        t = time.perf_counter()
        self.drawer.draw(gpsmap, cr, self.vector_files[q + "1"], bounding_geometry)
        self.last_time[q] = time.perf_counter() - t

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
