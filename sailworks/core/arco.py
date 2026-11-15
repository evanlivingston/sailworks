"""Anonymous ARCO ERA5 hourly wind acquisition for the ERA5 history catalog.

ARCO ERA5 is Google's public analysis-ready ERA5 store. It serves the same
0.25 degree hourly winds as the CDS single-levels dataset, so this service
reuses the ERA5 request, validation, catalog and publication machinery while
reading the public Zarr store anonymously instead of submitting CDS jobs.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .era5 import (
    FIELD_VARIABLES,
    Era5DownloadService,
    Era5Error,
    split_request,
    validate_grib,
)

STORE_URL = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
STORAGE_OPTIONS = {"token": "anon"}
WIND_VARIABLES = FIELD_VARIABLES["wind"]
U_VARIABLE = WIND_VARIABLES[0]
V_VARIABLE = WIND_VARIABLES[1]
DATASET_LABEL = "arco-era5"
SOURCE_LABEL = "ARCO ERA5 hourly single levels reanalysis"
GRID_SPACING = 0.25
_MISSING = 9999.0
_EPSILON = 1e-6


class ArcoError(Era5Error):
    """An actionable ARCO ERA5 acquisition or validation error."""


def _no_cds(*_args, **_kwargs):
    raise ArcoError(
        "ARCO ERA5 reads the anonymous public Zarr store and never submits CDS "
        "requests"
    )


def open_store(url=STORE_URL, *, storage_options=None):
    """Open the public ARCO ERA5 store with anonymous Google Cloud access."""
    try:
        import xarray as xr
    except ImportError:
        raise ArcoError(
            "ARCO ERA5 needs the xarray, gcsfs and zarr extras installed with "
            "WeatherNext"
        ) from None
    options = (
        dict(storage_options) if storage_options is not None else dict(STORAGE_OPTIONS)
    )
    try:
        return xr.open_zarr(url, storage_options=options, consolidated=True)
    except Exception as exc:
        raise ArcoError(
            "cannot open the anonymous ARCO ERA5 store "
            f"({type(exc).__name__}); check network access to Google Cloud "
            "Storage and retry"
        ) from exc


def _parse_time(value):
    """Parse a store attribute timestamp, or return ``None``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                cleaned = text.replace("Z", "").replace("+00:00", "")
                seconds = int(
                    np.datetime64(cleaned).astype("datetime64[s]").astype(np.int64)
                )
            except (TypeError, ValueError):
                return None
            stamp = datetime.fromtimestamp(seconds, timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _availability(dataset):
    """Return the store's advertised start, stable stop and ERA5T stop."""
    attrs = getattr(dataset, "attrs", None) or {}
    start = _parse_time(attrs.get("valid_time_start"))
    era5t_stop = _parse_time(attrs.get("valid_time_stop_era5t"))
    if start is None or era5t_stop is None:
        raise ArcoError(
            "ARCO ERA5 store does not advertise its available range "
            "(valid_time_start / valid_time_stop_era5t)"
        )
    stable_stop = _parse_time(attrs.get("valid_time_stop")) or era5t_stop
    return {"start": start, "stable_stop": stable_stop, "era5t_stop": era5t_stop}


def _check_available(plan, available):
    """Reject unsupported times and report whether ERA5T hours are included."""
    if plan.start < available["start"]:
        raise ArcoError(
            "ARCO ERA5 starts at "
            f"{available['start'].isoformat()}; requested {plan.start.isoformat()} "
            "is unsupported"
        )
    if plan.end > available["era5t_stop"]:
        raise ArcoError(
            "ARCO ERA5 ends at "
            f"{available['era5t_stop'].isoformat()}; requested {plan.end.isoformat()} "
            "is unsupported"
        )
    return plan.end > available["stable_stop"]


def _hours(piece):
    cursor = piece.start
    while cursor <= piece.end:
        yield cursor
        cursor += timedelta(hours=1)


def _time_positions(dataset, hours):
    """Locate every requested hour exactly, rejecting silent gaps."""
    indexes = getattr(dataset, "indexes", None)
    index = indexes.get("time") if indexes is not None else None
    if index is None or not hasattr(index, "get_indexer"):
        raise ArcoError("ARCO ERA5 store has no readable UTC time index")
    keys = np.array(
        [np.datetime64(hour.replace(tzinfo=None), "ns") for hour in hours],
        dtype="datetime64[ns]",
    )
    try:
        positions = np.asarray(index.get_indexer(keys), dtype=np.int64)
    except Exception as exc:
        raise ArcoError(
            "ARCO ERA5 store time index is not a standard UTC hourly index"
        ) from exc
    missing = [hour for hour, position in zip(hours, positions) if position < 0]
    if missing:
        detail = f" and {len(missing) - 1} more hour(s)" if len(missing) > 1 else ""
        raise ArcoError(
            "ARCO ERA5 has no hourly data for "
            f"{missing[0].isoformat()}{detail}; refusing to leave a gap"
        )
    return tuple(int(position) for position in positions)


def _check_axis(values, name, *, descending):
    if values.ndim != 1 or values.size < 2:
        raise ArcoError(f"ARCO ERA5 {name} coordinate is too small for a GRIB grid")
    if not np.all(np.isfinite(values)):
        raise ArcoError(f"ARCO ERA5 {name} coordinate contains non-finite values")
    steps = np.diff(values)
    if descending:
        if not np.all(steps < 0):
            raise ArcoError(f"ARCO ERA5 {name} must run north to south")
        steps = -steps
    elif not np.all(steps > 0):
        raise ArcoError(f"ARCO ERA5 {name} must run west to east")
    if not np.allclose(steps, GRID_SPACING, atol=_EPSILON):
        raise ArcoError(f"ARCO ERA5 {name} is not a uniform 0.25 degree grid")


def _grid(dataset):
    try:
        lat = np.asarray(dataset["latitude"].values, dtype=float)
        lon = np.asarray(dataset["longitude"].values, dtype=float)
    except Exception as exc:
        raise ArcoError(
            "ARCO ERA5 store is missing its latitude/longitude coordinates"
        ) from exc
    _check_axis(lat, "latitude", descending=True)
    _check_axis(lon, "longitude", descending=False)
    if lon[0] < 0 or lon[-1] >= 360:
        raise ArcoError("ARCO ERA5 longitude must be stored on a 0..360 grid")
    return lat, lon


def _require_wind(plan):
    requested = set(plan.request.get("variable") or ())
    if requested != set(WIND_VARIABLES):
        raise ArcoError(
            "ARCO ERA5 provides 10 m wind only; plan the request with "
            "fields=('wind',)"
        )


def _require_variables(dataset):
    for name in WIND_VARIABLES:
        try:
            present = name in dataset
        except TypeError:
            present = False
        if not present:
            raise ArcoError(f"ARCO ERA5 store is missing the {name} wind variable")


def _validate_area(area):
    try:
        north, west, south, east = (float(value) for value in area)
    except (TypeError, ValueError):
        raise ArcoError("ARCO ERA5 area must be north, west, south, east") from None
    if not (-90 <= south < north <= 90):
        raise ArcoError("ARCO ERA5 area has invalid north/south bounds")
    if not (-180 <= west < east <= 180):
        raise ArcoError(
            "ARCO ERA5 areas must not cross the antimeridian; west must be less "
            "than east within -180..180"
        )
    return north, west, south, east


def _padded_bounds(area):
    """Expand the requested box outward to whole grid cells, never inward."""
    north, west, south, east = area
    top = min(90.0, np.ceil(north / GRID_SPACING - _EPSILON) * GRID_SPACING)
    bottom = max(-90.0, np.floor(south / GRID_SPACING + _EPSILON) * GRID_SPACING)
    left = np.floor(west / GRID_SPACING + _EPSILON) * GRID_SPACING
    right = np.ceil(east / GRID_SPACING - _EPSILON) * GRID_SPACING
    return float(top), float(left), float(bottom), float(right)


def _select_lat(lat, area):
    top, _left, bottom, _right = _padded_bounds(area)
    indexes = np.where((lat <= top + _EPSILON) & (lat >= bottom - _EPSILON))[0]
    if indexes.size < 2:
        raise ArcoError("ARCO ERA5 area selects fewer than two latitudes")
    values = lat[indexes]
    steps = -np.diff(values)
    if not np.all(steps > 0) or not np.allclose(steps, GRID_SPACING, atol=_EPSILON):
        raise ArcoError("ARCO ERA5 latitude selection is not a contiguous 0.25 grid")
    return indexes, values


def _normalize_lon(values, *, wrap_180):
    if wrap_180:
        return np.where(values >= 180.0, values - 360.0, values)
    return np.where(values > 180.0, values - 360.0, values)


def _select_lon(lon, area):
    _top, left, _bottom, right = _padded_bounds(area)
    start = left % 360.0
    stop = right % 360.0
    # The store runs 0..360, so a Greenwich-crossing box is two contiguous
    # sections. Put the high-longitude section first so the normalized
    # -180..180 values stay strictly increasing.
    if left < 0 <= right:
        high = np.where(lon >= start - _EPSILON)[0]
        low = np.where((lon <= stop + _EPSILON) & (lon < start - _EPSILON))[0]
        # The wrapped section owns the 180 degree seam, so 180 becomes -180
        # there; the unwrapped section keeps +180 when the box reaches it.
        values = np.concatenate(
            [
                _normalize_lon(lon[high], wrap_180=True),
                _normalize_lon(lon[low], wrap_180=False),
            ]
        )
        indexes = np.concatenate([high, low])
    else:
        indexes = np.where((lon >= start - _EPSILON) & (lon <= stop + _EPSILON))[0]
        # A box entirely west of Greenwich starts at 180 in the 0..360 store
        # and must normalize through -180 to stay contiguous.
        values = _normalize_lon(lon[indexes], wrap_180=left < 0)
    if indexes.size < 2:
        raise ArcoError("ARCO ERA5 area selects fewer than two longitudes")
    steps = np.diff(values)
    if not np.all(steps > 0) or not np.allclose(steps, GRID_SPACING, atol=_EPSILON):
        raise ArcoError("ARCO ERA5 longitude selection is not a contiguous 0.25 grid")
    return indexes, values


def _read_field(dataset, name, position, lat_index, lon_index):
    try:
        block = dataset[name].isel(
            time=position, latitude=lat_index, longitude=lon_index
        )
        values = np.asarray(block.values, dtype=float)
    except Exception as exc:
        raise ArcoError(
            f"cannot read {name} from the anonymous ARCO ERA5 store"
        ) from exc
    expected = (len(lat_index), len(lon_index))
    if values.shape != expected:
        raise ArcoError(
            f"ARCO ERA5 {name} selection returned {values.shape} instead of "
            f"the requested {expected} grid"
        )
    if not np.isfinite(values).any():
        raise ArcoError(
            f"ARCO ERA5 {name} has no finite values for the requested grid and hour"
        )
    return values


def _write_message(eccodes, handle, short_name, valid, lat, lon, values):
    """Append one regular_ll_sfc_grib2 analysis message."""
    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        keys = {
            "shortName": short_name,
            "typeOfLevel": "heightAboveGround",
            "level": 10,
            "dataDate": int(valid.strftime("%Y%m%d")),
            "dataTime": int(valid.strftime("%H%M")),
            "forecastTime": 0,
            "stepRange": "0",
            "startStep": 0,
            "endStep": 0,
            "gridType": "regular_ll",
            "Ni": int(len(lon)),
            "Nj": int(len(lat)),
            "latitudeOfFirstGridPointInDegrees": float(lat[0]),
            "longitudeOfFirstGridPointInDegrees": float(lon[0]),
            "latitudeOfLastGridPointInDegrees": float(lat[-1]),
            "longitudeOfLastGridPointInDegrees": float(lon[-1]),
            "iDirectionIncrementInDegrees": float(abs(lon[1] - lon[0])),
            "jDirectionIncrementInDegrees": float(abs(lat[1] - lat[0])),
            "iScansNegatively": 0,
            "jScansPositively": 1 if lat[-1] > lat[0] else 0,
            "jPointsAreConsecutive": 0,
            "alternativeRowScanning": 0,
            "centre": 98,
            "subCentre": 0,
            "missingValue": _MISSING,
            "bitmapPresent": 1,
        }
        for key, value in keys.items():
            eccodes.codes_set(gid, key, value)
        encoded = np.asarray(values, dtype=float).ravel()
        encoded[~np.isfinite(encoded)] = _MISSING
        eccodes.codes_set_values(gid, encoded)
        eccodes.codes_write(gid, handle)
    finally:
        eccodes.codes_release(gid)


class ArcoDownloadService(Era5DownloadService):
    """Read anonymous ARCO ERA5 winds into the existing ERA5 history catalog."""

    queued_stage = "Queued for ARCO ERA5…"
    # Short label for callers (Qt progress wrappers) that show the source.
    source_label = "ARCO"
    catalog_dataset = DATASET_LABEL
    catalog_source = SOURCE_LABEL

    def __init__(
        self,
        store,
        *,
        opener=open_store,
        encoder=None,
        validator=validate_grib,
        store_url=STORE_URL,
    ):
        super().__init__(store, client_factory=_no_cds, validator=validator)
        self.opener = opener
        self.encoder = encoder
        self.store_url = store_url

    # --- CDS bookkeeping is inert: ARCO has no requests to queue or resume ---

    def _submit_with_backoff(self, piece, cancel):
        return None

    def _ensure_chunk_request(self, piece):
        raise ArcoError("ARCO ERA5 does not submit CDS requests")

    def _load_pending(self):
        return []

    def _pending_entry(self, entries, piece):
        return None

    def _forget_pending(self, request_id):
        return None

    def pending_requests(self):
        return []

    def pending_count(self):
        return 0

    def resume_pending(self):
        return 0

    def _failure_message(self, exc):
        if isinstance(exc, Era5Error):
            return str(exc)
        return (
            f"ARCO ERA5 read failed ({type(exc).__name__}); check anonymous "
            f"access to {self.store_url} and retry."
        )

    def _open(self):
        try:
            return self.opener(self.store_url)
        except ArcoError:
            raise
        except Exception as exc:
            raise ArcoError(
                "cannot open the anonymous ARCO ERA5 store "
                f"({type(exc).__name__}); check network access to Google Cloud "
                "Storage and retry"
            ) from exc

    def _eccodes(self):
        if self.encoder is not None:
            return self.encoder
        try:
            import eccodes
        except ImportError:
            raise ArcoError(
                "ARCO ERA5 GRIB encoding needs the eccodes package; install it "
                "and retry"
            ) from None
        return eccodes

    def _run(self, plan, cancel, retry=False):
        key = self._job_key(plan)
        _require_wind(plan)
        _validate_area(plan.area)
        root = Path(self.store.root)
        catalog = root / "era5-history.json"
        pieces = list(split_request(plan))
        total = len(pieces)
        self._set_progress(
            key, done=0, total=total, stage=f"Planning {total} ARCO chunk(s)…"
        )
        entries = self._catalog_entries(catalog)
        done = 0
        pending = []
        for n, piece in enumerate(pieces):
            if cancel.is_set():
                raise ArcoError("ARCO ERA5 download canceled")
            if self._catalog_covers(entries, piece):
                done += 1
                self._set_progress(
                    key,
                    done=done,
                    stage=(
                        f"Chunk {n + 1}/{total}: already in the ERA5 catalog "
                        "(cached source may differ)"
                    ),
                )
                continue
            pending.append((n, piece))
        if not pending:
            self._set_progress(key, done=total, stage="Already available")
            return

        staged = []
        dataset = None
        try:
            dataset = self._open()
            available = _availability(dataset)
            preliminary = _check_available(plan, available)
            hours = tuple(_hours(plan))
            positions = _time_positions(dataset, hours)
            by_hour = dict(zip(hours, positions))
            _require_variables(dataset)
            lat, lon = _grid(dataset)
            lat_index, lats = _select_lat(lat, plan.area)
            lon_index, lons = _select_lon(lon, plan.area)
            eccodes = self._eccodes()
            with tempfile.TemporaryDirectory(prefix="arco-") as staging:
                for n, piece in pending:
                    if cancel.is_set():
                        raise ArcoError("ARCO ERA5 download canceled")
                    detail = (
                        "reading preliminary ERA5T hours…"
                        if preliminary
                        else "reading anonymous ARCO ERA5…"
                    )
                    self._set_progress(key, stage=f"Chunk {n + 1}/{total}: {detail}")
                    path = Path(staging) / f"chunk-{n}.grib"
                    piece_hours = tuple(_hours(piece))
                    hour_total = len(piece_hours)
                    with path.open("wb") as handle:
                        for hour_number, valid in enumerate(piece_hours, start=1):
                            if cancel.is_set():
                                raise ArcoError("ARCO ERA5 download canceled")
                            position = by_hour[valid]
                            u = _read_field(
                                dataset, U_VARIABLE, position, lat_index, lon_index
                            )
                            v = _read_field(
                                dataset, V_VARIABLE, position, lat_index, lon_index
                            )
                            _write_message(eccodes, handle, "10u", valid, lats, lons, u)
                            _write_message(eccodes, handle, "10v", valid, lats, lons, v)
                            # One ARCO hour can take minutes on a cold read, so
                            # surface the hour that just finished. Chunk
                            # done/total semantics stay unchanged.
                            self._set_progress(
                                key,
                                stage=(
                                    f"Chunk {n + 1}/{total}: ARCO wind hour "
                                    f"{hour_number}/{hour_total} · "
                                    f"{valid:%Y-%m-%d %H:%M} UTC"
                                ),
                            )
                    if cancel.is_set():
                        raise ArcoError("ARCO ERA5 download canceled")
                    if self.validator is None:
                        raise ArcoError(
                            "No GRIB validator configured; refusing to import "
                            "unverified ARCO ERA5 data"
                        )
                    provenance = self.validator(path, piece)
                    if not provenance:
                        raise ArcoError(
                            "ARCO ERA5 GRIB is missing required wind fields or "
                            "UTC times"
                        )
                    provenance = dict(provenance)
                    provenance.update(
                        {
                            "store": self.store_url,
                            "preliminary_era5t": preliminary,
                        }
                    )
                    if preliminary:
                        provenance["note"] = (
                            "includes preliminary ERA5T hours beyond the stable "
                            "ARCO stop"
                        )
                    staged.append((path, piece, provenance))
                    done += 1
                    self._set_progress(
                        key,
                        done=done,
                        stage=f"Chunk {n + 1}/{total}: downloaded and validated",
                    )
                if cancel.is_set():
                    raise ArcoError("ARCO ERA5 download canceled")
                self._set_progress(key, stage="Publishing ARCO ERA5 history…")
                self._publish_chunks(root, catalog, staged, cancel)
        finally:
            close = getattr(dataset, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    # A close failure must not mask the acquisition error.
                    pass
        self._set_progress(key, done=total, stage="Complete")
