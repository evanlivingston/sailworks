"""Bounded access to preliminary NOAA ICOADS ship observations."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from urllib.request import urlopen
import re

import numpy as np

BASE = "https://www.ncei.noaa.gov/data/international-comprehensive-ocean-atmosphere/v3/archive/nrt/daily"
_NETCDF_LOCK = Lock()


@dataclass(frozen=True)
class ShipReport:
    latitude: float
    longitude: float
    observed_at: datetime
    wind_direction: float | None = None
    wind_speed: float | None = None
    air_temperature: float | None = None
    sea_temperature: float | None = None
    pressure: float | None = None
    wave_height: float | None = None
    wave_period: float | None = None
    platform_type: int = 0

    @property
    def measurements(self):
        """Return only present observations with source-standard units."""
        values = (
            ("Wind direction", self.wind_direction, "°"),
            ("Wind speed", self.wind_speed, "m/s"),
            ("Air temperature", self.air_temperature, "°C"),
            ("Sea temperature", self.sea_temperature, "°C"),
            ("Sea-level pressure", self.pressure, "hPa"),
            ("Wave height", self.wave_height, "m"),
            ("Wave period", self.wave_period, "s"),
        )
        return tuple(
            (label, f"{value:g} {unit}")
            for label, value, unit in values
            if value is not None
        )


def _value(dataset, name, index):
    variable = dataset.variables.get(name)
    if variable is None or index >= len(variable):
        return None
    try:
        value = variable[index]
    except (IndexError, TypeError, ValueError):
        return None
    if np.ma.is_masked(value):
        return None
    result = float(value)
    return result if np.isfinite(result) and abs(result) < 1e10 else None


def _column(dataset, name, count):
    variable = dataset.variables.get(name)
    if variable is None:
        return np.full(count, np.nan)
    values = np.ma.asarray(variable[:], dtype=np.float64)
    if values.shape != (count,):
        raise ValueError(f"Unexpected ICOADS variable shape for {name}")
    values = np.asarray(values.filled(np.nan), dtype=np.float64)
    values[(~np.isfinite(values)) | (np.abs(values) >= 1e10)] = np.nan
    return values


def parse_dataset(dataset):
    """Extract ship reports, excluding inferior duplicates and invalid positions."""
    variables = dataset.variables
    date_variable = variables.get("date")
    if date_variable is None:
        raise ValueError("ICOADS file is missing the date variable")
    dates = date_variable[:]
    count = len(dates)
    platform = _column(dataset, "PT", count)
    duplicate = _column(dataset, "DUPS", count)
    latitude = _column(dataset, "lat", count)
    longitude = _column(dataset, "lon", count)
    hour = _column(dataset, "HR", count)
    fields = {
        name: _column(dataset, name, count)
        for name in ("D", "W", "AT", "SST", "SLP", "WH", "WP")
    }
    valid = (
        np.isfinite(platform)
        & (platform >= 1)
        & (platform <= 5)
        & np.isfinite(duplicate)
        & (duplicate <= 2)
        & np.isfinite(latitude)
        & (latitude >= -90)
        & (latitude <= 90)
        & np.isfinite(longitude)
        & (longitude >= -180)
        & (longitude <= 360)
        & np.isfinite(hour)
        & (hour >= 0)
        & (hour < 24)
    )
    reports = []
    for i in np.flatnonzero(valid):
        day = dates[i]
        if np.ma.is_masked(day):
            continue
        try:
            day = (
                bytes(day).decode("ascii").strip("\x00 ")
                if not isinstance(day, bytes)
                else day.decode("ascii").strip("\x00 ")
            )
        except (UnicodeDecodeError, TypeError, ValueError):
            continue
        if not re.fullmatch(r"\d{8}", day):
            continue
        try:
            moment = datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc)
            moment += timedelta(hours=float(hour[i]))
        except ValueError:
            continue

        reports.append(
            ShipReport(
                float(latitude[i]),
                (float(longitude[i]) + 180) % 360 - 180,
                moment,
                *(
                    float(fields[name][i]) if np.isfinite(fields[name][i]) else None
                    for name in ("D", "W", "AT", "SST", "SLP", "WH", "WP")
                ),
                int(platform[i]),
            )
        )
    return tuple(reports)


def fetch_recent(days=3, today=None, timeout=20, progress=None):
    """Download at most seven recent daily files; files remain independently bounded."""
    from netCDF4 import Dataset

    if not 1 <= days <= 7:
        raise ValueError("days must be between 1 and 7")
    today = today or datetime.now(timezone.utc).date()
    if not isinstance(today, date):
        raise TypeError("today must be a date")
    if not 1 <= timeout <= 120:
        raise ValueError("timeout must be between 1 and 120 seconds")
    reports = []
    for offset in range(days):
        day = date.fromordinal(today.toordinal() - offset)
        if progress is not None:
            progress(f"Checking NOAA archive for {day:%d %b} ({offset + 1}/{days})…")
        folder = f"{BASE}/{day:%Y/%m}/"
        with urlopen(folder, timeout=timeout) as response:
            listing = response.read(1_000_001).decode("utf-8", "replace")
        if len(listing) > 1_000_000:
            raise ValueError("ICOADS directory index exceeds 1 MB")
        matches = re.findall(
            r'href="(icoads3\.0\.2_daily_preliminary_d\d{8}_c\d{8}\.nc)"', listing
        )
        if not matches:
            continue
        # Never follow an arbitrary URL supplied by the directory response.
        name = next((n for n in matches if f"d{day:%Y%m%d}_" in n), None)
        if name is None:
            continue
        if progress is not None:
            progress(f"Downloading ship reports for {day:%d %b}…")
        with urlopen(folder + name, timeout=timeout) as response:
            payload = response.read(10_000_001)
        if len(payload) > 10_000_000:
            raise ValueError("ICOADS daily file exceeds 10 MB")
        # HDF5/netCDF builds used by desktop environments are not always
        # thread-safe. Multiple map hosts can refresh independently, so guard
        # in-memory opens/reads across those workers.
        with _NETCDF_LOCK:
            with Dataset("inmemory.nc", mode="r", memory=payload) as ds:
                daily_reports = parse_dataset(ds)
        reports.extend(daily_reports)
        if progress is not None:
            progress(f"Read {len(daily_reports):,} ship reports for {day:%d %b}.")
    return tuple(reports)
