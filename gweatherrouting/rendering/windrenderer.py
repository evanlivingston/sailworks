"""Background wind portrayal with private GDAL handles and cached viewports."""

from collections import OrderedDict
from gweatherrouting.core.scheduling import schedule_idle
from functools import lru_cache
import json
import logging
from types import SimpleNamespace

import cairo
import numpy as np
from osgeo import ogr

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.utils import COUNTRY_SHAPES, point_in_bbox
from gweatherrouting.rendering.overlayrenderer import OverlayRenderer

logger = logging.getLogger("gweatherrouting")


class WindLandMask:
    """Keep expensive polygon containment out of Python's per-vertex loops."""

    def __init__(self):
        self._prepared = {}
        self.contains = lru_cache(maxsize=16384)(self._contains)

    def _contains(self, lat, lon):
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(lon, lat)
        for index, feature in enumerate(COUNTRY_SHAPES):
            if not point_in_bbox(feature["properties"]["bbox"], lat, lon):
                continue
            if index not in self._prepared:
                geometry = ogr.CreateGeometryFromJson(json.dumps(feature["geometry"]))
                self._prepared[index] = (geometry, geometry.CreatePreparedGeometry())
            geometry, prepared = self._prepared[index]
            if (prepared or geometry).Contains(point):
                return True
        return False


class DisplayGrib(Grib):
    """Crop before interpolation, without building/scanning a global tuple grid.

    Only the display worker uses these handles and arrays. Routing continues to
    use its existing GRIB provider and interpolation unchanged.
    """

    def __init__(self, source):
        super().__init__(
            source.path,
            source.name,
            source.centre,
            source.bounds,
            source.start_time,
            source.last_forecast,
        )
        self._arrays = OrderedDict()
        self._pressure_arrays = OrderedDict()
        transform = self.dataset.GetGeoTransform()
        self._lons = transform[0] + np.arange(self.dataset.RasterXSize) * transform[1]
        self._lats = transform[3] + np.arange(self.dataset.RasterYSize) * transform[5]

    def _get_wind_data(self, hour, bounds):
        rows = np.flatnonzero(
            (self._lats >= bounds[0][0]) & (self._lats <= bounds[1][0])
        )
        columns = np.flatnonzero(
            (self._lons >= bounds[0][1]) & (self._lons <= bounds[1][1])
        )
        if not len(rows) or not len(columns):
            return []
        arrays = self._arrays.get(hour)
        if arrays is None:
            u_band, v_band = self._find_bands_for_time(hour)
            if u_band is None or v_band is None:
                return None
            arrays = (u_band.ReadAsArray(), v_band.ReadAsArray())
            self._arrays[hour] = arrays
            if len(self._arrays) > 4:
                self._arrays.popitem(last=False)
        self._arrays.move_to_end(hour)
        u, v = (array[np.ix_(rows, columns)] for array in arrays)
        lons, lats = np.meshgrid(self._lons[columns], self._lats[rows])
        return list(zip(lats.ravel(), lons.ravel(), u.ravel(), v.ravel()))

    def _get_pressure_data(self, hour, bounds):
        rows = np.flatnonzero(
            (self._lats >= bounds[0][0]) & (self._lats <= bounds[1][0])
        )
        columns = np.flatnonzero(
            (self._lons >= bounds[0][1]) & (self._lons <= bounds[1][1])
        )
        if not len(rows) or not len(columns):
            return []
        array = self._pressure_arrays.get(hour)
        if array is None:
            band = self._find_pressure_band_for_time(hour)
            if band is None:
                return None
            array = band.ReadAsArray()
            self._pressure_arrays[hour] = array
            if len(self._pressure_arrays) > 4:
                self._pressure_arrays.popitem(last=False)
        self._pressure_arrays.move_to_end(hour)
        cropped = array[np.ix_(rows, columns)]
        lons, lats = np.meshgrid(self._lons[columns], self._lats[rows])
        return list(zip(lats.ravel(), lons.ravel(), cropped.ravel().astype(float)))


