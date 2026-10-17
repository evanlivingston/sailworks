"""Background wind portrayal with private GDAL handles and cached viewports."""

from collections import OrderedDict
from contextlib import contextmanager
from gweatherrouting.core.scheduling import schedule_idle
from functools import lru_cache
import json
import logging
import os
from types import SimpleNamespace

import cairo
import numpy as np
from osgeo import ogr

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.core.utils import COUNTRY_SHAPES, point_in_bbox
from gweatherrouting.rendering.overlayrenderer import OverlayRenderer, covers
from gweatherrouting.rendering.panprefetch import PanPreparationTime
from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.windpainter import (
    flatten_wind_columns,
    barb_density_parameters,
)

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
        self._last_content = {}
        self._pan_preparation = PanPreparationTime()
        self._painter = painter
        self._gribs = OrderedDict()
        self._leased_providers = {}
        self._land_mask = WindLandMask()
        self._reuse_job_sources = True
        self._source_retention_limit = 64
        # Wider survival reduced cancellation counts but delayed coverage in
        # the startup replay. Keep it an explicitly opted-in benchmark trial.
        self._preserve_covered_jobs = False
        # Retention between display passes, not a bound on GDAL/transient decode
        # allocations inside the unchanged shared sampler.
        self._decoded_retention_limit = 128 * 1024 * 1024
        self.cache_metrics = dict(lookups=0, hits=0, opens=0, samplers=0)
        self.cache_state = {}
        # View-independent field working set: previously sampled geographic
        # bounds are reused when they contain a new request (exact data, no
        # overfetch); resampled grids reuse on exact matches only.
        self._field_cache = OrderedDict()
        self._field_cache_bytes = 0
        self._field_cache_entry_limit = 64
        self._field_cache_byte_limit = 32 * 1024 * 1024
        self._field_cache_enabled = True
        self.field_cache_metrics = dict(hits=0, misses=0, evictions=0)

    def _release_if_idle(self):
        if not self._states and not self._running:
            for grib in self._gribs.values():
                grib.close()
            self._gribs.clear()
            self.cache_state = self._cache_snapshot()

    def forget(self, canvas):
        self._last_content.pop(canvas, None)
        super().forget(canvas)
        if not self._states:
            self._pan_preparation = PanPreparationTime()
        self._release_if_idle()

    def _completed(self, canvas, job, surface, error):
        if (
            surface is not None
            and not error
            and not job.cancel.is_set()
            and job.started_at is not None
            and job.finished_at is not None
        ):
            self._pan_preparation.observe(
                self._pan_preparation_key(job.charts, job.factor, job.viewport._zoom),
                job.finished_at - job.started_at,
            )
        result = super()._completed(canvas, job, surface, error)
        self._release_if_idle()
        return result

    @staticmethod
    def _pan_preparation_key(charts, factor, zoom):
        return (tuple(source for (source,) in charts), factor, zoom)

    def pan_lookahead(self, canvas):
        content = self._last_content.get(canvas)
        if content is None:
            return 1.0
        charts, _palette, factor = content
        return self._pan_preparation.lookahead(
            self._pan_preparation_key(charts, factor, canvas._zoom)
        )

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

    def _keep_active(self, job, canvas, key):
        if self._preserve_covered_jobs:
            return (
                not job.cancel.is_set()
                and covers(job.viewport, canvas)
                and self._can_complete(job, key)
            )
        return super()._keep_active(job, canvas, key)

    def _fits_job(self, job, canvas):
        if (
            getattr(job.viewport, "pan_prediction", False)
            and getattr(canvas, "is_moving", lambda: False)()
        ):
            return covers(job.viewport, canvas)
        return super()._fits_job(job, canvas)

    def prefetch_at(self, canvas, latitude, longitude, predictive=False, zoom=None):
        """Queue one speculative frame at an explicit center.

        The caller owns pixel geometry: pass the same center a pan would
        produce so arrival matches exactly regardless of padding conventions.
        """
        content = self._last_content.get(canvas)
        if content is None:
            return False
        charts, palette, factor = content
        from gweatherrouting.core.mapviewport import MapViewport
        from gweatherrouting.rendering.overlayrenderer import MARGIN

        # Capture through the same padded-build path as visible draws: the
        # transient render padding changes viewport dimensions, and promotion
        # requires an exact size match on arrival.
        holder = {}

        def capture():
            viewport = MapViewport(canvas, MARGIN, lambda: None)
            holder["viewport"] = viewport
            return None

        canvas._build_padded_layer(capture)
        viewport = holder["viewport"]
        viewport._center_lat = max(-85, min(85, latitude))
        viewport._center_lon = longitude
        viewport.pan_prediction = predictive
        if zoom is not None:
            viewport._zoom = zoom
            viewport.zoom_prediction = True
        key = (palette, factor, tuple(c[0] for c in charts))
        return self.prefetch(canvas, key, viewport, charts, factor, palette)

    def prepared_view_status(self, canvas):
        """Geometric preparation status, NOT proof of available forecast data."""
        state = self._states.get(canvas)
        if state is None or state.frame is None:
            return dict(preview=False, current=False)
        job, _surface = state.frame
        return dict(
            preview=self._can_preview(job, state.key),
            current=job.key == state.key and covers(job.viewport, canvas),
        )

    def _worker_grib(self, source):
        self.cache_metrics["lookups"] += 1
        grib = self._gribs.get(source)
        if grib is None:
            grib = DisplayGrib(source)
            grib._display_version = self._source_version(source)
            self.cache_metrics["opens"] += 1
        else:
            self.cache_metrics["hits"] += 1
        self._retain_grib(source, grib)
        return grib

    def _retain_grib(self, source, grib):
        self._gribs[source] = grib
        self._gribs.move_to_end(source)
        while len(self._gribs) > self._source_retention_limit:
            _, evicted = self._gribs.popitem(last=False)
            if id(evicted) not in self._leased_providers:
                evicted.close()

    @staticmethod
    def _source_version(source):
        try:
            stat = os.stat(source.path)
            file_version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            # GDAL virtual paths need not be native filesystem paths. Their
            # selected source instance/metadata still participates in identity.
            file_version = None
        return (
            source.path,
            source.name,
            source.centre,
            source.start_time,
            source.last_forecast,
            repr(source.bounds),
            file_version,
        )

    def _prune_sources(self, job):
        selected = {id(source) for (source,) in job.charts}
        for source, provider in list(self._gribs.items()):
            if id(provider) in self._leased_providers:
                continue
            if id(source) not in selected or getattr(
                provider, "_display_version", None
            ) != self._source_version(source):
                self._gribs.pop(source)
                provider.close()

    def _cache_snapshot(self):
        decoded = {}
        coordinates = {}
        for provider in self._gribs.values():
            index = provider.__dict__.get("_sampling")
            if index is not None:
                decoded.update(
                    {id(array): array.nbytes for array in index.arrays.values()}
                )
            legacy = provider.__dict__.get("_arrays")
            if legacy:
                for arrays in legacy.values():
                    for array in arrays:
                        decoded[id(array)] = array.nbytes
            legacy_pressure = provider.__dict__.get("_pressure_arrays")
            if legacy_pressure:
                for array in legacy_pressure.values():
                    decoded[id(array)] = array.nbytes
            for name in ("_lats", "_lons"):
                array = provider.__dict__.get(name)
                if array is not None:
                    coordinates[id(array)] = array.nbytes
        return dict(
            retained_sources=len(self._gribs),
            source_limit=self._source_retention_limit,
            decoded_bytes=sum(decoded.values()),
            decoded_limit=self._decoded_retention_limit,
            coordinate_bytes=sum(coordinates.values()),
        )

    def _release_providers(self, resources, retained):
        for _, provider in resources.get("opened", {}).values():
            self._leased_providers.pop(id(provider), None)
            if isinstance(provider, Grib) and provider not in retained:
                provider.close()

    @contextmanager
    def _source_scope(self, job):
        resources = getattr(job, "_weather_resources", None)
        owns_scope = resources is None
        if owns_scope:
            resources = {}
        try:
            if "opened" not in resources:
                self._prune_sources(job)
                # Pin cross-job cache entries before opening anything:
                # otherwise a first new source evicts an index cycle before
                # this pass reaches it, leaving cleanup to the GC.
                resources["opened"] = {
                    id(source): (source, provider)
                    for source, provider in self._gribs.items()
                }
            opened = resources["opened"]
            self._leased_providers.update(
                {
                    id(provider): provider
                    for _, provider in opened.values()
                    if isinstance(provider, Grib)
                }
            )
            providers = []
            for (source,) in job.charts:
                if job.cancel.is_set():
                    break
                pair = opened.get(id(source))
                if pair is None:
                    try:
                        pair = source, self._worker_grib(source)
                        opened[id(source)] = pair
                        if isinstance(pair[1], Grib):
                            self._leased_providers[id(pair[1])] = pair[1]
                    except Exception as exc:
                        # Retain successful opens, but retry unavailable sources
                        # on the next pass just as the uncached path does.
                        logger.debug("Weather source unavailable: %s", exc)
                        continue
                else:
                    metrics = getattr(job, "weather_metrics", None)
                    if metrics is not None:
                        metrics["provider_reuses"] = (
                            metrics.get("provider_reuses", 0) + 1
                        )
                    if isinstance(pair[1], Grib):
                        self._retain_grib(source, pair[1])
                providers.append(pair)
            if providers != resources.get("providers"):
                resources.pop("sampler", None)
            resources["providers"] = providers
            yield resources
        finally:
            if owns_scope:
                self._release_providers(resources, self._gribs.values())

    def _sampler_for(self, resources):
        sampler = resources.get("sampler")
        if sampler is not None and len(sampler.indices) != len(resources["providers"]):
            # GridIndex construction can fail independently of opening GDAL.
            # Preserve the old next-pass retry instead of caching an omission.
            del resources["sampler"]
        if "sampler" not in resources:
            sources = [provider for _, provider in resources["providers"]]
            resources["sampler"] = (
                WeatherSampler(sources)
                if sources and all(isinstance(source, Grib) for source in sources)
                else None
            )
            if resources["sampler"] is not None:
                self.cache_metrics["samplers"] += 1
        return resources["sampler"]

    def _trim_decoded(self, resources, job):
        # GridIndex arrays are a cache only. Dropping an entry never changes
        # record choice, masks, interpolation, or provider order. Do not open an
        # index just to account it, and do not touch GDAL's own driver cache.
        entries = []
        for _, provider in resources.get("opened", {}).values():
            index = getattr(provider, "__dict__", {}).get("_sampling")
            if index is not None:
                for array in index.arrays.values():
                    array.setflags(write=False)
                entries.extend(
                    (index.arrays, band, array.nbytes)
                    for band, array in index.arrays.items()
                )
        total = sum(size for _, _, size in entries)
        metrics = getattr(job, "weather_metrics", None)
        if metrics is not None:
            metrics["decoded_pass_peak_bytes"] = max(
                metrics.get("decoded_pass_peak_bytes", 0), total
            )
        for arrays, band, size in entries:
            if total <= self._decoded_retention_limit:
                break
            del arrays[band]
            total -= size
            if metrics is not None:
                metrics["decoded_evictions"] = metrics.get("decoded_evictions", 0) + 1
        if metrics is not None:
            metrics["decoded_retained_bytes"] = total

    @staticmethod
    def _viewport_bounds(job):
        p1, p2 = (p.get_degrees() for p in job.viewport.get_bbox())
        return (
            (min(p1[0], p2[0]) - 1, min(p1[1], p2[1]) - 1),
            (max(p1[0], p2[0]) + 1, max(p1[1], p2[1]) + 1),
        )

    def _field_key(self, job, kind, field, bounds):
        instant = job.palette[0]
        stamp = instant.timestamp() if hasattr(instant, "timestamp") else float(instant)
        versions = tuple(self._source_version(source) for (source,) in job.charts)
        # Keep exact float bounds: rounding would shrink stored extents below
        # the sampled area and break containment checks by ~1e-7 degrees.
        exact = (
            (float(bounds[0][0]), float(bounds[0][1])),
            (float(bounds[1][0]), float(bounds[1][1])),
        )
        return (kind, field, round(float(stamp), 3), versions, exact)

    @staticmethod
    def _bounds_contain(outer, inner):
        (osouth, owest), (onorth, oeast) = outer
        (isouth, iwest), (inorth, ieast) = inner
        return (
            osouth <= isouth + 1e-9
            and onorth >= inorth - 1e-9
            and owest <= iwest + 1e-9
            and oeast >= ieast - 1e-9
        )

    @staticmethod
    def _filter_columns(columns, field, bounds):
        (south, west), (north, east) = bounds
        south, north = sorted((south, north))
        kept = []
        for column in columns:
            matches = []
            for point in column:
                lat, lon = point[2] if field == "wind" else point[:2]
                wrapped = lon
                if wrapped < west - 1e-7 and wrapped + 360 <= east + 1e-7:
                    wrapped += 360
                if (
                    south - 1e-7 <= lat <= north + 1e-7
                    and west - 1e-7 <= wrapped <= east + 1e-7
                ):
                    matches.append(point)
            if matches:
                kept.append(matches)
        return kept

    def _contained_columns(self, job, kind, field, bounds):
        prefix = self._field_key(job, kind, field, bounds)[:4]
        for key in reversed(list(self._field_cache)):
            if key[:4] != prefix:
                continue
            columns = self._field_cache.get(key)
            if columns is None:
                continue
            if kind != "native":
                # Resampled grids depend on their exact bounds (regular
                # linspace); containment would serve the wrong grid.
                if key[4] != bounds:
                    continue
                self._field_cache.move_to_end(key)
                self.field_cache_metrics["hits"] += 1
                return columns
            if not self._bounds_contain(key[4], bounds):
                continue
            self._field_cache.move_to_end(key)
            self.field_cache_metrics["hits"] += 1
            return self._filter_columns(columns, field, bounds)
        return None

    @staticmethod
    def _columns_bytes(columns):
        points = sum(len(column) for column in columns)
        return points * 96 + len(columns) * 64

    def _store_columns(self, key, columns):
        size = self._columns_bytes(columns)
        if size > self._field_cache_byte_limit:
            return
        previous = self._field_cache.pop(key, None)
        if previous is not None:
            self._field_cache_bytes -= self._columns_bytes(previous)
        self._field_cache[key] = columns
        self._field_cache_bytes += size
        while (
            len(self._field_cache) > self._field_cache_entry_limit
            or self._field_cache_bytes > self._field_cache_byte_limit
        ):
            _, evicted = self._field_cache.popitem(last=False)
            self._field_cache_bytes -= self._columns_bytes(evicted)
            self.field_cache_metrics["evictions"] += 1

    @staticmethod
    def _rect_overlap(rect, cover):
        (rs, rw), (rn, re) = rect
        (cs, cw), (cn, ce) = cover
        return rw < ce and re > cw and rs < cn and rn > cs

    @classmethod
    def _subtract_rect(cls, rect, cover):
        (rs, rw), (rn, re) = rect
        (cs, cw), (cn, ce) = cover
        west = max(rw, cw)
        east = min(re, ce)
        south = max(rs, cs)
        north = min(rn, cn)
        if west >= east or south >= north:
            return [rect]
        pieces = []
        if rw < west:
            pieces.append(((rs, rw), (rn, west)))
        if east < re:
            pieces.append(((rs, east), (rn, re)))
        if rs < south:
            pieces.append(((rs, west), (south, east)))
        if north < rn:
            pieces.append(((north, west), (rn, east)))
        return pieces

    def _uncovered_rects(self, bounds, covers):
        remaining = [bounds]
        for cover in covers:
            stripped = []
            for rect in remaining:
                if self._rect_overlap(rect, cover):
                    stripped.extend(self._subtract_rect(rect, cover))
                else:
                    stripped.append(rect)
            remaining = stripped
            if not remaining:
                break
        return [
            rect
            for rect in remaining
            if rect[1][0] > rect[0][0] and rect[1][1] > rect[0][1]
        ]

    def _load_field(self, job, field):
        with self._source_scope(job) as resources:
            if job.cancel.is_set():
                return []
            sampler = self._sampler_for(resources)
            if sampler is None:
                data = self._provider_field(job, field, resources["providers"])
                self._trim_decoded(resources, job)
                return self._columns(data, field)
            if not self._field_cache_enabled:
                data = sampler.grid(
                    field, job.palette[0], self._viewport_bounds(job), job.cancel.is_set
                )
                self._trim_decoded(resources, job)
                return self._columns(data, field)
            bounds = self._viewport_bounds(job)
            cached = self._contained_columns(job, "native", field, bounds)
            if cached is not None:
                metrics = getattr(job, "weather_metrics", None)
                if metrics is not None:
                    metrics["field_cache_hits"] = metrics.get("field_cache_hits", 0) + 1
                return cached
            self.field_cache_metrics["misses"] += 1
            prefix = self._field_key(job, "native", field, bounds)[:4]
            overlaps = [
                (key[4], columns)
                for key, columns in self._field_cache.items()
                if key[:4] == prefix and self._rect_overlap(key[4], bounds)
            ]
            data = []
            (south, west), (north, east) = bounds
            for _cover, columns in overlaps:
                for column in columns:
                    for point in column:
                        lat, lon = point[2] if field == "wind" else point[:2]
                        if (
                            south - 1e-7 <= lat <= north + 1e-7
                            and west - 1e-7 <= lon <= east + 1e-7
                        ):
                            data.append(point)
            strips = self._uncovered_rects(bounds, [cover for cover, _ in overlaps])
            for rect in strips:
                if job.cancel.is_set():
                    return []
                chunk = sampler.grid(field, job.palette[0], rect, job.cancel.is_set)
                if chunk:
                    data.extend(chunk)
            self._trim_decoded(resources, job)
            columns = self._columns(data, field)
            metrics = getattr(job, "weather_metrics", None)
            if metrics is not None:
                metrics["field_cache_misses"] = metrics.get("field_cache_misses", 0) + 1
                metrics["field_strips_sampled"] = metrics.get(
                    "field_strips_sampled", 0
                ) + len(strips)
                metrics["field_strips_reused"] = metrics.get(
                    "field_strips_reused", 0
                ) + len(overlaps)
            if not job.cancel.is_set():
                key = self._field_key(job, "native", field, bounds)
                self._store_columns(key, columns)
            return columns

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
        with self._source_scope(job) as resources:
            if job.cancel.is_set():
                return []
            sampler = self._sampler_for(resources)
            if sampler is None:
                return []
            bounds = self._viewport_bounds(job)
            cached = (
                self._contained_columns(job, "resampled", field, bounds)
                if self._field_cache_enabled
                else None
            )
            if cached is not None:
                metrics = getattr(job, "weather_metrics", None)
                if metrics is not None:
                    metrics["field_cache_hits"] = metrics.get("field_cache_hits", 0) + 1
                return cached
            if self._field_cache_enabled:
                self.field_cache_metrics["misses"] += 1
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
            self._trim_decoded(resources, job)
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
            if (
                len(columns) >= 2
                and not job.cancel.is_set()
                and self._field_cache_enabled
            ):
                self._store_columns(
                    self._field_key(job, "resampled", field, bounds), columns
                )
                metrics = getattr(job, "weather_metrics", None)
                if metrics is not None:
                    metrics["field_cache_misses"] = (
                        metrics.get("field_cache_misses", 0) + 1
                    )
            return columns if len(columns) >= 2 else []

    @staticmethod
    def _show_isobars(job):
        # Palettes built before the isobar toggle carry no fourth element.
        return job.palette[3] if len(job.palette) > 3 else True

    def _render(self, job):
        before = dict(self.cache_metrics)
        before_fields = dict(self.field_cache_metrics)
        job.weather_metrics = {}
        resources = {}
        if self._reuse_job_sources:
            job._weather_resources = resources
        try:
            return self._render_fields(job)
        finally:
            if self._reuse_job_sources:
                del job._weather_resources
                try:
                    if "providers" in resources:
                        self._trim_decoded(resources, job)
                finally:
                    self._release_providers(resources, self._gribs.values())
            job.weather_metrics.update(
                {key: value - before[key] for key, value in self.cache_metrics.items()}
            )
            job.weather_metrics["field_cache"] = {
                key: value - before_fields[key]
                for key, value in self.field_cache_metrics.items()
            }
            job.weather_metrics["field_cache_entries"] = len(self._field_cache)
            job.weather_metrics["field_cache_bytes"] = self._field_cache_bytes
            self.cache_state = self._cache_snapshot()
            job.weather_metrics["retained_cache"] = dict(self.cache_state)

    def _render_fields(self, job):
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
        # Keep land-mask work on the worker, alongside weather sampling.
        # Only completed jobs publish these vectors to the foreground.
        barb_data = data
        if data and not job.palette[2]:
            barb_data = []
            for column in data:
                if job.cancel.is_set():
                    return None
                barb_data.append(
                    [
                        point
                        for point in column
                        if not self._land_mask.contains(*point[2])
                    ]
                )
        job.barb_flat = flatten_wind_columns(barb_data)
        viewport = job.viewport
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,
            viewport.width * job.factor,
            viewport.height * job.factor,
        )
        surface.set_device_scale(job.factor, job.factor)
        settings = self._settings_for(job.palette)
        # Barbs stay out of the cached raster so zoom previews only stretch
        # the smooth fill; the foreground repaints barbs live at fixed size.
        self._painter(settings, self._land_mask.contains).draw(
            viewport,
            cairo.Context(surface),
            data,
            pressure,
            fill_data=fill_data,
            draw_barbs=False,
        )
        surface.flush()
        return None if job.cancel.is_set() else surface

    def draw(
        self, canvas, cr, charts, palette, live_barbs=True, barb_density="standard"
    ):
        """Paint cached fill, then crisp live barbs for the current view."""
        if charts:
            self._last_content[canvas] = (
                charts,
                palette,
                canvas.get_scale_factor(),
            )
        super().draw(canvas, cr, charts, palette)
        if live_barbs:
            self.draw_live_barbs(canvas, cr, barb_density)

    def draw_live_barbs(self, canvas, cr, density="standard"):
        """Reproject cached wind vectors at fixed screen size (no scaling)."""
        geometry, opacity = self.live_geometry_for_view(
            canvas,
            canvas.get_allocated_width(),
            canvas.get_allocated_height(),
            canvas._center_lat,
            canvas._center_lon,
            canvas._zoom,
            density=density,
        )
        WindPainter.paint_barb_geometry(cr, geometry, opacity)

    def live_geometry_for_view(
        self, canvas, width, height, center_lat, center_lon, zoom, density="standard"
    ):
        """Foreground geometry for toolkits that cache the base image (Qt).

        Returns ``(geometry, opacity)`` in unpadded widget pixels so the
        caller can paint crisp vectors after the scaled base image.
        """
        state = self._states.get(canvas)
        if state is None or state.frame is None:
            return {}, 0.0
        job, _surface = state.frame
        if not self._can_preview(job, state.key):
            return {}, 0.0
        flat = getattr(job, "barb_flat", None)
        if flat is None:
            return {}, 0.0
        step, max_count = barb_density_parameters(density)
        view = (center_lat, center_lon, zoom, width, height, step, max_count)
        # Repaints of a stationary view reuse geometry; gestures only update
        # positions/selection, never reload weather or scale symbol strokes.
        if getattr(job, "barb_geometry_view", None) != view:
            job.barb_geometry, _count = WindPainter.live_geometry_for_view(
                flat,
                center_lat,
                center_lon,
                zoom,
                width,
                height,
                step=step,
                max_count=max_count,
            )
            job.barb_geometry_view = view
        geometry = job.barb_geometry
        return geometry, float(job.palette[1])

    @staticmethod
    def _settings_for(palette):
        return SimpleNamespace(
            gribArrowOpacity=palette[1],
            gribArrowOnGround=palette[2],
            gribIsobarsVisible=palette[3] if len(palette) > 3 else True,
            gribIsobarInterval=palette[5] if len(palette) > 5 else 4.0,
            gribIsobarOpacity=palette[6] if len(palette) > 6 else 0.85,
            gribIsobarLabels=palette[7] if len(palette) > 7 else True,
        )
