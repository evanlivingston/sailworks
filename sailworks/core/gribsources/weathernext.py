"""WeatherNext 3 Zarr source: bounded slices written as validated GRIB2."""

from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, List

from sailworks.core.gribsources.base import GribSource
from sailworks.core.gribsources.options import DEFAULT_FIELDS
from sailworks.core.forecastcapabilities import MODEL_CAPABILITIES
from sailworks.core.weathernext import (
    BASE_URL,
    U_WIND,
    V_WIND,
    open_statistics_store,
    statistics_store_url,
)
from sailworks.core.weathernextcredentials import WeatherNextCredentials

PRESSURE = "mean_sea_level_pressure_mean"
_RUN = re.compile(r"^(\d{8})_(\d{2})hr_(\d{2})_preds$")
# Listing the bucket prefix costs ~1 minute (7360 entries), so share one
# catalogue per process and keep it for the whole session.
_CATALOGUE_TTL = 1800.0
_CATALOGUE = ()
_CATALOGUE_AT = 0.0


def _expanded(bounds):
    """Expand to 0.1-degree grid points so the written GRIB covers the request."""
    south, west, north, east = bounds
    return (
        math.floor(south * 10) / 10,
        math.floor(west * 10) / 10,
        math.ceil(north * 10) / 10,
        math.ceil(east * 10) / 10,
    )


