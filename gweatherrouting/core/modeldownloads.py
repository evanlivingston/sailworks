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
