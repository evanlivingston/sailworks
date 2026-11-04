"""NOAA NCEI Blended Seawinds (satellite-blended) historical wind import."""

from __future__ import annotations

import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .forecasttranche import (
    AnalysisSelection,
    Artifact,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)

BASE_URL = (
    "https://coastwatch.noaa.gov/data/pub0015/coastwatch/blended/wind/"
    "science/uvcomp/6hr"
)
MODEL_ID = "NBS"
INTERVAL_HOURS = 6
LEVEL = "10-HTGL"
RESOLUTION = "0.25"
NBS_EARLIEST = date(1987, 7, 1)
LATENCY = timedelta(days=2)
_MISSING = 9999.0


class NbsError(RuntimeError):
    """An actionable NBS acquisition or validation error."""


@dataclass(frozen=True)
class NbsPlan:
    start: datetime
    end: datetime
    area: tuple[float, float, float, float]
    days: tuple[date, ...]

    @property
    def times(self):
        cursor = self.start
        times = []
        while cursor <= self.end:
            times.append(cursor)
            cursor += timedelta(hours=INTERVAL_HOURS)
        return tuple(times)


def plan_request(start, end, area, *, now=None):
    """Plan a 6-hourly NBS request over an inclusive UTC time range."""

    def utc(value):
        if not isinstance(value, datetime):
            raise ValueError("start and end must be datetimes")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("start and end must be timezone-aware")
        return value.astimezone(timezone.utc)

    start, end = utc(start), utc(end)
    if start > end:
        raise ValueError("start must not be after end")
    if start.date() < NBS_EARLIEST:
        raise ValueError("NBS coverage starts on 1987-07-01")
    clock = (
        now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    )
    if end > clock - LATENCY:
        raise ValueError("NBS data is not available until about two days later")

    # NBS times are 00/06/12/18 UTC; snap inwards so every planned hour exists.
    snapped_start = start.replace(minute=0, second=0, microsecond=0)
    while snapped_start.hour % INTERVAL_HOURS:
        snapped_start += timedelta(hours=1)
    snapped_end = end.replace(minute=0, second=0, microsecond=0)
    while snapped_end.hour % INTERVAL_HOURS:
        snapped_end -= timedelta(hours=1)
    if snapped_start > snapped_end:
        raise ValueError("requested range contains no NBS 6-hourly times")

    north, west, south, east = (float(value) for value in area)
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError("area must be north, west, south, east bounds")

    days = []
    cursor = snapped_start.date()
    while cursor <= snapped_end.date():
        days.append(cursor)
        cursor += timedelta(days=1)
    return NbsPlan(snapped_start, snapped_end, (north, west, south, east), tuple(days))


def day_url(day: date, *, nrt: bool = False):
    if nrt:
        return (
            "https://coastwatch.noaa.gov/data/pub0015/coastwatch/blended/wind/"
            f"nrt/uvcomp/6hr/NBSv02_wind_6hourly_{day:%Y%m%d}_nrt.nc"
        )
    return f"{BASE_URL}/{day:%Y}/NBSv02_wind_6hourly_{day:%Y%m%d}.nc"


def download_day(day: date, directory, *, timeout=120):
    """Download one daily NBS file, preferring the delayed-mode archive.

    Delayed-mode (science) files lag about a month, so recent dates only exist
    in the near-real-time tree; fall back to it when the archive lacks the day.
    """
    import requests

    path = Path(directory) / f"NBSv02_wind_6hourly_{day:%Y%m%d}.nc"
    if path.is_file() and path.stat().st_size > 0:
        return path
    nrt_path = Path(directory) / f"NBSv02_wind_6hourly_{day:%Y%m%d}_nrt.nc"
    if nrt_path.is_file() and nrt_path.stat().st_size > 0:
        return nrt_path
    errors = []
    try:
        response = requests.get(day_url(day), timeout=timeout)
        if response.status_code != 404:
            response.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.content)
            return path
        errors.append("archive file not published yet")
    except Exception as exc:  # network and HTTP failures
        errors.append(str(exc))
    try:
        response = requests.get(day_url(day, nrt=True), timeout=timeout)
        response.raise_for_status()
    except Exception as exc:
        errors.append(str(exc))
        raise NbsError(f"NBS download failed for {day}: {'; '.join(errors)}") from None
    nrt_path.parent.mkdir(parents=True, exist_ok=True)
    nrt_path.write_bytes(response.content)
    return nrt_path


