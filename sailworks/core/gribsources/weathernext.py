"""WeatherNext 3 Zarr source: bounded slices written as validated GRIB2."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Callable, List

from sailworks.core.gribsources.base import GribSource
from sailworks.core.gribsources.options import DEFAULT_FIELDS
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
        if self._runs is not None:
            return list(self._runs)
        filesystem = WeatherNextCredentials().gcs_filesystem()
        entries = filesystem.ls(BASE_URL.rstrip("/"), detail=False)
        runs = []
        for entry in entries:
            match = _RUN.fullmatch(entry.rstrip("/").rsplit("/", 1)[-1])
            if match is None:
                continue
            date, cycle, tag = match.groups()
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
        return sorted(runs, key=lambda item: item[3])

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
        available = tuple(range(361))
        fields = tuple(
            field for field in options.fields if field in self.download_fields
        )
        if not fields:
            return []
        rows = []
        for date, cycle, tag, run_time in self._catalogue_runs()[-24:]:
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
                    f"WeatherNext 3 · {coverage}",
                    self.name,
                    f"~{count * 0.4:.1f} MB",
                    run_time.strftime("%Y-%m-%d %H:%M UTC"),
                    json.dumps(payload),
                ]
            )
        return rows

    def _select(self, dataset, variable, run_time, step, latitude, longitude):
        import numpy as np

        south, west, north, east = latitude[0], longitude[0], latitude[1], longitude[1]
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

    def download(
        self,
        identifier: str,
        dest_dir: str,
        percentage_callback: Callable[[int], None],
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
        south, west, north, east = self.bounds
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

        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        path = Path(dest_dir) / (
            f"weathernext_{run_time:%Y%m%d_%H}_{min(steps)}_{max(steps)}.grib2"
        )
        total = len(steps) * (2 if "wind" in fields else 0)
        total += len(steps) if "pressure" in fields else 0
        done = 0

        def write_message(short_name, level_type, level, values):
            nonlocal done
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
            }
            for key, value in keys.items():
                eccodes.codes_set(gid, key, value)
            eccodes.codes_set_values(gid, np.asarray(values, dtype=float).ravel())
            eccodes.codes_write(gid, handle)
            eccodes.codes_release(gid)
            done += 1
            percentage_callback(min(100, round(100 * done / max(1, total))))

        with path.open("wb") as handle:
            for step in steps:
                if "wind" in fields:
                    write_message(
                        "10u",
                        "heightAboveGround",
                        10,
                        self._select(
                            dataset,
                            U_WIND,
                            run_time,
                            step,
                            (south, north),
                            (west, east),
                        ),
                    )
                    write_message(
                        "10v",
                        "heightAboveGround",
                        10,
                        self._select(
                            dataset,
                            V_WIND,
                            run_time,
                            step,
                            (south, north),
                            (west, east),
                        ),
                    )
                if "pressure" in fields:
                    write_message(
                        "prmsl",
                        "meanSea",
                        0,
                        self._select(
                            dataset,
                            PRESSURE,
                            run_time,
                            step,
                            (south, north),
                            (west, east),
                        ),
                    )
        return str(path)
