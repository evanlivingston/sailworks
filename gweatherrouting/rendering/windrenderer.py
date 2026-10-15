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
from gweatherrouting.rendering.windpainter import WindPainter

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

    @staticmethod
    def _columns_share_lats(columns, key):
        """Whether columns form one regular grid for fills and contours."""
        if len(columns) < 2:
            return False
        first = None
        for column in columns:
            if len(column) < 2:
                return False
            latitudes = tuple(key(point) for point in column)
            if first is None:
                first = latitudes
            elif latitudes != first:
                return False
        return True

    def _gridded_sources(self, job):
        return bool(job.charts) and all(
            isinstance(source, Grib) for (source,) in job.charts
        )

    def _load_wind(self, job):
        return self._load_field(job, "wind")

    def _load_pressure(self, job):
        columns = self._load_field(job, "pressure")
        if (
            columns
            and not WindPainter._pressure_blocks(columns)
            and self._gridded_sources(job)
        ):
            # Mixed model grids (e.g. 0.25-degree GFS plus projected HRRR or
            # HRDPS) never share a latitude vector, so the native-node union
            # forms no contourable block. Resample the same blended field
            # onto one regular grid instead of drawing nothing.
            resampled = self._resample_field(job, "pressure")
            if resampled:
                return resampled
        return columns

    def _resample_field(self, job, field):
        """Sample a blended field on a regular grid covering the viewport."""
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
            if not sources or not all(
                isinstance(source, Grib) for source in sources
            ):
                return []
            sampler = WeatherSampler(sources)
            bounds = self._viewport_bounds(job)
            south, north = sorted((bounds[0][0], bounds[1][0]))
            west, east = bounds[0][1], bounds[1][1]
            if east < west:
                east += 360.0
            span_lat = north - south
            span_lon = east - west
            if not (span_lat > 0 and span_lon > 0):
                return []
            ncols = min(240, max(2, int(round(span_lon / 0.1)) + 1))
            nrows = min(240, max(2, int(round(span_lat / 0.1)) + 1))
            total = ncols * nrows
            if total > 30000:
                scale = (30000.0 / total) ** 0.5
                ncols = max(2, int(ncols * scale))
                nrows = max(2, int(nrows * scale))
            lats = np.linspace(south, north, nrows)
            lons = np.linspace(west, east, ncols)
            grid_lons, grid_lats = np.meshgrid(lons, lats)
            values = np.asarray(
                sampler.sample(
                    field,
                    job.palette[0],
                    grid_lats.ravel(),
                    grid_lons.ravel(),
                    job.cancel.is_set,
                )
            ).reshape(nrows, ncols, -1)
            if job.cancel.is_set() or not np.isfinite(values).any():
                return []
            # Model footprints rarely fill the padded viewport. Shrink to
            # the sampled footprint so coverage margins cannot empty every
            # column; dropping whole columns keeps one shared latitude
            # vector behind for fills and contouring.
            finite = np.isfinite(values).all(axis=-1)
            rows_with_data = np.flatnonzero(finite.any(axis=1))
            cols_with_data = np.flatnonzero(finite.any(axis=0))
            first_row, last_row = rows_with_data[0], rows_with_data[-1]
            first_col, last_col = cols_with_data[0], cols_with_data[-1]
            row_slice = slice(first_row, last_row + 1)
            col_slice = slice(first_col, last_col + 1)
            values = values[row_slice, col_slice]
            finite = finite[row_slice, col_slice]
            lats = lats[row_slice]
            lons = lons[col_slice]
            if field == "wind":
                speed = np.hypot(values[..., 0], values[..., 1])
                direction = (
                    np.degrees(np.arctan2(-values[..., 0], -values[..., 1])) + 360.0
                ) % 360.0
            columns = []
            for index in range(values.shape[1]):
                if not finite[:, index].all():
                    continue
                lon = (lons[index] + 180.0) % 360.0 - 180.0
                if field == "wind":
                    columns.append(
                        [
                            (
                                float(direction[row, index]),
                                float(speed[row, index]),
                                (float(lats[row]), float(lon)),
                            )
                            for row in range(values.shape[0])
                        ]
                    )
                else:
                    columns.append(
                        [
                            (
                                float(lats[row]),
                                float(lon),
                                float(values[row, index, 0]),
                            )
                            for row in range(values.shape[0])
                        ]
                    )
            return columns if len(columns) >= 2 else []
        finally:
            for _, provider in providers:
                if isinstance(provider, Grib) and provider not in self._gribs.values():
                    provider.close()

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
        fill_data = None
        if (
            data
            and not self._columns_share_lats(data, lambda point: point[2][0])
            and self._gridded_sources(job)
        ):
            # The gradient fill builds quads between neighboring columns,
            # so a ragged multi-model union leaves it empty. Fill from a
            # resampled regular grid while barbs keep the dense native
            # points.
            fill_data = self._resample_field(job, "wind") or None
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
            viewport, cairo.Context(surface), data, pressure, fill_data=fill_data
        )
        surface.flush()
        return None if job.cancel.is_set() else surface
