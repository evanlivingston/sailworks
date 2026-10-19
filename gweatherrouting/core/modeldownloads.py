"""Choose recent model runs and install forecasts without exposing file choices."""

import json
import math
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .forecastselection import IMPORTED, forecast_source
from .grib import Grib
from .gribsources import NOAAGFSSource, NOAAHRRRSource, ECCHRDPSource
from .gribsources.options import DownloadOptions
from .marine.chs import DownloadCancelledError
from .marine.routing import FrozenRoutingWind, routing_utc
from types import SimpleNamespace

PROVIDERS = {
    "NOAA GFS": NOAAGFSSource,
    "NOAA HRRR": NOAAHRRRSource,
    "CMC HRDPS": ECCHRDPSource,
}


def newest_entries(selection, bounds, required_time=None, catalog=None):
    """Newest eligible run per selected provider, respecting its own horizon."""
    entries, issues = [], []
    for name in selection.models:
        if name == IMPORTED:
            continue
        source = PROVIDERS[name]()
        if hasattr(source, "bounds"):
            source.bounds = bounds
        try:
            options = selection.options(routing=required_time is not None)
            if required_time is not None:
                # Discover whole run horizons, then retain the requested duration
                # or extend it to bracket the actual routing query.
                options = DownloadOptions(384, options.interval, options.fields)
            rows = model_catalog(source, options, bounds, catalog)
            if required_time is not None:
                entries.append((source, routing_entry(rows, selection, required_time)))
            elif rows:
                entries.append((source, rows[0]))
            else:
                raise ValueError("No available run supports these forecast hours")
        except DownloadCancelledError:
            raise
        except Exception as exc:
            issues.append(f"{name}: {exc}")
    return entries, issues


def model_catalog(source, options, bounds, catalog):
    key = (source.name, bounds, options)
    if catalog is not None and key in catalog:
        return catalog[key]
    rows = source.get_download_list(options)
    if catalog is not None:
        catalog[key] = rows
    return rows


def routing_entry(rows, selection, instant):
    target = routing_utc(instant).timestamp()
    for row in rows:
        config = json.loads(row[4])
        run = datetime.strptime(config["date"] + config["cycle"], "%Y%m%d%H").replace(
            tzinfo=timezone.utc
        )
        hour = (target - run.timestamp()) / 3600
        if not 0 <= hour <= config["hours"]:
            continue
        interval = max(selection.interval, 1)
        end = min(
            config["hours"], max(selection.hours, math.ceil(hour / interval) * interval)
        )
        config["steps"] = [h for h in config["steps"] if h <= end]
        config["hours"] = max(config["steps"])
        return [*row[:4], json.dumps(config)]
    raise ValueError("No available run covers the required route time")


def _parse_estimate_size(text):
    """Parse an estimate like '~12.3 MB' or '800 KB' into megabytes."""
    parts = text.replace("~", "").strip().split()
    if len(parts) != 2:
        raise ValueError("Unrecognized size estimate: " + text)
    value, unit = float(parts[0]), parts[1].upper()
    if unit == "KB":
        return value / 1024
    if unit == "MB":
        return value
    if unit == "GB":
        return value * 1024
    raise ValueError("Unrecognized size estimate: " + text)


def _missing_fields(missing):
    """Weather fields among the missing downloads of one model."""
    return tuple(
        f for f in ("wind", "pressure") if any(m.startswith(f) for m in missing)
    )


def _gfs_estimate_mb(bounds, options, fields):
    """Estimated megabytes for a custom GFS download over the bounds."""
    from .gribsources.noaagfs import GFS_CUSTOM_HOURS

    source = NOAAGFSSource()
    source.bounds = bounds
    steps = options.select(GFS_CUSTOM_HOURS)
    if not steps:
        return None
    return _parse_estimate_size(source._estimate_custom_size(steps, fields))


def _hrrr_estimate_mb(bounds, options, fields):
    """Estimated megabytes for a custom HRRR download over the bounds."""
    source = NOAAHRRRSource()
    source.bounds = bounds
    cap = source._max_hours(source._get_available_cycles(1)[0])
    steps = options.select(range(cap + 1))
    if not steps:
        return None
    return _parse_estimate_size(source._estimate_custom_size(steps, fields))


def _hrdps_estimate_mb(options, fields):
    """Estimated megabytes for a custom HRDPS download."""
    available = range(0, 49, 3 if "wind" not in fields else 1)
    steps = options.select(available)
    if not steps:
        return None
    return _parse_estimate_size(ECCHRDPSource._estimate_custom_size(steps, fields))


