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

import math

from osgeo import ogr

from gweatherrouting.core.enccharts import chart_scale_visible
from gweatherrouting.rendering.cm93datasource import CM93DataSource
from gweatherrouting.rendering.cm93driver import CM93Driver
from gweatherrouting.rendering.vectordrawer.osmchartdrawer import OSMChartDrawer

from .chartlayer import ChartLayer
from .vectordrawer import (
    CM93ChartDrawer,
    S57ChartDrawer,
    SimpleChartDrawer,
    VectorChartDrawer,
)


class GDALVectorChart(ChartLayer):
    def __init__(self, path, settings_manager, metadata=None, enabled=True):
        super().__init__(path, "vector", settings_manager, metadata, enabled)

        self.drawer: VectorChartDrawer

        if path.find("geojson") != -1:
            drv_name = "GeoJSON"
            self.drawer = SimpleChartDrawer(settings_manager)
        elif path.find("shp") != -1:
            drv_name = "ESRI Shapefile"
            self.drawer = SimpleChartDrawer(settings_manager)
        elif path.find(".000") != -1:
            drv_name = "S57"
            self.drawer = S57ChartDrawer(settings_manager)
        elif path.find("Cm93") != -1:
            drv_name = "CM93"
            self.drawer = CM93ChartDrawer(settings_manager)
        elif path.find("osm") != -1 or path.find("pbf") != -1:
            drv_name = "OSM"
            self.drawer = OSMChartDrawer(self.settings_manager)
        else:
            raise Exception("Invalid format")

        if drv_name == "CM93":
            drv = CM93Driver()
        else:
            drv = ogr.GetDriverByName(drv_name)
        self.vector_file = drv.Open(path)

        if self.vector_file is None:
            raise Exception("Unable to open vector map %s" % path)

        self._coverage: list[ogr.Geometry] = []
        self._native_scale = 0
        if drv_name == "S57":
            self._load_enc_metadata()
        self._coverage_envelopes = tuple(g.GetEnvelope() for g in self._coverage)

    def _load_enc_metadata(self):
        identification = self.vector_file.GetLayerByName("DSID")
        if identification:
            identification.ResetReading()
            feature = identification.GetNextFeature()
            if feature and feature.GetFieldIndex("DSPM_CSCL") >= 0:
                self._native_scale = feature.GetField("DSPM_CSCL") or 0
        self._coverage = self.read_coverage(self.vector_file)

    @staticmethod
    def read_coverage(vector_file):
        """Read actual ENC coverage, preserving holes and no-coverage panels."""
        coverage: list[ogr.Geometry] = []
        exclusions: list[ogr.Geometry] = []
        layer = vector_file.GetLayerByName("M_COVR")
        if layer is None:
            return coverage
        layer.ResetReading()
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is not None:
                geometry = geometry.Clone()
                if not geometry.IsValid():
                    geometry = geometry.MakeValid()
                if feature.GetField("CATCOV") == 1:
                    coverage.append(geometry)
                elif feature.GetField("CATCOV") == 2:
                    exclusions.append(geometry)
        for exclusion in exclusions:
            coverage = [g.Difference(exclusion) for g in coverage]
        return [g for g in coverage if g is not None and not g.IsEmpty()]

    def covers_point(self, lat, lon, scale):
        if not chart_scale_visible(self._native_scale, scale):
            return False
        if isinstance(self.vector_file, CM93DataSource):
            level = self.vector_file.get_scale_for_zoom(scale)
            return bool(self.vector_file.get_cells_for_bbox(lat, lon, lat, lon, level))
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(lon, lat)
        return any(geometry.Intersects(point) for geometry in self._coverage)

    def on_register(self, on_tick_handler=None):
        return True

    def do_draw(self, gpsmap, cr):
        bounding_geometry = self.get_bounding_geometry(gpsmap)
        if isinstance(self.drawer, S57ChartDrawer):
            if not chart_scale_visible(self._native_scale, gpsmap.get_scale()):
                return
            # Avoid scanning offscreen charts, including their attribute layers.
            if self._coverage and not any(
                g.Intersects(bounding_geometry) for g in self._coverage
            ):
                return
        self.drawer.draw(gpsmap, cr, self.vector_file, bounding_geometry)

    def do_render(self, gpsmap):
        pass

    def do_busy(self):
        return False

    def query_point(self, lat, lon, tolerance=0.001):
        """Query vector chart features near a given lat/lon.

        Returns a list of dicts with layer name, geometry type, and attributes.
        """
        if isinstance(self.vector_file, CM93DataSource):
            return self.vector_file.query_point(lat, lon, tolerance)

        results = []
        bbox = ogr.CreateGeometryFromWkt(
            self.get_bounding_wkt_of_coords(
                lat - tolerance, lon - tolerance, lat + tolerance, lon + tolerance
            )
        )

        for i in range(self.vector_file.GetLayerCount()):
            layer = self.vector_file.GetLayerByIndex(i)
            layer.SetSpatialFilter(bbox)
            layer.ResetReading()

            feat = layer.GetNextFeature()
            while feat is not None:
                attrs = {}
                for j in range(feat.GetFieldCount()):
                    fd = feat.GetFieldDefnRef(j)
                    val = feat.GetFieldAsString(j)
                    if val:
                        attrs[fd.GetNameRef()] = val

                geom = feat.GetGeometryRef()
                geom_type = geom.GetGeometryName() if geom else "Unknown"

                results.append(
                    {
                        "layer": layer.GetName(),
                        "geometry": geom_type,
                        "attributes": attrs,
                    }
                )
                feat = layer.GetNextFeature()

        return results

    def depth_at(self, lat, lon, tolerance=0.001):
        """Nearest sounding depth and enclosing depth area near a point.

        Returns {"sounding": metres, "distance_m": metres} and/or
        {"area": (min, max)} in metres, or None when this chart has no
        depth data near the point.
        """
        if not isinstance(self.drawer, S57ChartDrawer):
            return None
        bbox = ogr.CreateGeometryFromWkt(
            self.get_bounding_wkt_of_coords(
                lat - tolerance, lon - tolerance, lat + tolerance, lon + tolerance
            )
        )
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(lon, lat)
        result = {}
        cosine = math.cos(math.radians(lat))
        layer = self.vector_file.GetLayerByName("SOUNDG")
        if layer is not None:
            sounding = self._nearest_sounding(layer, bbox, lat, lon, tolerance, cosine)
            if sounding is not None:
                result["sounding"] = sounding[1]
                result["distance_m"] = sounding[0] ** 0.5 * 111320
        layer = self.vector_file.GetLayerByName("DEPARE")
        if layer is not None:
            area = self._enclosing_depth_area(layer, bbox, point)
            if area is not None:
                result["area"] = area
        return result or None

    def _nearest_sounding(self, layer, bbox, lat, lon, tolerance, cosine):
        """Closest sounding point inside the tolerance box, or None."""
        previous = layer.GetSpatialFilter()
        previous = previous.Clone() if previous is not None else None
        layer.SetSpatialFilter(bbox)
        layer.ResetReading()
        try:
            best = None
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is None:
                    continue
                for point_geometry in self._point_geometries(geometry):
                    candidate = self._sounding_candidate(
                        point_geometry, lat, lon, tolerance, cosine
                    )
                    if candidate is not None and (
                        best is None or candidate[0] < best[0]
                    ):
                        best = candidate
            return best
        finally:
            layer.SetSpatialFilter(previous)

    @staticmethod
    def _point_geometries(geometry):
        count = geometry.GetGeometryCount()
        if count:
            return (geometry.GetGeometryRef(i) for i in range(count))
        return (geometry,)

    @staticmethod
    def _sounding_candidate(point_geometry, lat, lon, tolerance, cosine):
        """Distance and depth of one sounding point, or None if it is unusable."""
        if point_geometry.GetCoordinateDimension() < 3:
            return None
        x, y, z = point_geometry.GetPoint()
        if not all(math.isfinite(value) for value in (x, y, z)):
            return None
        # A matched multipoint may also contain distant soundings.
        if abs(x - lon) > tolerance or abs(y - lat) > tolerance:
            return None
        distance = ((x - lon) * cosine) ** 2 + (y - lat) ** 2
        return (distance, z)

    def _enclosing_depth_area(self, layer, bbox, point):
        """First depth area that encloses the point, or None."""
        previous = layer.GetSpatialFilter()
        previous = previous.Clone() if previous is not None else None
        layer.SetSpatialFilter(bbox)
        layer.ResetReading()
        try:
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is not None and geometry.Intersects(point):
                    area = (
                        feature.GetField("DRVAL1"),
                        feature.GetField("DRVAL2"),
                    )
                    if (
                        all(
                            value is not None and math.isfinite(value) for value in area
                        )
                        and area[0] <= area[1]
                    ):
                        return area
            return None
        finally:
            layer.SetSpatialFilter(previous)

    def do_button_press(self, gpsmap, gdkeventbutton):
        return False
