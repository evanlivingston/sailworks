"""Bounded wind forecast recovery and ahead-of-search downloads for routing."""

import hashlib
import json
import math
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsources.noaagfs import GFS_FORECAST_HOURS, NOAAGFSSource
from gweatherrouting.core.storage import GRIB_DIR
from .batchwind import ArrayWind
from .chs import DownloadCancelledError
from .fields import Area
from .routing import DataGap, FrozenRoutingWind, routing_utc, wind_run_timestamp


class WindCoverageRecovery:
    def __init__(
        self,
        routing,
        save=None,
        directory=GRIB_DIR,
        source_factory=NOAAGFSSource,
        selection=None,
    ):
        self.routing = routing
        self.environment = routing.current_environment
        self.wind = self.environment.wind
        self.save = save
        self.directory = Path(directory)
        self.source_factory = source_factory
        self._stop = threading.Event()
        self._pending = None
        self._attempted: set = set()
        self._errors: dict = {}
        self.model_recovery = None
        if selection is not None:
            from ..modeldownloads import ModelForecastRecovery

            self.model_recovery = ModelForecastRecovery(selection, directory, self)

    def is_set(self):
        return self._stop.is_set() or self.environment.cancel.is_set()

    def close(self):
        self._stop.set()

    def _check(self):
        if self.is_set():
            raise DownloadCancelledError()

    def missing(self, gaps, wind=None):
        """Check coverage without adding speculative queries to run provenance."""
        if not gaps:
            return []
        wind = wind or self.wind
        used, last = set(wind.used), wind.last_source
        try:
            positions = np.array([g.position for g in gaps], dtype=float)
            times = np.array(
                [round(routing_utc(g.time).timestamp() * 1e6) for g in gaps],
                dtype=np.int64,
            )
            values, _ = ArrayWind(wind, self).query_us(positions, times)
            return [g for g, ok in zip(gaps, np.isfinite(values).all(axis=1)) if not ok]
        finally:
            wind.used.clear()
            wind.used.update(used)
            wind.last_source = last

    def prefetch(self):
        """Screen the route's next day; download on a separate I/O worker."""
        if self._pending is not None:
            return
        points = [self.routing.position, *self.routing.track[self.routing.wp :]]
        start = routing_utc(self.routing.time)
        gaps = [
            DataGap(tuple(p), start + timedelta(hours=h), "wind", "no_coverage")
            for h in range(25)
            for p in points
        ]
        missing = self.missing(gaps)
        if not missing:
            return
        job: dict = dict(result=None, error=None)

        def run():
            try:
                job["result"] = self.download(missing)
            except Exception as error:
                job["error"] = error

        thread = threading.Thread(target=run, daemon=True)
        self._pending = (thread, job)
        thread.start()

    def ready(self, wait=False):
        if self._pending is None:
            return
        thread, job = self._pending
        if wait:
            while thread.is_alive():
                self._check()
                thread.join(0.1)
        if thread.is_alive():
            return
        self._pending = None
        self._check()
        if job["result"]:
            self.install(job["result"])
        # Speculative failures only matter if the actual search needs that data.

    def install(self, gribs):
        self._check()
        if self.wind.add_gribs(gribs) and self.save is not None:
            self.save(gribs)

    def recover(self, gaps):
        self.ready(wait=True)
        gaps = self.missing(gaps)
        if not gaps:
            return
        self.install(self.download(gaps))
        if self.missing(gaps):
            raise ValueError(
                "Downloaded wind forecast does not cover the required route area and time."
            )

    def refresh(self):
        """Use the latest eligible GFS cycle before starting the search."""
        self._check()
        if self.model_recovery is not None:
            return
        if self.wind.gribs and not all(
            g.name.startswith("gfs_") for g in self.wind.gribs
        ):
            return
        start = routing_utc(self.routing.time)
        cycles = [
            c for c in self.source_factory()._get_available_cycles() if c <= start
        ]
        if not cycles:
            return  # Historical routes can still use their saved forecasts.
        cycle = max(cycles)
        latest = FrozenRoutingWind(
            SimpleNamespace(
                gribs=[
                    g
                    for g in self.wind.gribs
                    if wind_run_timestamp(g) >= cycle.timestamp()
                ]
            )
        )
        points = [self.routing.position, *self.routing.track[self.routing.wp :]]
        gaps = self.missing(
            [
                DataGap(tuple(p), start + timedelta(hours=h), "wind", "no_coverage")
                for h in range(25)
                for p in points
            ],
            latest,
        )
        if gaps:
            self.install(self.download(gaps, minimum_cycle=cycle))

    def _cached_forecast(self, gaps, minimum_cycle=None):
        best = None
        for path in sorted(self.directory.glob("gfs_*_*z_*h*.grib2")):
            self._check()
            try:
                cached = Grib.parse(str(path))
                stamp = wind_run_timestamp(cached)
                if minimum_cycle is not None and stamp < minimum_cycle.timestamp():
                    continue
                if best is not None and stamp <= wind_run_timestamp(best):
                    continue
                probe = FrozenRoutingWind(SimpleNamespace(gribs=[cached]))
                if not self.missing(gaps, probe):
                    best = cached
            except DownloadCancelledError:
                raise
            except Exception:
                continue
        return best

    def download(self, gaps, minimum_cycle=None):
        self._check()
        if self.model_recovery is not None:
            points = [
                self.routing.position,
                *self.routing.track[self.routing.wp :],
                *(g.position for g in gaps),
            ]
            area = Area.from_points(points, padding=1)
            instant = max(g.time for g in gaps)
            gribs = [getattr(g, "grib", g) for g in self.wind.gribs]
            return self.model_recovery.download(gribs, points, instant, area)
        if self.wind.gribs and not all(
            g.name.startswith("gfs_") for g in self.wind.gribs
        ):
            raise ValueError(
                "Automatic wind downloads support NOAA GFS. Load a covering forecast "
                "from your selected wind model; it will not be replaced with GFS."
            )
        start = min(routing_utc(g.time) for g in gaps)
        end = max(routing_utc(g.time) for g in gaps) + timedelta(hours=24)
        source = self.source_factory()
        cycles = [c for c in source._get_available_cycles() if c <= start]
        cycle = max(cycles) if cycles else None
        cached = self._cached_forecast(gaps, minimum_cycle or cycle)
        if cached is not None:
            return [cached]
        if cycle is None:
            raise ValueError(
                "No GFS run covers this departure. "
                "Load an archived forecast or choose a later departure."
            )
        first = math.floor((start - cycle).total_seconds() / 3600)
        last = math.ceil((end - cycle).total_seconds() / 3600)
        lower = max((h for h in GFS_FORECAST_HOURS if h <= first), default=None)
        upper = min((h for h in GFS_FORECAST_HOURS if h >= last), default=None)
        if lower is None or upper is None:
            raise ValueError(
                "The required route time exceeds the available GFS forecast horizon."
            )
        points = [
            self.routing.position,
            *self.routing.track[self.routing.wp :],
            *(g.position for g in gaps),
        ]
        area = Area.from_points(points, padding=1)
        result = []
        for part in area.parts():
            self._check()
            source.bounds = (part.south, part.west, part.north, part.east)
            config = dict(
                date=cycle.strftime("%Y%m%d"),
                cycle=f"{cycle.hour:02d}",
                start_hour=lower,
                hours=upper,
            )
            key = hashlib.sha256(
                json.dumps([config, source.bounds], sort_keys=True).encode()
            ).hexdigest()[:20]
            config["cache_key"] = key
            if key in self._attempted:
                raise ValueError(
                    self._errors.get(
                        key,
                        "Wind download did not fill the gap; no repeated download was started.",
                    )
                )
            self._attempted.add(key)
            try:
                path = source.download(
                    json.dumps(config),
                    str(self.directory),
                    lambda percent: self._check(),
                    cancel=self,
                )
                self._check()
                result.append(Grib.parse(path))
            except Exception as error:
                self._errors[key] = str(error)
                raise
        return result