def _model_estimate_mb(name, bounds, options, fields):
    """Estimated megabytes for one model, or None when it cannot be estimated."""
    if name == "NOAA GFS":
        return _gfs_estimate_mb(bounds, options, fields)
    if name == "NOAA HRRR":
        return _hrrr_estimate_mb(bounds, options, fields)
    if name == "CMC HRDPS":
        return _hrdps_estimate_mb(options, fields)
    return None


def _estimated_model_mb(name, bounds, selection, fields):
    """Estimated megabytes for one model; None when it cannot be estimated."""
    try:
        options = DownloadOptions(
            selection.hours, selection.interval, fields, selection.steps
        )
        return _model_estimate_mb(name, bounds, options, fields)
    except Exception:
        return None


def estimate_missing_download(models_gaps, bounds, selection):
    """Local, network-free size estimate for the missing models/fields.

    Sums per-model estimates for the current download settings over the
    visible bounds. Returns a display string like '~12.3 MB', or None
    when nothing downloadable can be estimated.
    """
    if not models_gaps or bounds is None:
        return None
    total_mb = 0.0
    estimated = False
    for name, missing in models_gaps.items():
        if name == IMPORTED or name not in PROVIDERS or not missing:
            continue
        fields = _missing_fields(missing)
        if not fields:
            continue
        size_mb = _estimated_model_mb(name, bounds, selection, fields)
        if size_mb is None:
            continue
        total_mb += size_mb
        estimated = True
    if not estimated:
        return None
    if total_mb < 1:
        return f"~{total_mb * 1024:.0f} KB"
    return f"~{total_mb:.1f} MB"


def install_download(source, row, directory, progress, cancel=None):
    """Stage and validate before atomically adding a unique library copy."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as stage:
        path = source.download(row[4], stage, progress, cancel=cancel)
        metadata = Grib.parse_metadata(path)
        name = Path(path).stem + "_" + uuid4().hex[:8] + Path(path).suffix
        destination = directory / name
        if cancel is not None and cancel.is_set():
            raise DownloadCancelledError()
        os.replace(path, destination)
    return Grib(
        str(destination),
        name,
        metadata.centre,
        metadata.bounds,
        metadata.start_time,
        metadata.last_forecast,
        metadata.model_name,
    )


class ModelForecastRecovery:
    """Fill selected-model gaps; never fetch or substitute an unchecked model."""

    def __init__(self, selection, directory, cancel, report=lambda _: None):
        self.selection = selection
        self.directory = Path(directory).resolve()
        self.cancel = cancel
        self.report = report
        self.attempted = set()
        self.issues = []
        self.catalog = {}

    def check(self):
        if self.cancel.is_set():
            raise DownloadCancelledError()

    def cached(self, gribs):
        known = {str(Path(g.path).resolve()) for g in gribs if hasattr(g, "path")}
        opened = []
        for path in sorted(self.directory.glob("*")):
            self.check()
            if str(path) in known or path.suffix.lower() not in (
                ".grib",
                ".grb",
                ".grib2",
                ".grb2",
            ):
                continue
            try:
                metadata = Grib.parse_metadata(str(path))
                if forecast_source(metadata) in self.selection.models:
                    opened.append(Grib.parse(str(path)))
            except DownloadCancelledError:
                raise
            except Exception:
                continue
        return opened

    def download(self, gribs, points, instant, area):
        result = []
        for name in self.selection.models:
            self.check()
            candidates = [g for g in gribs if forecast_source(g) == name]
            probe = FrozenRoutingWind(SimpleNamespace(gribs=candidates))
            if candidates and all(
                probe.get_wind_at(instant, *point) is not None for point in points
            ):
                continue
            if name == IMPORTED:
                self.issues.append(
                    "Imported forecasts: missing route coverage; import a covering forecast"
                )
                continue
            parts = area.parts() if name in ("NOAA GFS", "NOAA HRRR") else (area,)
            for part in parts:
                self.check()
                bounds = (part.south, part.west, part.north, part.east)
                selection = replace(self.selection, models=(name,))
                entries, issues = newest_entries(
                    selection, bounds, instant, self.catalog
                )
                self.issues.extend(issues)
                for source, row in entries:
                    key = (name, bounds, row[4])
                    if key in self.attempted:
                        continue
                    self.attempted.add(key)
                    self.report(f"Downloading {name} for routing…")
                    try:
                        result.append(
                            install_download(
                                source,
                                row,
                                self.directory,
                                lambda _: self.check(),
                                self.cancel,
                            )
                        )
                    except DownloadCancelledError:
                        raise
                    except Exception as exc:
                        self.issues.append(f"{name}: {exc}")
        return result
