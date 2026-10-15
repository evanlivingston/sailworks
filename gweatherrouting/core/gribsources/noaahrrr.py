# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Callable, List

import requests

from gweatherrouting.core.gribsources.base import GribSource

logger = logging.getLogger("gweatherrouting")

# NOAA NOMADS HRRR 3km filter endpoint (CONUS domain)
HRRR_FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl"
HRRR_SUBDIR = "conus"

# HRRR runs hourly; extended (48h) runs at 00/06/12/18, others reach 18h.
HRRR_LONG_CYCLES = ("00", "06", "12", "18")
HRRR_SHORT_RANGE = 18
HRRR_LONG_RANGE = 48

# Download presets (in hours)
FORECAST_PRESETS = {
    "12 hours": 12,
    "24 hours": 24,
    "48 hours": 48,
}


class NOAAHRRRSource(GribSource):
    """GRIB source for NOAA HRRR 3km regional forecasts (CONUS).

    Downloads wind (UGRD/VGRD at 10m) plus MSL pressure (PRMSL, for
    isobars) from NOAA NOMADS for user-specified geographic bounds.
    Covers the US Pacific Northwest south of the border; pair with
    CMC HRDPS for BC waters.
    """

    def __init__(self):
        self._bounds = None  # (lat_south, lon_west, lat_north, lon_east)

    @property
    def name(self) -> str:
        return "NOAA HRRR"

    @property
    def bounds(self):
        return self._bounds

    @bounds.setter
    def bounds(self, value):
        self._bounds = value

    def _source_tag(self, identifier: str) -> str:
        try:
            return json.loads(identifier).get("source", "NOAA HRRR")
        except (ValueError, AttributeError):
            return "NOAA HRRR"

    def _get_latest_cycle(self):
        """Latest plausibly available HRRR cycle (hourly, ~2h latency)."""
        now = datetime.now(timezone.utc)
        available = now - timedelta(hours=2)
        return available.replace(minute=0, second=0, microsecond=0)

    def _get_available_cycles(self, num_cycles=4):
        latest = self._get_latest_cycle()
        return [latest - timedelta(hours=i) for i in range(num_cycles)]

    @staticmethod
    def _max_hours(cycle) -> int:
        return (
            HRRR_LONG_RANGE
            if f"{cycle.hour:02d}" in HRRR_LONG_CYCLES
            else HRRR_SHORT_RANGE
        )

    def get_download_list(self) -> List:
        cycles = self._get_available_cycles()
        grib_files = []
        for cycle in cycles:
            date_str = cycle.strftime("%Y%m%d")
            cycle_str = f"{cycle.hour:02d}"
            cap = self._max_hours(cycle)
            for preset_name, hours in FORECAST_PRESETS.items():
                if hours > cap:
                    continue
                identifier = json.dumps(
                    {
                        "source": "NOAA HRRR",
                        "date": date_str,
                        "cycle": cycle_str,
                        "hours": hours,
                    }
                )
                grib_files.append(
                    [
                        f"HRRR 3km - {preset_name}",
                        f"NOAA HRRR {cycle_str}Z",
                        self._estimate_size(hours),
                        cycle.strftime("%Y-%m-%d %H:%M UTC"),
                        identifier,
                    ]
                )
        return grib_files

    def _estimate_size(self, hours):
        # Measured ~80KB per forecast hour floor (GRIB headers dominate
        # small subsets) plus ~2MB/hour scaled to the full globe.
        num_steps = hours + 1
        if self._bounds:
            lat_range = abs(self._bounds[2] - self._bounds[0])
            lon_range = abs(self._bounds[3] - self._bounds[1])
            area_fraction = (lat_range * lon_range) / (180 * 360)
            size_mb = num_steps * (0.08 + 2.0 * area_fraction)
        else:
            size_mb = num_steps * 2.0
        if size_mb < 1:
            return f"{size_mb * 1024:.0f} KB"
        return f"{size_mb:.1f} MB"

    def _build_filter_url(self, date_str, cycle, forecast_hour):
        # HRRR mean-sea-level pressure is MSLMA, not PRMSL (verified in the
        # .idx inventory); both decode through the shared sampler.
        params = {
            "file": f"hrrr.t{cycle}z.wrfsfcf{forecast_hour:02d}.grib2",
            "dir": f"/hrrr.{date_str}/{HRRR_SUBDIR}",
            "lev_10_m_above_ground": "on",
            "var_UGRD": "on",
            "var_VGRD": "on",
            "lev_mean_sea_level": "on",
            "var_MSLMA": "on",
        }
        if self._bounds:
            lat_south, lon_west, lat_north, lon_east = self._bounds
            params["subregion"] = ""
            params["leftlon"] = str(lon_west)
            params["rightlon"] = str(lon_east)
            params["toplat"] = str(lat_north)
            params["bottomlat"] = str(lat_south)
        return params

    def download(
        self,
        identifier: str,
        dest_dir: str,
        percentage_callback: Callable[[int], None],
        cancel=None,
    ) -> str:
        config = json.loads(identifier)
        date_str = config["date"]
        cycle = config["cycle"]
        max_hours = config["hours"]
        forecast_steps = list(range(0, max_hours + 1))
        total_steps = len(forecast_steps)

        filename = f"hrrr_{date_str}_{cycle}z_{max_hours}h.grib2"
        final_path = dest_dir + "/" + filename
        logger.info(
            "Downloading HRRR %s %sz, %d forecast hours (%d steps)",
            date_str,
            cycle,
            max_hours,
            total_steps,
        )

        def check_cancel():
            if cancel is not None and cancel.is_set():
                from gweatherrouting.core.marine.chs import DownloadCancelledError

                raise DownloadCancelledError()

        check_cancel()
        temporary = tempfile.NamedTemporaryFile(
            dir=dest_dir, suffix=".part", delete=False
        )
        try:
            with temporary as outfile:
                bytes_written = 0
                for i, fhour in enumerate(forecast_steps):
                    check_cancel()
                    params = self._build_filter_url(date_str, cycle, fhour)
                    response = requests.get(
                        HRRR_FILTER_URL, params=params, stream=True, timeout=(10, 60)
                    )
                    try:
                        response.raise_for_status()
                        for chunk in response.iter_content(chunk_size=8192):
                            check_cancel()
                            outfile.write(chunk)
                            bytes_written += len(chunk)
                            if bytes_written > 256 * 1024 * 1024:
                                raise ValueError(
                                    "Forecast download exceeded 256 MiB; "
                                    "choose a smaller route area"
                                )
                    finally:
                        response.close()
                    percentage_callback(int(100 * (i + 1) / total_steps))
            check_cancel()
            if bytes_written == 0:
                raise RuntimeError(
                    "HRRR download returned empty data. Try a different cycle or date."
                )
            os.replace(temporary.name, final_path)
        finally:
            if os.path.exists(temporary.name):
                os.remove(temporary.name)
        logger.info("HRRR download completed: %s (%d bytes)", filename, bytes_written)
        return final_path
