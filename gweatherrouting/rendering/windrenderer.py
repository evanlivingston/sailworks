"""Background wind portrayal with private GDAL handles and cached viewports."""

from collections import OrderedDict
from contextlib import contextmanager
from gweatherrouting.core.scheduling import schedule_idle
from functools import lru_cache
import json
import logging
import math
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cairo
import numpy as np
from osgeo import ogr

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.core.storage import DATA_DIR
from gweatherrouting.core.utils import COUNTRY_SHAPES, point_in_bbox
from gweatherrouting.rendering.overlayrenderer import OverlayRenderer, covers
from gweatherrouting.rendering.panprefetch import PanPreparationTime
from gweatherrouting.rendering.windpainter import WindPainter
from gweatherrouting.rendering.streamlines import trace_streamlines, geometry_for_view
from gweatherrouting.rendering.windpainter import (
    flatten_wind_columns,
    barb_density_parameters,
    mercator_project_nodes,
)

logger = logging.getLogger("gweatherrouting")


class WindLandMask:
    """Worker-only, cached containment using the installed chart shoreline."""

    def __init__(self, directory=None):
        self.directory = Path(directory or DATA_DIR) / "gshhs" / "GSHHS_shp"
        self._path = None
        self._dataset = None
        self._prepared = OrderedDict()
        self._tiles = OrderedDict()
        self.contains = lru_cache(maxsize=16384)(self._contains)

    def refresh_source(self, scale=0):
        """Check once per background job, including after coastline installation."""
        # Match the base chart's scale tiers instead of reading full-resolution
        # global polygons for a zoomed-out view. Prefer finer data if this tier
        # is absent, then fall back to any installed lower-resolution tier.
        if scale > 5000:
            quality = "c"
        elif scale > 500:
            quality = "l"
        elif scale > 200:
            quality = "i"
        elif scale >= 15:
            quality = "h"
        else:
            quality = "f"
        qualities = (quality,) + tuple(
            q for q in ("f", "h", "i", "l", "c") if q != quality
        )
        path = next(
            (
                self.directory / quality / f"GSHHS_{quality}_L1.shp"
                for quality in qualities
                if (self.directory / quality / f"GSHHS_{quality}_L1.shp").exists()
            ),
            None,
        )
        if path != self._path:
            self._path = path
            self._dataset = None
            self._prepared.clear()
            self._tiles.clear()
            self.contains.cache_clear()

    def _shoreline_contains(self, lat, lon):
        if self._dataset is None:
            self._dataset = ogr.Open(str(self._path))
        if self._dataset is None:
            raise RuntimeError(f"Unable to open shoreline: {self._path}")
        layer = self._dataset.GetLayer(0)
        tile = (math.floor(lon), math.floor(lat))
        if tile not in self._tiles:
            x, y = tile
            layer.SetSpatialFilterRect(x, y, x + 1, y + 1)
            layer.ResetReading()
            self._tiles[tile] = tuple(
                (feature.GetFID(), feature.GetGeometryRef().GetEnvelope())
                for feature in layer
            )
            if len(self._tiles) > 32:
                self._tiles.popitem(last=False)
        self._tiles.move_to_end(tile)
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(lon, lat)
        for fid, (west, east, south, north) in self._tiles[tile]:
            # A coastal bucket may contain hundreds of tiny islands. Reject
            # their envelopes before fetching/preparing geometry; otherwise
            # water samples churn the bounded polygon cache on every point.
            if not (west <= lon <= east and south <= lat <= north):
                continue
            if fid not in self._prepared:
                feature = layer.GetFeature(fid)
                geometry = feature.GetGeometryRef().Clone()
                self._prepared[fid] = (geometry, geometry.CreatePreparedGeometry())
                if len(self._prepared) > 64:
                    self._prepared.popitem(last=False)
            self._prepared.move_to_end(fid)
            geometry, prepared = self._prepared[fid]
            if (prepared or geometry).Contains(point):
                return True
        return False

    def _contains(self, lat, lon):
        lon = (lon + 180) % 360 - 180
        if self._path is not None:
            return self._shoreline_contains(lat, lon)
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
        # One retained wind frame per canvas while a pressure-only frame is
        # visible. Visibility is not a request to discard decoded forecasts.
        self._hidden_wind_frames = {}
        self._barbs_over_land = {}
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
        # Persistent weather sampler shared across jobs so its grid-result cache
        # survives pan steps. Invalidated when the provider set changes.
        self._persistent_sampler = None
        self._persistent_sampler_provider_ids: tuple[int, ...] = ()
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
            self._release_resources()

    def _release_resources(self):
        for grib in self._gribs.values():
            grib.close()
        self._gribs.clear()
        self._field_cache.clear()
        self._field_cache_bytes = 0
        self.cache_state = self._cache_snapshot()

    def forget(self, canvas):
        self._last_content.pop(canvas, None)
        self._hidden_wind_frames.pop(canvas, None)
        self._barbs_over_land.pop(canvas, None)
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
        # Switching symbol style can keep the previous symbols visible while
        # the worker prepares streamlines; all raster-affecting style stays strict.
        return job.key[1:] == key[1:] and job.palette[1:8] == key[0][1:8]

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
        # The prediction markers live on the viewport at runtime; the type
        # checker cannot see the dynamic attributes set by the map canvas.
        setattr(viewport, "pan_prediction", predictive)
        if zoom is not None:
            viewport._zoom = zoom
            setattr(viewport, "zoom_prediction", True)
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
        if resources is None:
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
            providers = self._scope_providers(job, opened)
            if providers != resources.get("providers"):
                resources.pop("sampler", None)
            resources["providers"] = providers
            yield resources
        finally:
            if owns_scope:
                self._release_providers(resources, self._gribs.values())

    def _scope_providers(self, job, opened):
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
                    metrics["provider_reuses"] = metrics.get("provider_reuses", 0) + 1
                if isinstance(pair[1], Grib):
                    self._retain_grib(source, pair[1])
            providers.append(pair)
        return providers

    def _sampler_for(self, resources):
        provider_ids = tuple(id(p) for _, p in resources["providers"])
        sampler = self._persistent_sampler
        if (
            sampler is not None
            and self._persistent_sampler_provider_ids == provider_ids
            and len(sampler.indices) == len(resources["providers"])
        ):
            resources["sampler"] = sampler
            return sampler
        if (
            sampler is not None
            and self._persistent_sampler_provider_ids == provider_ids
        ):
            # GridIndex construction can fail independently of opening GDAL.
            # Do not cache the omission: build a fresh sampler so the missing
            # index is attempted again on this pass.
            logger.info("Rebuilding the weather sampler after a failed index")
        sources = [provider for _, provider in resources["providers"]]
        sampler = (
            WeatherSampler(sources)
            if sources and all(isinstance(source, Grib) for source in sources)
            else None
        )
        if sampler is not None:
            self.cache_metrics["samplers"] += 1
        self._persistent_sampler = sampler
        self._persistent_sampler_provider_ids = provider_ids
        resources["sampler"] = sampler
        return sampler

    def _trim_decoded(self, resources, job):
        # GridIndex arrays are a cache only. Dropping an entry never changes
        # record choice, masks, interpolation, or provider order. Do not open an
        # index just to account it, and do not touch GDAL's own driver cache.
        entries: list[tuple[Any, int, int]] = []
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
        """Filter array-columns to the given bounds."""
        (south, west), (north, east) = bounds
        south, north = sorted((south, north))
        kept: list[tuple[Any, ...]] = []
        for column in columns:
            lon = float(column[0])
            col_lats = column[1]
            # A view may cross the antimeridian; the wrapped longitude is the
            # one that falls inside the requested extent.
            wrapped = lon
            if wrapped < west - 1e-7 and wrapped + 360 <= east + 1e-7:
                wrapped += 360
            if not west - 1e-7 <= wrapped <= east + 1e-7:
                continue
            mask = (col_lats >= south - 1e-7) & (col_lats <= north + 1e-7)
            if not mask.any():
                continue
            if field == "wind":
                kept.append((lon, col_lats[mask], column[2][mask], column[3][mask]))
            else:
                kept.append((lon, col_lats[mask], column[2][mask]))
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
            # A wider cached extent still answers the request, but only the
            # requested points are returned, so nothing leaks in from outside.
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
        # Four bands around the cover. They tile the remainder exactly: the
        # two full-width bands and the two side bands meet at the corners
        # without leaving a gap or overlapping.
        pieces = []
        if rs < south:
            pieces.append(((rs, rw), (south, re)))
        if north < rn:
            pieces.append(((north, rw), (rn, re)))
        if rw < west:
            pieces.append(((south, rw), (north, west)))
        if east < re:
            pieces.append(((south, east), (north, re)))
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
            bounds = self._viewport_bounds(job)
            if not self._field_cache_enabled:
                data = sampler.grid(field, job.palette[0], bounds, job.cancel.is_set)
                self._trim_decoded(resources, job)
                return self._columns(data, field) if data is not None else []
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
            parts, strips = self._sample_strips(job, sampler, field, bounds, overlaps)
            if parts is None:
                return []
            merged = self._merge_field(parts, field)
            self._trim_decoded(resources, job)
            columns = self._columns(merged, field) if merged is not None else []
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

    def _sample_strips(self, job, sampler, field, bounds, overlaps):
        """Reuse the cached parts and sample only the strips the request adds.

        Returns the flat field arrays and the number of sampled strips, or
        None when the job was cancelled.
        """
        reused = []
        for _cover, columns in overlaps:
            reused.extend(self._filter_columns(columns, field, bounds))
        parts = []
        if reused:
            parts.append(self._column_arrays(reused))
        strips = self._uncovered_rects(bounds, [cover for cover, _ in overlaps])
        for rect in strips:
            if job.cancel.is_set():
                return None, strips
            chunk = sampler.grid(field, job.palette[0], rect, job.cancel.is_set)
            if chunk is not None:
                parts.append(self._grid_arrays(chunk, field))
        return parts, strips

    @staticmethod
    def _column_arrays(columns):
        """Flatten columns into parallel arrays with the values per slot."""
        lats: list[Any] = []
        lons: list[Any] = []
        slots: list[list[Any]] = []
        for column in columns:
            lon = float(column[0])
            col_lats = np.asarray(column[1], dtype=np.float64)
            lats.append(col_lats)
            lons.append(np.full(col_lats.shape, lon, dtype=np.float64))
            for index, part in enumerate(column[2:]):
                while len(slots) <= index:
                    slots.append([])
                slots[index].append(np.asarray(part, dtype=np.float64))
        if not lats:
            return None
        return (
            np.concatenate(lats),
            np.concatenate(lons),
            [np.concatenate(group) for group in slots],
        )

    @staticmethod
    def _grid_arrays(chunk, field):
        """A sampler grid result as parallel arrays with the values per slot."""
        slot_values: tuple
        if field == "wind":
            dirs, speeds, lats, lons = chunk
            slot_values = (dirs, speeds)
        else:
            lats, lons, values = chunk
            slot_values = (values,)
        return (
            np.asarray(lats, dtype=np.float64),
            np.asarray(lons, dtype=np.float64),
            slot_values,
        )

    @classmethod
    def _merge_field(cls, parts, field):
        """Combine flat field arrays, keeping one sample per coordinate."""
        lats: list[Any] = []
        lons: list[Any] = []
        slots: list[list[Any]] = []
        for part in parts:
            if part is None:
                continue
            part_lats, part_lons, part_slots = part
            if not len(part_lats):
                continue
            lats.append(part_lats)
            lons.append(part_lons)
            while len(slots) < len(part_slots):
                slots.append([])
            for index, values in enumerate(part_slots):
                slots[index].append(values)
        if not lats:
            return None
        merged_lats = np.concatenate(lats)
        merged_lons = np.concatenate(lons)
        merged_slots = [np.concatenate(group) for group in slots]
        # A shared edge is sampled twice, once per side; keep one sample.
        order = np.lexsort((merged_lats, merged_lons))
        merged_lats = merged_lats[order]
        merged_lons = merged_lons[order]
        merged_slots = [values[order] for values in merged_slots]
        keep = np.ones(len(merged_lats), dtype=bool)
        if len(merged_lats) > 1:
            keep[1:] = (merged_lats[1:] != merged_lats[:-1]) | (
                merged_lons[1:] != merged_lons[:-1]
            )
        merged_lats = merged_lats[keep]
        merged_lons = merged_lons[keep]
        merged_slots = [values[keep] for values in merged_slots]
        if field == "wind":
            return (merged_slots[0], merged_slots[1], merged_lats, merged_lons)
        return (merged_lats, merged_lons, merged_slots[0])

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
    def _wind_arrays(data):
        """Tuple-format wind rows as parallel float arrays."""
        # Deduplicate by coordinate (keep first), filter nonfinite,
        # like the old dict-based code.
        seen = set()
        dirs_list, speeds_list, lats_list, lons_list = [], [], [], []
        for p in data:
            if not (math.isfinite(p[0]) and math.isfinite(p[1])):
                continue
            coord = (p[2][0], p[2][1])
            if coord in seen:
                continue
            seen.add(coord)
            dirs_list.append(p[0])
            speeds_list.append(p[1])
            lats_list.append(p[2][0])
            lons_list.append(p[2][1])
        return (
            np.array(dirs_list, dtype=np.float64),
            np.array(speeds_list, dtype=np.float64),
            np.array(lats_list, dtype=np.float64),
            np.array(lons_list, dtype=np.float64),
        )

    @staticmethod
    def _pressure_arrays(data):
        """Tuple-format pressure rows as parallel float arrays."""
        seen = set()
        lats_list, lons_list, values_list = [], [], []
        for p in data:
            if not all(map(math.isfinite, p)):
                continue
            coord = (p[0], p[1])
            if coord in seen:
                continue
            seen.add(coord)
            lats_list.append(p[0])
            lons_list.append(p[1])
            values_list.append(p[2])
        return (
            np.array(lats_list, dtype=np.float64),
            np.array(lons_list, dtype=np.float64),
            np.array(values_list, dtype=np.float64),
        )

    @staticmethod
    def _column_spans(lons_s):
        """Unique longitudes with the sample slice bounds of each one."""
        unique_lons, start_indices = np.unique(lons_s, return_index=True)
        n = len(lons_s)
        return [
            (
                float(unique_lons[i]),
                int(start_indices[i]),
                int(start_indices[i + 1]) if i + 1 < len(unique_lons) else n,
            )
            for i in range(len(unique_lons))
        ]

    @staticmethod
    def _wind_columns(lons_s, lats_s, dirs_s, speeds_s):
        """Group sorted wind samples into one column per longitude."""
        return [
            (lon, lats_s[start:end], dirs_s[start:end], speeds_s[start:end])
            for lon, start, end in WindOverlayRenderer._column_spans(lons_s)
        ]

    @staticmethod
    def _pressure_columns(lons_s, lats_s, values_s):
        """Group sorted pressure samples into one column per longitude."""
        return [
            (lon, lats_s[start:end], values_s[start:end])
            for lon, start, end in WindOverlayRenderer._column_spans(lons_s)
        ]

    @staticmethod
    def _columns(data, field):
        # Group data into columns by longitude, sorted by latitude.
        # Accepts either:
        #   * Array format from grid(): (dirs, speeds, lats, lons) for wind,
        #     (lats, lons, values) for pressure
        #   * Tuple format from _provider_field(): list of tuples
        # Returns list of columns. Each column is:
        #   Wind:   (lon, lats_array, dirs_array, speeds_array)
        #   Pressure: (lon, lats_array, values_array)
        if field == "wind":
            if WindOverlayRenderer._is_array_format(data, 4):
                dirs, speeds, lats, lons = data
            else:
                dirs, speeds, lats, lons = WindOverlayRenderer._wind_arrays(data)
        else:
            if WindOverlayRenderer._is_array_format(data, 3):
                lats, lons, values = data
            else:
                lats, lons, values = WindOverlayRenderer._pressure_arrays(data)

        if len(lons) == 0:
            return []

        # Sort by lon, then lat within each lon
        order = np.lexsort((lats, lons))
        lons_s = lons[order]
        lats_s = lats[order]
        if field == "wind":
            return WindOverlayRenderer._wind_columns(
                lons_s, lats_s, dirs[order], speeds[order]
            )
        return WindOverlayRenderer._pressure_columns(lons_s, lats_s, values[order])

    @staticmethod
    def _is_array_format(data, expected_len):
        """Check if data is already in NumPy array format from grid()."""
        if not isinstance(data, tuple) or len(data) != expected_len:
            return False
        # Element 0 is a scalar lon/lat; elements 1+ are NumPy arrays.
        return all(hasattr(a, "shape") and hasattr(a, "dtype") for a in data[1:])

    @staticmethod
    def _columns_share_lats(columns, key):
        """Whether columns form one regular grid for fills and contours."""
        if not columns:
            return False
        # Array-columns: (lon, lats_array, dirs_array, speeds_array)
        # Tuple-columns: list of (dir, speed, (lat, lon)) points
        if WindOverlayRenderer._is_array_format(columns[0], 4):
            first_lats = columns[0][1]
            return all(
                len(col[1]) == len(first_lats) and np.array_equal(col[1], first_lats)
                for col in columns
            )
        if WindOverlayRenderer._is_array_format(columns[0], 3):
            first_lats = columns[0][1]
            return all(
                len(col[1]) == len(first_lats) and np.array_equal(col[1], first_lats)
                for col in columns
            )
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
            axes = self._resample_axes(bounds)
            if axes is None:
                return []
            lats, lons = axes
            nrows, ncols = len(lats), len(lons)
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
            columns = self._sampled_columns(values, lats, lons, field)
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
    def _resample_axes(bounds):
        south, north = sorted((bounds[0][0], bounds[1][0]))
        west, east = bounds[0][1], bounds[1][1]
        if east < west:
            east += 360.0
        span_lat = north - south
        span_lon = east - west
        if not (span_lat > 0 and span_lon > 0):
            return None
        ncols = min(240, max(2, int(round(span_lon / 0.1)) + 1))
        nrows = min(240, max(2, int(round(span_lat / 0.1)) + 1))
        total = ncols * nrows
        if total > 30000:
            scale = (30000.0 / total) ** 0.5
            ncols = max(2, int(ncols * scale))
            nrows = max(2, int(nrows * scale))
        return np.linspace(south, north, nrows), np.linspace(west, east, ncols)

    @staticmethod
    def _sampled_columns(values, lats, lons, field):
        # Model footprints rarely fill the padded viewport. Shrink to the
        # sampled footprint so coverage margins cannot empty every column;
        # dropping whole columns keeps one shared latitude vector behind for
        # fills and contouring.
        finite = np.isfinite(values).all(axis=-1)
        rows_with_data = np.flatnonzero(finite.any(axis=1))
        cols_with_data = np.flatnonzero(finite.any(axis=0))
        # No point carries a complete vector, so there is no footprint to
        # index. Return an empty field instead of indexing an empty range.
        if not len(rows_with_data) or not len(cols_with_data):
            return []
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
        # Wind columns are (lon, lats, directions, speeds); pressure columns
        # are (lon, lats, values).
        columns: list[tuple[Any, ...]] = []
        for index in range(values.shape[1]):
            if not finite[:, index].all():
                continue
            lon = float((lons[index] + 180.0) % 360.0 - 180.0)
            if field == "wind":
                columns.append(
                    (lon, lats, direction[:, index].copy(), speed[:, index].copy())
                )
            else:
                columns.append((lon, lats, values[:, index, 0].copy()))
        return columns

    @staticmethod
    def _show_isobars(job):
        # Palettes built before the isobar toggle carry no fourth element.
        return job.palette[3] if len(job.palette) > 3 else True

    def _render(self, job):
        before = dict(self.cache_metrics)
        before_fields = dict(self.field_cache_metrics)
        job.weather_metrics = {}
        resources: dict[str, Any] = {}
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

    def _prepare_vectors(self, job, data, fill_data):
        """Worker-side land flags and streamlines; False when the job is cancelled."""
        if data and self._wind_style(job.palette) != "streamlines":
            land: list[bool] = []
            for column in data:
                if job.cancel.is_set():
                    return False
                lon = float(column[0])
                # The flags line up with the flat barb vectors, which are
                # concatenated in the same column-then-latitude order.
                land.extend(
                    self._land_mask.contains(lat, lon) for lat in column[1].tolist()
                )
            job.barb_land = np.asarray(land, dtype=np.bool_)
        job.streamlines = None
        if data and self._wind_style(job.palette) == "streamlines":
            job.streamlines = trace_streamlines(
                job.viewport,
                fill_data if fill_data is not None else data,
                self._land_mask.contains,
                job.cancel.is_set,
                density=job.palette[9] if len(job.palette) > 9 else "standard",
            )
            if job.cancel.is_set():
                return False
        return True

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
        job.barb_flat = flatten_wind_columns(data)
        job.barb_land = None
        if data:
            self._land_mask.refresh_source(job.viewport.get_scale())
        if not self._prepare_vectors(job, data, fill_data):
            return None
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
        if self._closed:
            return
        # Land visibility changes foreground selection only, never the cached
        # fill/isobars or sampled field. Prepare flags with every wind job so
        # toggling this setting cannot blank the overlay waiting for a rebuild.
        self._barbs_over_land[canvas] = bool(palette[2])
        palette = palette[:2] + (True,) + palette[3:]
        if len(palette) > 9 and self._wind_style(palette) != "streamlines":
            palette = palette[:9] + ("standard",) + palette[10:]
        if charts:
            self._last_content[canvas] = (
                charts,
                palette,
                canvas.get_scale_factor(),
            )
            state = self._states.get(canvas)
            if state is not None:
                show_wind = palette[4] if len(palette) > 4 else True
                if not show_wind and state.frame is not None:
                    previous = state.frame[0].palette
                    if len(previous) < 5 or previous[4]:
                        self._hidden_wind_frames[canvas] = state.frame
                elif show_wind:
                    retained = self._hidden_wind_frames.pop(canvas, None)
                    key = (
                        palette,
                        canvas.get_scale_factor(),
                        tuple(c[0] for c in charts),
                    )
                    if retained is not None and retained[0].key == key:
                        state.frame = retained
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
        if self._wind_style(job.palette) == "streamlines":
            over_land = self._barbs_over_land.get(canvas, bool(job.palette[2]))
            view = (center_lat, center_lon, zoom, width, height, over_land)
            if getattr(job, "streamline_geometry_view", None) != view:
                job.streamline_geometry = geometry_for_view(
                    getattr(job, "streamlines", None),
                    center_lat,
                    center_lon,
                    zoom,
                    width,
                    height,
                    over_land,
                )
                job.streamline_geometry_view = view
            return job.streamline_geometry, float(job.palette[1])
        flat = getattr(job, "barb_flat", None)
        if flat is None:
            return {}, 0.0
        step, max_count = barb_density_parameters(density)
        over_land = self._barbs_over_land.get(canvas, bool(job.palette[2]))
        barb_view = (
            center_lat,
            center_lon,
            zoom,
            width,
            height,
            step,
            max_count,
            over_land,
        )
        # Repaints of a stationary view reuse geometry; gestures only update
        # positions/selection, never reload weather or scale symbol strokes.
        if getattr(job, "barb_geometry_view", None) != barb_view:
            # Retain only the current zoom's world pixels on this immutable
            # frame. Pans still recompute selection and exact screen positions.
            # Large frames use the uncached path rather than growing retention.
            projection = getattr(job, "barb_projection", None)
            if projection is not None and projection[0] != zoom:
                job.barb_projection = projection = None
            if projection is None and len(flat[0]) * 16 <= 8 * 1024 * 1024:
                pixels = mercator_project_nodes(flat[2], flat[3], zoom)
                for array in pixels:
                    array.setflags(write=False)
                job.barb_projection = projection = (zoom, pixels)
            job.barb_geometry, _count = WindPainter.live_geometry_for_view(
                flat,
                center_lat,
                center_lon,
                zoom,
                width,
                height,
                step=step,
                max_count=max_count,
                land_mask=None if over_land else getattr(job, "barb_land", None),
                projected=projection[1] if projection is not None else None,
            )
            job.barb_geometry_view = barb_view
        geometry = job.barb_geometry
        return geometry, float(job.palette[1])

    @staticmethod
    def _wind_style(palette):
        return (
            "streamlines"
            if len(palette) > 8 and palette[8] == "streamlines"
            else "barbs"
        )

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
