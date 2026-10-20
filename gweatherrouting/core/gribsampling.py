"""Shared GRIB record selection and linear space/time interpolation.

GDAL decodes packing; this module samples pixel centers, masks missing values,
combines split files within a model run, and never blends distinct model runs.
See docs/weather-sampling.md for selection and fallback policy.
"""

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import math
import logging
import re
from typing import cast

import numpy as np
from osgeo import gdal
from pyproj import CRS, Transformer, Geod

from .gribvectors import vector_flags

logger = logging.getLogger("gweatherrouting")

FIELDS = {"wind": ("UGRD", "VGRD"), "pressure": ("PRMSL", "MSLMA")}


def number(value):
    """GDAL versions expose times both as integers and '123 sec UTC'."""
    return float(str(value).split()[0])


def wind_value(uv):
    u, v = uv
    return (math.degrees(math.atan2(-u, -v)) + 360) % 360, math.hypot(u, v)


def model_key(metadata, source):
    ids = metadata.get("GRIB_IDS", "")
    center = re.search(r"(?:^|\s)CENTER=(\d+)", ids)
    subcenter = re.search(r"SUBCENTER=(\d+)", ids)
    origin = (
        center.group(1) if center else getattr(source, "centre", ""),
        subcenter.group(1) if subcenter else "",
    )
    pds = metadata.get("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES", "").split()
    # Template 4.0/4.1: process IDs at items 2..4; ensemble member at 15+.
    process = tuple(pds[2:5])
    if origin[0] == "7" and len(process) == 3 and process[2] in ("81", "96"):
        process = (process[0], process[1], "gfs")
    ensemble = tuple(pds[15:]) if metadata.get("GRIB_PDS_PDTN") == "1" else ()
    if not process:
        # GDAL GRIB1 exports may omit process identity. Never infer that two
        # anonymous files from the same center are the same model.
        process = (
            getattr(source, "model", None) or getattr(source, "path", str(id(source))),
        )
    status = re.search(r"PROD_STATUS=(\d+)", ids)
    return origin, process, ensemble, status.group(1) if status else ""


@dataclass(frozen=True)
class Record:
    index: object
    band: int
    element: str
    reference: float
    valid: float
    identity: tuple
    scale: float = 1.0
    grid_relative: bool = False