def _open_netcdf(path):
    try:
        import netCDF4
    except ImportError:
        raise NbsError(
            "Reading NBS files requires the netCDF4 Python package"
        ) from None
    return netCDF4.Dataset(path)


def _times(variable):
    import netCDF4

    units = variable.units
    result = []
    for value in variable[:]:
        stamp = netCDF4.num2date(
            float(value),
            units,
            only_use_cftime_datetimes=False,
            only_use_python_datetimes=True,
        )
        result.append(stamp.replace(tzinfo=timezone.utc))
    return tuple(result)


def _select(plan, lat, lon):
    lo = plan.area[1] % 360
    hi = plan.area[3] % 360
    if lo > hi:
        raise ValueError("NBS area must not wrap the dateline")
    lat_mask = (lat >= plan.area[2]) & (lat <= plan.area[0])
    lon_mask = (lon >= lo) & (lon <= hi)
    lat_index = np.where(lat_mask)[0]
    lon_index = np.where(lon_mask)[0]
    if len(lat_index) < 2 or len(lon_index) < 2:
        raise NbsError("NBS selection is smaller than a GRIB grid")
    return lat_index, lon_index


def encode_grib(plan, paths, destination):
    """Encode u/v 10 m wind from daily NBS NetCDF files into one GRIB2 file."""
    try:
        import eccodes
    except ImportError:
        raise NbsError(
            "Satellite winds need the eccodes package; install it and retry"
        ) from None

    written = []
    with destination.open("wb") as handle:
        for path in paths:
            dataset = _open_netcdf(path)
            try:
                lat = np.asarray(dataset.variables["lat"][:], dtype=float)
                lon = np.asarray(dataset.variables["lon"][:], dtype=float)
                lat_index, lon_index = _select(plan, lat, lon)
                selected_lat = lat[lat_index]
                selected_lon = lon[lon_index]
                times = _times(dataset.variables["time"])
                wanted = set(plan.times)
                for index, valid in enumerate(times):
                    if valid not in wanted:
                        continue
                    for short_name, source in (
                        ("10u", "u_wind"),
                        ("10v", "v_wind"),
                    ):
                        variable = dataset.variables[source]
                        if variable.ndim == 4:
                            values = variable[index, 0, :, :]
                        else:
                            values = variable[index, :, :]
                        values = np.ma.filled(np.asarray(values, dtype=float), np.nan)[
                            lat_index
                        ][:, lon_index]
                        _write_message(
                            eccodes,
                            handle,
                            short_name,
                            valid,
                            selected_lat,
                            selected_lon,
                            values,
                        )
                        written.append((valid, short_name))
            finally:
                dataset.close()
    expected = {(valid, short) for valid in plan.times for short in ("10u", "10v")}
    if set(written) != expected:
        raise NbsError("NBS files did not contain every requested 6-hourly wind field")
    return destination


def _write_message(eccodes, handle, short_name, valid, lat, lon, values):
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
            "Ni": len(lon),
            "Nj": len(lat),
            "latitudeOfFirstGridPointInDegrees": float(lat[0]),
            "longitudeOfFirstGridPointInDegrees": float(lon[0]),
            "latitudeOfLastGridPointInDegrees": float(lat[-1]),
            "longitudeOfLastGridPointInDegrees": float(lon[-1]),
            "iDirectionIncrementInDegrees": float(abs(lon[1] - lon[0])),
            "jDirectionIncrementInDegrees": float(abs(lat[1] - lat[0])),
            "iScansNegatively": 0,
            "jScansPositively": 1 if lat[-1] > lat[0] else 0,
            "centre": 7,
            "subCentre": 0,
            # Local-use process id so the display identity is NBS, not Unknown.
            "generatingProcessIdentifier": 200,
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


