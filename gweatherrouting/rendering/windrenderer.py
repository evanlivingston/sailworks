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
from gweatherrouting.core.gribsampling import WeatherSampler
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

    Workers use private handles. The shared sampler supplies all numerical
    space/time interpolation; legacy crop helpers remain for compatibility.
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
        self._lons = (
            transform[0] + (np.arange(self.dataset.RasterXSize) + 0.5) * transform[1]
        )
        self._lats = (
            transform[3] + (np.arange(self.dataset.RasterYSize) + 0.5) * transform[5]
        )

    def close(self):
        self._arrays.clear()
        self._pressure_arrays.clear()
        super().close()

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

    def _release_if_idle(self):
        if not self._states and not self._running:
            for grib in self._gribs.values():
                grib.close()
            self._gribs.clear()

    def forget(self, canvas):
        super().forget(canvas)
        self._release_if_idle()

    def _completed(self, canvas, job, surface, error):
        result = super()._completed(canvas, job, surface, error)
        self._release_if_idle()
        return result

    def _can_preview(self, job, key):
        # Playback changes the forecast before its replacement image is ready.
        # Keep the last completed forecast visible, then swap complete images.
        # Changes to sources, display style or pixel density still invalidate it.
        return job.key[1:] == key[1:] and job.palette[1:] == key[0][1:]

    def _can_complete(self, job, key):
        # Continuous scrubbing must not starve the display by cancelling every
        # in-flight forecast. Publish completed previews while the latest time
        # waits in the bounded queue; style/source changes still discard them.
        return self._can_preview(job, key)

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

    def _load_field(self, job, field):
        providers = []
        try:
            for (source,) in job.charts:
                if job.cancel.is_set():
                    return []
                try:
                    providers.append((source, self._worker_grib(source)))
                except Exception as exc:
                    logger.debug("Weather source unavailable: %s", exc)
            sources = [provider for _, provider in providers]
            if sources and all(isinstance(source, Grib) for source in sources):
                sampler = WeatherSampler(sources)
                data = sampler.grid(
                    field, job.palette[0], self._viewport_bounds(job), job.cancel.is_set
                )
            else:
                data = self._provider_field(job, field, providers)
            return self._columns(data, field)
        finally:
            # Split-file sampling needs every selected handle until completion.
            # Release evictions afterwards, rather than leaving dataset cycles
            # and decoded grids waiting for Python's garbage collector.
            for _, provider in providers:
                if isinstance(provider, Grib) and provider not in self._gribs.values():
                    provider.close()

    def _provider_field(self, job, field, providers):
        data: list[tuple] = []
        ordered = sorted(
            providers,
            key=lambda pair: (
                pair[0].start_time.timestamp()
                if hasattr(pair[0], "start_time")
                else float("-inf")
            ),
            reverse=True,
        )
        for _, provider in ordered:
            try:
                data.extend(
                    getattr(provider, "get_" + field)(
                        job.palette[0], self._viewport_bounds(job)
                    )
                    or []
                )
            except Exception as exc:
                logger.debug("Weather source unavailable: %s", exc)
        return data

    @staticmethod
    def _columns(data, field):
        points: dict[tuple, tuple] = {}
        for point in data:
            coordinate = point[2] if field == "wind" else point[:2]
            if np.isfinite(point[:2] if field == "wind" else point).all():
                points.setdefault(tuple(coordinate), point)
        columns: dict[float, list] = {}
        for coordinate, point in points.items():
            columns.setdefault(coordinate[1], []).append(point)
        return [
            sorted(columns[lon], key=lambda p: p[2][0] if field == "wind" else p[0])
            for lon in sorted(columns)
        ]

    def _load_wind(self, job):
        return self._load_field(job, "wind")

    def _load_pressure(self, job):
        return self._load_field(job, "pressure")

    @staticmethod
    def _show_isobars(job):
        # Palettes built before the isobar toggle carry no fourth element.
        return job.palette[3] if len(job.palette) > 3 else True

    def _render(self, job):
        show_wind = job.palette[4] if len(job.palette) > 4 else True
        data = self._load_wind(job) if show_wind else []
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
            gribIsobarInterval=job.palette[5] if len(job.palette) > 5 else 4.0,
            gribIsobarOpacity=job.palette[6] if len(job.palette) > 6 else 0.85,
            gribIsobarLabels=job.palette[7] if len(job.palette) > 7 else True,
        )
        self._painter(settings, self._land_mask.contains).draw(
            viewport, cairo.Context(surface), data, pressure
        )
        surface.flush()
        return None if job.cancel.is_set() else surface