class WeatherNextSource(GribSource):
    """Read a bounded WeatherNext Zarr run and emit a GRIB2 artifact.

    WeatherNext publishes Zarr, while the application's refresh, validation and
    viewport path consumes GRIB. This source performs the smallest bounded
    conversion needed: selected wind/pressure slices, selected steps, one file.
    """

    download_fields = ("wind", "pressure")

    def __init__(self, runs=None):
        self._bounds = None
        self._runs = runs

    @property
    def name(self) -> str:
        return "Google WeatherNext 3"

    @property
    def bounds(self):
        return self._bounds

    @bounds.setter
    def bounds(self, value):
        self._bounds = value

    def _catalogue_runs(self):
        global _CATALOGUE, _CATALOGUE_AT
        if self._runs is not None:
            return list(self._runs)
        now = time.monotonic()
        if _CATALOGUE and now - _CATALOGUE_AT < _CATALOGUE_TTL:
            self._runs = _CATALOGUE
            return list(self._runs)
        filesystem = WeatherNextCredentials().gcs_filesystem()
        entries = filesystem.ls(BASE_URL.rstrip("/"), detail=False)
        names = sorted(entry.rstrip("/").rsplit("/", 1)[-1] for entry in entries)
        candidates = []
        for name in names[-96:]:
            match = _RUN.fullmatch(name)
            if match is not None:
                candidates.append((name, match.groups()))
        stores = {
            name: f"{BASE_URL}{name}/predictions.zarr" for name, _groups in candidates
        }

        def complete(name):
            store = stores[name]
            return filesystem.exists(f"{store}/zarr.json") or filesystem.exists(
                f"{store}/.zgroup"
            )

        # ~190 HEAD requests: run them concurrently or the first plan stalls a minute.
        with ThreadPoolExecutor(max_workers=16) as pool:
            ready = {
                name
                for name, complete in zip(stores, pool.map(complete, stores))
                if complete
            }

        runs = []
        for name, (date, cycle, tag) in candidates:
            if name not in ready:
                continue
            runs.append(
                (
                    date,
                    cycle,
                    tag + "_preds",
                    datetime.strptime(date + cycle, "%Y%m%d%H").replace(
                        tzinfo=timezone.utc
                    ),
                )
            )
        runs = sorted(runs, key=lambda item: item[3])
        _CATALOGUE, _CATALOGUE_AT = tuple(runs), now
        self._runs = _CATALOGUE
        return list(runs)

    def estimate_download_size(self, identifier, *, cancel=None):
        """Estimate the GRIB2 payload from the region grid and requested steps."""
        payload = json.loads(identifier)
        steps = tuple(payload.get("steps") or ())
        fields = tuple(payload.get("fields") or ())
        if not steps or not fields or self.bounds is None:
            raise ValueError("WeatherNext estimate needs steps, fields and a region")
        # The store chunks one full global field per lead (1801x3600 float32),
        # so even a tiny region transfers ~26 MB for every message.
        global_points = 1801 * 3600
        messages = len(steps) * (2 if "wind" in fields else 0)
        messages += len(steps) if "pressure" in fields else 0
        return int(messages * (global_points * 4 + 512)), True

    def get_download_list(self, options=None) -> List:
        options = (
            options
            or type(
                "Options",
                (),
                {
                    "hours": 120,
                    "interval": 3,
                    "fields": DEFAULT_FIELDS,
                    "steps": (),
                },
            )()
        )
        fields = tuple(
            field for field in options.fields if field in self.download_fields
        )
        if not fields:
            return []
        rows = []
        for date, cycle, tag, run_time in self._catalogue_runs()[-24:]:
            available = MODEL_CAPABILITIES["WEATHERNEXT"].available_steps(
                run_time.hour, "wind"
            )
            steps = options.select(available)
            if not steps:
                continue
            count = len(steps) * (2 if "wind" in fields else 0)
            count += len(steps) if "pressure" in fields else 0
            coverage = f"{min(steps)}–{max(steps)} h · {len(steps)} times"
            payload = {
                "source": self.name,
                "date": date,
                "cycle": cycle,
                "hours": max(steps),
                "steps": list(steps),
                "fields": list(fields),
                "run_tag": tag,
            }
            rows.append(
                [
                    f"WN3 · {coverage}",
                    self.name,
                    f"~{count * 0.4:.1f} MB",
                    run_time.strftime("%Y-%m-%d %H:%M UTC"),
                    json.dumps(payload),
                ]
            )
        return rows

    def _select(self, dataset, variable, run_time, step, latitude, longitude):
        import numpy as np

        south, west, north, east = _expanded(
            (latitude[0], longitude[0], latitude[1], longitude[1])
        )
        if west > east:
            raise ValueError(
                "WeatherNext does not support a region crossing the prime meridian"
            )
        lat = dataset.coords["lat_0p1"].values
        lat_slice = slice(south, north) if lat[0] <= lat[-1] else slice(north, south)
        west360, east360 = west % 360, east % 360
        selected = dataset[[variable]].sel(
            lead_time=np.timedelta64(step, "h"),
            lat_0p1=lat_slice,
            lon_0p1=slice(west360, east360),
        )
        if any(selected.sizes.get(dim, 0) == 0 for dim in ("lat_0p1", "lon_0p1")):
            raise ValueError("WeatherNext selection has no grid points")
        return selected[variable].values

    def _select_many(self, dataset, variable, steps, latitude, longitude, on_done=None):
        """Read leads concurrently: each lead is one 26 MB remote chunk."""
        workers = min(8, max(1, len(steps)))

        def one(step):
            value = self._select(dataset, variable, None, step, latitude, longitude)
            if on_done is not None:
                on_done()
            return step, value

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(pool.map(one, steps))

    def download(
        self,
        identifier: str,
        dest_dir: str,
        percentage_callback: Callable[[int], None],
        cancel=None,
    ) -> str:
        import eccodes
        import numpy as np
        from pathlib import Path

        payload = json.loads(identifier)
        run_time = datetime.strptime(
            payload["date"] + payload["cycle"], "%Y%m%d%H"
        ).replace(tzinfo=timezone.utc)
        steps = tuple(payload["steps"])
        fields = tuple(payload["fields"])
        if self.bounds is None:
            raise ValueError("WeatherNext requires a requested region")
        south, west, north, east = _expanded(self.bounds)
        url = statistics_store_url(run_time, payload.get("run_tag", "01_preds"))
        dataset = open_statistics_store(url, credentials=WeatherNextCredentials().token)
        lat = dataset.coords["lat_0p1"].values
        lat_slice = slice(south, north) if lat[0] <= lat[-1] else slice(north, south)
        selected_lat = dataset.coords["lat_0p1"].sel(lat_0p1=lat_slice).values
        west360, east360 = west % 360, east % 360
        selected_lon = (
            dataset.coords["lon_0p1"].sel(lon_0p1=slice(west360, east360)).values
        )
        if len(selected_lat) < 2 or len(selected_lon) < 2:
            raise ValueError("WeatherNext selection is smaller than a GRIB grid")

        wind_u = wind_v = pressure = {}
        total_reads = len(steps) * (2 if "wind" in fields else 0)
        total_reads += len(steps) if "pressure" in fields else 0
        read_done = 0

        def read_progress():
            # Reads are ~90% of the wall time; move the bar while they land.
            nonlocal read_done
            read_done += 1
            percentage_callback(min(90, round(90 * read_done / max(1, total_reads))))

        if "wind" in fields:
            wind_u = self._select_many(
                dataset, U_WIND, steps, (south, north), (west, east), read_progress
            )
            wind_v = self._select_many(
                dataset, V_WIND, steps, (south, north), (west, east), read_progress
            )
        if "pressure" in fields:
            pressure = self._select_many(
                dataset, PRESSURE, steps, (south, north), (west, east), read_progress
            )

        # Published runs can still be uploading, leaving some leads blank.
        # Drop them so the artifact never carries an empty frame.
        usable = tuple(
            step
            for step in steps
            if any(
                np.isfinite(arrays[step]).any()
                for arrays in (wind_u, wind_v, pressure)
                if arrays
            )
        )
        if not usable:
            raise ValueError(
                "WeatherNext returned no data for this region and run; "
                "the run may still be uploading, try another run"
            )
        steps = usable

        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        path = Path(dest_dir) / (
            f"weathernext_{run_time:%Y%m%d_%H}_{min(steps)}_{max(steps)}.grib2"
        )
        total = len(steps) * (2 if "wind" in fields else 0)
        total += len(steps) if "pressure" in fields else 0
        done = 0
        written_any = False

        def write_message(short_name, level_type, level, values):
            nonlocal done, written_any
            gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            keys = {
                "shortName": short_name,
                "typeOfLevel": level_type,
                "level": level,
                "dataDate": int(run_time.strftime("%Y%m%d")),
                "dataTime": int(run_time.strftime("%H%M")),
                "forecastTime": int(step),
                "stepRange": str(step),
                "startStep": int(step),
                "endStep": int(step),
                "gridType": "regular_ll",
                "Ni": len(selected_lon),
                "Nj": len(selected_lat),
                "latitudeOfFirstGridPointInDegrees": float(selected_lat[0]),
                "longitudeOfFirstGridPointInDegrees": float(selected_lon[0]),
                "latitudeOfLastGridPointInDegrees": float(selected_lat[-1]),
                "longitudeOfLastGridPointInDegrees": float(selected_lon[-1]),
                "iDirectionIncrementInDegrees": float(
                    abs(selected_lon[1] - selected_lon[0])
                ),
                "jDirectionIncrementInDegrees": float(
                    abs(selected_lat[1] - selected_lat[0])
                ),
                "jScansPositively": 1 if selected_lat[-1] > selected_lat[0] else 0,
                "centre": 250,
                "subCentre": 0,
                "missingValue": 9999.0,
                "bitmapPresent": 1,
            }
            for key, value in keys.items():
                eccodes.codes_set(gid, key, value)
            encoded = np.asarray(values, dtype=float).ravel()
            if np.isfinite(encoded).any():
                written_any = True
            encoded[~np.isfinite(encoded)] = 9999.0
            eccodes.codes_set_values(gid, encoded)
            eccodes.codes_write(gid, handle)
            eccodes.codes_release(gid)
            done += 1
            percentage_callback(90 + min(10, round(10 * done / max(1, total))))

        with path.open("wb") as handle:
            for step in steps:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("WeatherNext download cancelled")
                if "wind" in fields:
                    write_message("10u", "heightAboveGround", 10, wind_u[step])
                    write_message("10v", "heightAboveGround", 10, wind_v[step])
                if "pressure" in fields:
                    write_message("prmsl", "meanSea", 0, pressure[step])
        if done and not written_any:
            path.unlink(missing_ok=True)
            raise ValueError(
                "WeatherNext returned no data for this region and run; "
                "the run may still be uploading, try another run"
            )
        return str(path)