def validate_grib(path, plan):
    """Return field time coverage when the GRIB carries the planned wind."""
    from osgeo import gdal

    dataset = gdal.Open(str(path))
    if dataset is None:
        raise NbsError("NBS GRIB could not be opened for validation")
    found = set()
    valid_cells = 0
    for index in range(1, dataset.RasterCount + 1):
        metadata = dataset.GetRasterBand(index).GetMetadata()
        element = metadata.get("GRIB_ELEMENT")
        if element not in ("UGRD", "VGRD"):
            continue
        if metadata.get("GRIB_SHORT_NAME") != LEVEL:
            raise NbsError("NBS GRIB wind is not at 10 m")
        array = dataset.GetRasterBand(index).ReadAsArray()
        if array is not None:
            valid_cells += int(((array < 9000) & (array > -9000)).sum())
        seconds = float(metadata.get("GRIB_VALID_TIME", 0))
        valid = datetime.fromtimestamp(seconds, tz=timezone.utc)
        found.add((valid, "10u" if element == "UGRD" else "10v"))
    expected = {(valid, short) for valid in plan.times for short in ("10u", "10v")}
    if found != expected:
        raise NbsError("NBS GRIB is missing planned wind times")
    if not valid_cells:
        raise NbsError("NBS GRIB contains no valid wind values")
    return {"source": "NOAA/NCEI Blended Seawinds v2", "times": plan.times}


def build(state, plan, *, directory=None, progress=None, cancel=None):
    """Download, encode and validate NBS wind, returning a GRIB artifact."""
    cancel = cancel or threading.Event()
    if directory is None:
        working = Path(state.store.root) / "nbs-cache"
        working.mkdir(parents=True, exist_ok=True)
    else:
        working = Path(directory)
    paths = []
    for index, day in enumerate(plan.days):
        if cancel.is_set():
            raise NbsError("NBS download canceled")
        if progress is not None:
            progress(
                round(10 * index / max(1, len(plan.days))),
                f"Downloading NBS {day}…",
            )
        paths.append(download_day(day, working))
    if progress is not None:
        progress(60, "Encoding NBS wind…")
    destination = working / "nbs.grib2"
    encode_grib(plan, paths, destination)
    if progress is not None:
        progress(85, "Validating NBS wind…")
    validate_grib(destination, plan)
    artifact = state.store.register_file(destination)
    if progress is not None:
        progress(95, "Publishing NBS wind…")
    tranche = publish(state, plan, artifact)
    if progress is not None:
        progress(100, "Complete")
    return tranche


def publish(state, plan, artifact):
    """Publish validated NBS wind as an activatable analysis tranche."""
    times = plan.times
    if len(times) < 2:
        raise NbsError("NBS history needs at least two times to display")
    north, west, south, east = plan.area
    region = (south, west, north, east)
    hours = int((times[-1] - times[0]).total_seconds() // 3600)
    steps = tuple(range(0, hours + 1, INTERVAL_HOURS))
    config = ModelConfiguration(
        MODEL_ID,
        ("wind",),
        region,
        RESOLUTION,
        hours,
        steps,
        INTERVAL_HOURS,
    )
    now = datetime.now(timezone.utc)
    entry = ModelEntry(
        MODEL_ID,
        MODEL_ID,
        times[0],
        "deterministic",
        ("wind",),
        (LEVEL,),
        region,
        RESOLUTION,
        times,
        (Artifact(artifact.path, artifact.sha256, artifact.size),),
        now,
        field_times=(("wind", times),),
    )
    tranche = ForecastTranche(
        f"nbs-{artifact.sha256[:16]}-v1", now, (config,), (entry,)
    )
    state.store.publish(tranche)
    state.store.activate(AnalysisSelection(tranche.tranche_id, entry.entry_id))
    return tranche