class WindOverlayRenderer(OverlayRenderer):
    """Render wind using a bounded queue, cancellation and image reuse."""

    def __init__(self, painter, schedule=schedule_idle):
        super().__init__(schedule)
        self._painter = painter
        self._gribs = OrderedDict()
        self._land_mask = WindLandMask()

    def _can_preview(self, job, key):
        # Playback changes the forecast before its replacement image is ready.
        # Keep the last completed forecast visible, then swap complete images.
        # Changes to sources, display style or pixel density still invalidate it.
        return job.key[1:] == key[1:] and job.palette[1:] == key[0][1:]

    def _worker_grib(self, source):
        grib = self._gribs.get(source)
        if grib is None:
            grib = DisplayGrib(source)
            self._gribs[source] = grib
            if len(self._gribs) > 2:
                self._gribs.popitem(last=False)
        self._gribs.move_to_end(source)
        return grib

    @staticmethod
    def _viewport_bounds(job):
        p1, p2 = (p.get_degrees() for p in job.viewport.get_bbox())
        return (
            (min(p1[0], p2[0]) - 1, min(p1[1], p2[1]) - 1),
            (max(p1[0], p2[0]) + 1, max(p1[1], p2[1]) + 1),
        )

    def _load_wind(self, job):
        bounds = self._viewport_bounds(job)
        columns: dict[float, list[tuple[float, float, tuple[float, float]]]] = {}
        # Merge overlapping forecasts by issue time, with one value per grid
        # coordinate. Duplicate vertices make ragged, overlapping gradient cells.
        points: dict[tuple, tuple] = {}
        sources = sorted(
            job.charts,
            key=lambda item: (
                item[0].start_time.timestamp()
                if hasattr(item[0], "start_time")
                else float("-inf")
            ),
            reverse=True,
        )
        for (source,) in sources:
            if job.cancel.is_set():
                return []
            try:
                wind = self._worker_grib(source).get_wind(job.palette[0], bounds) or []
            except Exception as exc:
                # Match GribManager: one unavailable forecast must not hide the
                # other enabled weather sources.
                logger.debug("Wind display source unavailable: %s", exc)
                continue
            for point in wind:
                if np.isfinite(point[:2]).all():
                    points.setdefault(tuple(point[2]), point)
        for point in points.values():
            columns.setdefault(point[2][1], []).append(point)
        return [
            sorted(columns[lon], key=lambda point: point[2][0])
            for lon in sorted(columns)
        ]

    def _load_pressure(self, job):
        """Time-interpolated MSL pressure columns mirroring _load_wind.

        Returns columns of (lat, lon, hPa) sorted by latitude, or [] when no
        enabled source carries pressure bands (e.g. older wind-only GRIBs).
        """
        bounds = self._viewport_bounds(job)
        columns: dict[float, list] = {}
        # Newest forecast wins per grid coordinate, as with wind.
        points: dict[tuple, tuple] = {}
        sources = sorted(
            job.charts,
            key=lambda item: (
                item[0].start_time.timestamp()
                if hasattr(item[0], "start_time")
                else float("-inf")
            ),
            reverse=True,
        )
        for (source,) in sources:
            if job.cancel.is_set():
                return []
            try:
                pressure = (
                    self._worker_grib(source).get_pressure(job.palette[0], bounds) or []
                )
            except Exception as exc:
                logger.debug("Pressure display source unavailable: %s", exc)
                continue
            for lat, lon, hpa in pressure:
                if np.isfinite((lat, lon, hpa)).all():
                    points.setdefault((lat, lon), (lat, lon, hpa))
        for lat, lon, hpa in points.values():
            columns.setdefault(lon, []).append((lat, lon, hpa))
        return [
            sorted(columns[lon], key=lambda point: point[0]) for lon in sorted(columns)
        ]

    @staticmethod
    def _show_isobars(job):
        # Palettes built before the isobar toggle carry no fourth element.
        return job.palette[3] if len(job.palette) > 3 else True

    def _render(self, job):
        data = self._load_wind(job)
        if job.cancel.is_set():
            return None
        pressure = self._load_pressure(job) if self._show_isobars(job) else []
        if job.cancel.is_set():
            return None
        viewport = job.viewport
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            viewport.width * job.factor,
            viewport.height * job.factor,
        )
        surface.set_device_scale(job.factor, job.factor)
        settings = SimpleNamespace(
            gribArrowOpacity=job.palette[1],
            gribArrowOnGround=job.palette[2],
            gribIsobarsVisible=self._show_isobars(job),
        )
        self._painter(settings, self._land_mask.contains).draw(
            viewport, cairo.Context(surface), data, pressure
        )
        surface.flush()
        return None if job.cancel.is_set() else surface