class GridIndex:
    """Metadata index and bounded decoded-band cache, private to each worker."""

    def __init__(self, source):
        self.source = source
        self.dataset = source.dataset
        self.transform = self.dataset.GetGeoTransform()
        self.inverse = gdal.InvGeoTransform(self.transform)
        if self.inverse is None:
            raise ValueError("GRIB grid has a singular geotransform")
        self.width, self.height = self.dataset.RasterXSize, self.dataset.RasterYSize
        projection = self.dataset.GetProjection()
        self.forward = self.backward = None
        crs = CRS(projection) if projection else CRS("EPSG:4326")
        self.geod = crs.get_geod() or Geod(ellps="WGS84")
        self.vector_step = 1e-5 if crs.is_geographic else 0.1
        if projection and (not crs.is_geographic or crs.is_derived):
            self.forward = Transformer.from_crs("EPSG:4326", projection, always_xy=True)
            self.backward = Transformer.from_crs(
                projection, "EPSG:4326", always_xy=True
            )
        self.periodic = (
            self.forward is None
            and self.transform[2] == self.transform[4] == 0
            and abs(abs(self.transform[1]) * self.width - 360) < 1e-6
        )
        self.arrays = OrderedDict()
        self._stencil = lru_cache(maxsize=4096)(self._stencil_for)
        path = self.dataset.GetDescription()
        self.vector_flags = vector_flags(path) if path else []
        self.records = self._records()
        self.references = {record.reference for record in self.records}

    @property
    def nominal_step_degrees(self):
        """Coarsest grid axis in degrees, for newest-first tiebreaks only."""
        step = max(abs(self.transform[1]), abs(self.transform[5]))
        if self.forward is None:
            return step
        return step / 111319.0

    def _records(self):
        records = []
        for band_id in range(1, self.dataset.RasterCount + 1):
            metadata = self.dataset.GetRasterBand(band_id).GetMetadata()
            element = metadata.get("GRIB_ELEMENT")
            if element not in ("UGRD", "VGRD", "PRMSL", "MSLMA"):
                continue
            level = metadata.get("GRIB_SHORT_NAME", "")
            expected = "10-HTGL" if element in FIELDS["wind"] else "0-MSL"
            if level and level != expected:
                continue
            relative = self.vector_flags[band_id - 1] if self.vector_flags else False
            if element in FIELDS["wind"] and relative is None:
                logger.warning("Unsupported GRIB vector grid: %s", self.source.path)
                continue
            # Interval statistics and ensemble-derived fields aren't point forecasts.
            if metadata.get("GRIB_PDS_PDTN", "0") not in ("0", "1"):
                continue
            try:
                reference = number(
                    metadata.get("GRIB_REF_TIME", self.source.start_time.timestamp())
                )
                valid = (
                    number(metadata["GRIB_VALID_TIME"])
                    if "GRIB_VALID_TIME" in metadata
                    else reference + number(metadata["GRIB_FORECAST_SECONDS"])
                )
                identity = (reference, model_key(metadata, self.source), expected)
            except (ValueError, KeyError):
                continue
            unit = metadata.get("GRIB_UNIT", "[Pa]")
            scale = (
                0.01
                if element in FIELDS["pressure"]
                and unit not in ("[hPa]", "hPa", "[mb]", "mb")
                else 1.0
            )
            records.append(
                Record(
                    self,
                    band_id,
                    element,
                    reference,
                    valid,
                    identity,
                    scale,
                    bool(relative),
                )
            )
        return records

    def array(self, record):
        if self.dataset is None:
            raise ValueError("Forecast handle is closed")
        cached = self.arrays.get(record.band)
        if cached is None:
            band = self.dataset.GetRasterBand(record.band)
            cached = np.asarray(band.ReadAsArray(), dtype=np.float64)
            invalid = ~np.isfinite(cached)
            nodata = band.GetNoDataValue()
            if nodata is not None:
                invalid |= cached == nodata
            if band.GetMaskFlags() != gdal.GMF_ALL_VALID:
                invalid |= band.GetMaskBand().ReadAsArray() == 0
            cached = np.where(invalid, np.nan, cached)
            self.arrays[record.band] = cached
            if len(self.arrays) > 6:
                self.arrays.popitem(last=False)
        self.arrays.move_to_end(record.band)
        return cached

    def pixel(self, lats, lons):
        x, y = lons, lats
        if self.forward is not None:
            x, y = self.forward.transform(lons, lats)
        else:
            gt = self.transform
            center = gt[0] + self.width / 2 * gt[1] + self.height / 2 * gt[2]
            x = lons + 360 * np.round((center - lons) / 360)
        inv = self.inverse
        return (
            inv[0] + inv[1] * x + inv[2] * y - 0.5,
            inv[3] + inv[4] * x + inv[5] * y - 0.5,
        )

    def sample(self, record, lats, lons, vector=False):
        x, y = self.pixel(lats, lons)
        if self.periodic:
            x = x % self.width
        inside = (
            np.isfinite(x)
            & np.isfinite(y)
            & (y >= -1e-7)
            & (y <= self.height - 1 + 1e-7)
        )
        if not self.periodic:
            inside &= (x >= -1e-7) & (x <= self.width - 1 + 1e-7)
        # Clip before casting to keep invalid/outside queries safe to index.
        x = np.clip(
            np.nan_to_num(x), 0, self.width if self.periodic else self.width - 1
        )
        y = np.clip(np.nan_to_num(y), 0, self.height - 1)
        x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
        x1 = (
            (x0 + 1) % self.width
            if self.periodic
            else np.minimum(x0 + 1, self.width - 1)
        )
        y1 = np.minimum(y0 + 1, self.height - 1)
        dx, dy = x - x0, y - y0
        if not inside.any():
            return np.full(lats.shape + ((2,) if vector else ()), np.nan)
        array = self.array(record)
        value = np.zeros(lats.shape + ((2,) if vector else ()))
        corners = (
            (x0, y0, (1 - dx) * (1 - dy)),
            (x1, y0, dx * (1 - dy)),
            (x0, y1, (1 - dx) * dy),
            (x1, y1, dx * dy),
        )
        bases = self._corner_basis(record, corners) if vector else None
        for position, (xx, yy, weight) in enumerate(corners):
            corner = array[yy, xx]
            needed = weight > 1e-12
            inside &= ~needed | np.isfinite(corner)
            contribution = np.where(needed, np.nan_to_num(corner) * weight, 0)
            if vector:
                contribution = (
                    contribution[:, None] * cast("list[np.ndarray]", bases)[position]
                )
            value += contribution
        return np.where(inside[:, None] if vector else inside, value, np.nan)

    def _corner_basis(self, record, corners):
        """The true east/north basis at a stencil's corners.

        The four corner groups of one stencil share their corners: a raster
        node is a corner of four neighbouring stencils.  The basis is
        therefore computed once per *distinct* corner and gathered back, which
        leaves every value unchanged and removes the repeated geodesic
        inversions that a second pass over the same window would repeat.
        """
        if self.backward is None:
            return [self.basis(record, xx, yy) for xx, yy, _weight in corners]
        keys = np.concatenate(
            [xx.astype(np.int64) * self.height + yy for xx, yy, _weight in corners]
        )
        unique, inverse = np.unique(keys, return_inverse=True)
        basis = self.basis(
            record,
            (unique // self.height).astype(int),
            (unique % self.height).astype(int),
        )
        gathered = []
        offset = 0
        for xx, _yy, _weight in corners:
            taken = inverse[offset : offset + len(xx)]
            offset += len(xx)
            gathered.append(basis[taken])
        return gathered

    def basis(self, record, xx, yy):
        """True east/north unit vector at each native sample center."""
        axis = record.element == "UGRD"
        if self.backward is None:
            return np.tile((1, 0) if axis else (0, 1), (len(xx), 1))
        gt = self.transform
        x = gt[0] + (xx + 0.5) * gt[1] + (yy + 0.5) * gt[2]
        y = gt[3] + (xx + 0.5) * gt[4] + (yy + 0.5) * gt[5]
        step = self.vector_step
        lon, lat = self.backward.transform(x, y)
        lon2, lat2 = self.backward.transform(
            x + step if axis else x, y if axis else y + step
        )
        azimuth, _, _ = self.geod.inv(lon, lat, lon2, lat2)
        angle = np.radians(azimuth)
        return np.column_stack((np.sin(angle), np.cos(angle)))

    def vector_at(self, record, lat, lon):
        return self.sample(record, np.array([lat]), np.array([lon]), vector=True)[0]

    def _stencil_for(self, lat, lon):
        if not math.isfinite(lat) or not math.isfinite(lon):
            return ()
        if self.forward is not None:
            x, y = self.forward.transform(lon, lat)
        else:
            gt = self.transform
            center = gt[0] + self.width / 2 * gt[1] + self.height / 2 * gt[2]
            x, y = lon + 360 * round((center - lon) / 360), lat
        inv = self.inverse
        x, y = (
            inv[0] + inv[1] * x + inv[2] * y - 0.5,
            inv[3] + inv[4] * x + inv[5] * y - 0.5,
        )
        if not math.isfinite(x) or not math.isfinite(y):
            return ()
        if self.periodic:
            x %= self.width
        if y < -1e-7 or y > self.height - 1 + 1e-7:
            return ()
        if not self.periodic and (x < -1e-7 or x > self.width - 1 + 1e-7):
            return ()
        x = max(0, min(x, self.width - 1)) if not self.periodic else x
        y = max(0, min(y, self.height - 1))
        x0, y0 = math.floor(x), math.floor(y)
        x1 = (x0 + 1) % self.width if self.periodic else min(x0 + 1, self.width - 1)
        y1 = min(y0 + 1, self.height - 1)
        dx, dy = x - x0, y - y0
        return tuple(
            (xx, yy, w)
            for xx, yy, w in (
                (x0, y0, (1 - dx) * (1 - dy)),
                (x1, y0, dx * (1 - dy)),
                (x0, y1, (1 - dx) * dy),
                (x1, y1, dx * dy),
            )
            if w > 1e-12
        )

    def sample_at(self, record, lat, lon):
        stencil = self._stencil(lat, lon)
        if not stencil:
            return math.nan
        array = self.array(record)
        return sum(float(array[y, x]) * weight for x, y, weight in stencil)

    def nodes(self, bounds):
        """Native grid nodes within a view; don't build a global Python tuple grid."""
        south, north = sorted((bounds[0][0], bounds[1][0]))
        west, east = bounds[0][1], bounds[1][1]
        if east < west:
            east += 360
        if self.forward is None and self.transform[2] == self.transform[4] == 0:
            return self._geographic_nodes(south, north, west, east)
        lats = np.array([south, south, north, north])
        lons = np.array([west, east, west, east])
        x, y = self.pixel(lats, lons)
        # Geographic global grids can cross their storage seam.
        if self.periodic and (np.ptp(x) > self.width / 2 or east - west >= 359):
            left, right = 0, self.width - 1
        else:
            left = max(0, int(math.floor(np.min(x))))
            right = min(self.width - 1, int(math.ceil(np.max(x))))
        top = max(0, int(math.floor(np.min(y))))
        bottom = min(self.height - 1, int(math.ceil(np.max(y))))
        if right < left or bottom < top:
            return np.empty((0, 2))
        xx, yy = np.meshgrid(
            np.arange(left, right + 1) + 0.5, np.arange(top, bottom + 1) + 0.5
        )
        gt = self.transform
        lon = gt[0] + xx * gt[1] + yy * gt[2]
        lat = gt[3] + xx * gt[4] + yy * gt[5]
        if self.backward is not None:
            lon, lat = self.backward.transform(lon, lat)
        lon = lon + 360 * np.round(((west + east) / 2 - lon) / 360)
        use = (
            (lat >= south - 1e-7)
            & (lat <= north + 1e-7)
            & (lon >= west - 1e-7)
            & (lon <= east + 1e-7)
        )
        return np.column_stack((lat[use], lon[use]))

    def _geographic_nodes(self, south, north, west, east):
        gt = self.transform
        lat = gt[3] + (np.arange(self.height) + 0.5) * gt[5]
        lon = gt[0] + (np.arange(self.width) + 0.5) * gt[1]
        lon = west + (lon - west) % 360
        lat = lat[(lat >= south - 1e-7) & (lat <= north + 1e-7)]
        lon = lon[(lon >= west - 1e-7) & (lon <= east + 1e-7)]
        xx, yy = np.meshgrid(lon, lat)
        return np.column_stack((yy.ravel(), xx.ravel()))

    def close(self):
        self.arrays.clear()
        self._stencil.cache_clear()
        self.dataset = None


class WeatherSampler:
    """Newest compatible model run wins; file order, finest grid, then largest
    coverage break ties between models from the same issue time."""

    _GRID_CACHE_TOLERANCE_DEG = (
        0.5  # ~55 km; consecutive pan steps within this hit cache
    )
    _GRID_CACHE_ENTRY_LIMIT = 64
    _GRID_CACHE_BYTE_LIMIT = 64 * 1024 * 1024

    def __init__(self, sources):
        self.indices = []
        for source in sources:
            try:
                self.indices.append(source.sampling)
            except (ValueError, AttributeError, RuntimeError) as exc:
                logger.warning("Weather grid unavailable: %s", exc)
        self.last_sources = set()
        self.groups: dict[tuple, dict[float, dict[str, list[Record]]]] = {}
        self.coverage: dict[tuple, float] = {}
        for index in self.indices:
            for record in index.records:
                group = self.groups.setdefault(record.identity, {})
                group.setdefault(record.valid, {}).setdefault(
                    record.element, []
                ).append(record)
                self.coverage[record.identity] = max(
                    self.coverage.get(record.identity, record.valid),
                    record.valid,
                    (
                        index.source.end_time.timestamp()
                        if len(index.references) == 1
                        else record.valid
                    ),
                )
        self._grid_cache: OrderedDict[tuple, tuple] = OrderedDict()
        self._grid_cache_bytes = 0
        # Stable sort preserves the first enabled file for equal issue times.
        resolutions: dict = {}
        spans: dict = {}
        for index in self.indices:
            cells = index.width * index.height
            for identity in {record.identity for record in index.records}:
                if identity not in resolutions:
                    resolutions[identity] = index.nominal_step_degrees
                    spans[identity] = cells
                else:
                    resolutions[identity] = min(
                        resolutions[identity], index.nominal_step_degrees
                    )
                    spans[identity] += cells
        self.order = sorted(
            self.groups,
            key=lambda key: (
                -key[0],
                resolutions.get(key, math.inf),
                -spans.get(key, 0),
            ),
        )
        self._times = {
            (identity, field): tuple(
                sorted(
                    t
                    for t, records in group.items()
                    if any(e in records for e in FIELDS[field])
                )
            )
            for identity, group in self.groups.items()
            for field in FIELDS
        }
        self._pairs = {
            (identity, t): self._wind_pairs(records)
            for identity, group in self.groups.items()
            for t, records in group.items()
        }

    @staticmethod
    def _sample_record(record, lats, lons):
        try:
            return record.index.sample(record, lats, lons) * record.scale
        except (ValueError, RuntimeError, TypeError, IndexError) as exc:
            logger.debug("Weather record unavailable: %s", exc)
            return np.full(lats.shape, np.nan)

    @staticmethod
    def _origin(*records):
        return "\x1f".join(
            sorted({getattr(r.index.source, "name", "unknown") for r in records})
        )

    def _pressure_at_time(self, records, lats, lons):
        result = np.full((len(lats), 1), np.nan)
        origins = np.full(len(lats), "", dtype=object)
        for element in FIELDS["pressure"]:
            for record in records.get(element, ()):
                use = ~np.isfinite(result[:, 0])
                if not use.any():
                    return result, origins
                candidate = self._sample_record(record, lats[use], lons[use])
                ids = np.flatnonzero(use)[np.isfinite(candidate)]
                result[ids, 0] = candidate[np.isfinite(candidate)]
                origins[ids] = self._origin(record)
        return result, origins

    @staticmethod
    def _wind_pairs(records):
        us, vs = records.get("UGRD", ()), records.get("VGRD", ())
        paired = [
            (u, v)
            for u in us
            for v in vs
            if u.index is v.index and u.grid_relative == v.grid_relative
        ]
        complete = {u.index for u, v in paired}
        return paired + [
            (u, v)
            for u in us
            for v in vs
            if u.index not in complete
            and v.index not in complete
            and u.grid_relative == v.grid_relative
        ]

    def _at_time(self, records, field, lats, lons):
        if field == "pressure":
            return self._pressure_at_time(records, lats, lons)
        result = np.full((len(lats), 2), np.nan)
        origins = np.full(len(lats), "", dtype=object)
        for u, v in self._wind_pairs(records):
            use = ~np.isfinite(result).all(axis=1)
            if not use.any():
                break
            candidate = np.column_stack(
                (
                    self._sample_record(u, lats[use], lons[use]),
                    self._sample_record(v, lats[use], lons[use]),
                )
            )
            if u.grid_relative:
                candidate = u.index.sample(u, lats[use], lons[use], vector=True)
                candidate += v.index.sample(v, lats[use], lons[use], vector=True)
            good = np.isfinite(candidate).all(axis=1)
            ids = np.flatnonzero(use)[good]
            result[ids] = candidate[good]
            origins[ids] = self._origin(u, v)
        return result, origins

    def _side(self, group, times, field, lats, lons, query, before, cancel):
        values = np.full((len(lats), 2 if field == "wind" else 1), np.nan)
        stamps = np.full(len(lats), np.nan)
        origins = np.full(len(lats), "", dtype=object)
        for valid in times:
            if cancel():
                break
            use = ~np.isfinite(stamps) & (
                (query >= valid) if before else (query <= valid)
            )
            if not use.any():
                continue
            candidate, names = self._at_time(group[valid], field, lats[use], lons[use])
            good = np.isfinite(candidate).all(axis=1)
            ids = np.flatnonzero(use)[good]
            values[ids], stamps[ids], origins[ids] = candidate[good], valid, names[good]
            if np.isfinite(stamps).all():
                break
        return values, stamps, origins

    @staticmethod
    def _blend(a, ta, sa, b, tb, sb, stamps):
        av, bv = np.isfinite(ta), np.isfinite(tb)
        values = np.where(av[:, None], a, b)
        names = np.where(av, sa, sb)
        both = av & bv & (tb > ta)
        weight = (stamps[both] - ta[both]) / (tb[both] - ta[both])
        values[both] = a[both] + (b[both] - a[both]) * weight[:, None]
        for i in np.flatnonzero(both):
            names[i] = "\x1f".join(
                sorted(set(sa[i].split("\x1f")) | set(sb[i].split("\x1f")))
            )
        return values, names

    def sample(self, field, instant, lats, lons, cancel=lambda: False):
        return self.sample_timestamps(field, instant.timestamp(), lats, lons, cancel)

    def sample_timestamps(self, field, stamps, lats, lons, cancel=lambda: False):
        stamps, lats, lons = np.broadcast_arrays(
            np.atleast_1d(stamps).astype(float),
            np.atleast_1d(lats).astype(float),
            np.atleast_1d(lons).astype(float),
        )
        stamps, lats, lons = stamps.ravel(), lats.ravel(), lons.ravel()
        result = np.full((len(lats), 2 if field == "wind" else 1), np.nan)
        origins = np.full(len(lats), "", dtype=object)
        for identity in self.order:
            if cancel():
                break
            use = (
                ~np.isfinite(result).all(axis=1)
                & (stamps >= identity[0])
                & (stamps <= self.coverage[identity])
            )
            if not use.any():
                continue
            group = self.groups[identity]
            times = self._times[identity, field]
            a, ta, sa = self._side(
                group,
                reversed(times),
                field,
                lats[use],
                lons[use],
                stamps[use],
                True,
                cancel,
            )
            b, tb, sb = self._side(
                group, times, field, lats[use], lons[use], stamps[use], False, cancel
            )
            result[use], origins[use] = self._blend(a, ta, sa, b, tb, sb, stamps[use])
        self.last_sources = {
            name for origin in origins for name in origin.split("\x1f") if name
        }
        self.point_sources = np.array(
            [", ".join(origin.split("\x1f")) if origin else None for origin in origins],
            dtype=object,
        )
        return result

    @staticmethod
    def _scalar_record(record, lat, lon):
        try:
            return record.index.sample_at(record, lat, lon)
        except (ValueError, RuntimeError, TypeError, IndexError):
            return math.nan

    def _scalar_wind(self, identity, times, lat, lon):
        for valid in times:
            for u, v in self._pairs[identity, valid]:
                uv = (
                    self._scalar_record(u, lat, lon),
                    self._scalar_record(v, lat, lon),
                )
                if u.grid_relative:
                    uv = tuple(
                        u.index.vector_at(u, lat, lon) + v.index.vector_at(v, lat, lon)
                    )
                if all(math.isfinite(value) for value in uv):
                    return valid, uv, self._origin(u, v)
        return None

    def wind_at(self, instant, lat, lon):
        stamp = instant.timestamp()
        self.last_sources = set()
        for identity in self.order:
            if not identity[0] <= stamp <= self.coverage[identity]:
                continue
            times = self._times[identity, "wind"]
            a = self._scalar_wind(
                identity, (t for t in reversed(times) if t <= stamp), lat, lon
            )
            b = (
                a
                if a and a[0] == stamp
                else self._scalar_wind(
                    identity, (t for t in times if t >= stamp), lat, lon
                )
            )
            if a is None and b is None:
                continue
            chosen = a if a is not None else b
            valid, uv, names = chosen
            self.last_sources.update(names.split("\x1f"))
            if a is not None and b is not None and b[0] > a[0]:
                weight = (stamp - a[0]) / (b[0] - a[0])
                uv = tuple(x + (y - x) * weight for x, y in zip(a[1], b[1]))
                self.last_sources.update(b[2].split("\x1f"))
            return wind_value(uv)
        return None

    @staticmethod
    def _round_bounds(bounds):
        """Round viewport bounds to a coarse lattice so nearby pans reuse cache.

        Rounding to ~0.05 degrees (~5 km) keeps cache keys stable across
        consecutive pan steps while introducing at most one native grid cell of
        positional error, which is below the sampling resolution.
        """
        (south, west), (north, east) = bounds
        return (
            (round(south / 0.05) * 0.05, round(west / 0.05) * 0.05),
            (round(north / 0.05) * 0.05, round(east / 0.05) * 0.05),
        )

    def _cached_grid(self, field, instant, bounds):
        key = (field, instant, self._round_bounds(bounds))
        cached = self._grid_cache.get(key)
        if cached is not None:
            # Move to end for LRU eviction order.
            result, _bytes = cached
            del self._grid_cache[key]
            self._grid_cache[key] = (result, _bytes)
            return result
        return None

    def _store_cached_grid(self, field, instant, bounds, result):
        key = (field, instant, self._round_bounds(bounds))
        # Estimate bytes: arrays are compact NumPy buffers, not Python tuples
        byte_estimate = sum(getattr(a, "nbytes", 0) for a in result)
        if byte_estimate > self._GRID_CACHE_BYTE_LIMIT:
            return
        if key in self._grid_cache:
            self._grid_cache_bytes -= self._grid_cache[key][1]
        self._grid_cache[key] = (result, byte_estimate)
        self._grid_cache_bytes += byte_estimate
        while (
            len(self._grid_cache) > self._GRID_CACHE_ENTRY_LIMIT
            or self._grid_cache_bytes > self._GRID_CACHE_BYTE_LIMIT
        ):
            _, (_, evicted_bytes) = self._grid_cache.popitem(last=False)
            self._grid_cache_bytes -= evicted_bytes

    def grid(self, field, instant, bounds, cancel=lambda: False):
        cached = self._cached_grid(field, instant, bounds)
        if cached is not None:
            return cached
        nodes = [
            index.nodes(bounds)
            for index in self.indices
            if any(r.element in FIELDS[field] for r in index.records)
        ]
        if not nodes:
            return None
        positions = np.unique(np.concatenate(nodes), axis=0)
        if not len(positions):
            return None
        values = self.sample(field, instant, positions[:, 0], positions[:, 1], cancel)
        result: tuple
        if field == "wind":
            speeds = np.hypot(values[..., 0], values[..., 1])
            directions = (
                np.degrees(np.arctan2(-values[..., 0], -values[..., 1])) + 360.0
            ) % 360.0
            finite = np.isfinite(speeds) & np.isfinite(directions)
            lats = positions[finite, 0].astype(np.float64)
            lons = positions[finite, 1].astype(np.float64)
            dirs = directions[finite].astype(np.float64)
            spds = speeds[finite].astype(np.float64)
            lons = (lons + 180) % 360 - 180
            result = (dirs, spds, lats, lons)
        else:
            finite = np.isfinite(values).all(axis=-1)
            lats = positions[finite, 0].astype(np.float64)
            lons = positions[finite, 1].astype(np.float64)
            vals = values[finite, 0].astype(np.float64)
            result = (lats, lons, vals)
        self._store_cached_grid(field, instant, bounds, result)
        return result
