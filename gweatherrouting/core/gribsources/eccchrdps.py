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
import re
import tempfile
from datetime import datetime, timezone
from typing import Callable, List

import requests

from gweatherrouting.core.gribsources.base import GribSource
from .options import custom_rows, selection
from .parallel import (
    MAX_CONCURRENT_FORECAST_REQUESTS,
    MAX_DOWNLOAD_BYTES,
    write_ordered_payloads,
)

logger = logging.getLogger("gweatherrouting")

# ECCC MSC Datamart, continental 2.5km HRDPS on a rotated lat-lon grid.
# No server-side subsetting: each variable/hour is one full-domain file.
DATAMART_URL = "https://dd.weather.gc.ca/"
HRDPS_PATH = "WXO-DD/model_hrdps/continental/2.5km"
HRDPS_CYCLES = ("00", "06", "12", "18")

# Per-hour download sizes (continental domain, measured Oct 2026)
UGRD_MB = 1.9
VGRD_MB = 1.9
PRMSL_MB = 0.8

FORECAST_PRESETS = {
    "24 hours": 24,
    "48 hours": 48,
}


class ECCHRDPSource(GribSource):
    """GRIB source for CMC HRDPS 2.5km continental forecasts.

    Downloads 10m wind (UGRD/VGRD AGL-10m, every hour) plus MSL
    pressure (PRMSL, every 3rd hour) from the free ECCC MSC Datamart.
    Covers BC and the PNW north of the HRRR domain edge on a rotated
    grid (decoded via GDAL/projection support in the sampler).
    """

    def __init__(self, session=None):
        self._session = session

    @property
    def name(self) -> str:
        return "CMC HRDPS"

    def _get(self, url, session):
        response = session.get(url, timeout=(5, 30))
        response.raise_for_status()
        return response

    def _recent_dates(self, session, count=3):
        root = self._get(DATAMART_URL, session).text
        dates = sorted(set(re.findall(r'href="(\d{8})/"', root)), reverse=True)
        return dates[:count]

    def _discover(self):
        """Newest date/cycle with a complete PT000H wind pair."""
        session = self._session or requests.Session()
        close = self._session is None
        try:
            for date in self._recent_dates(session):
                base = f"{DATAMART_URL}{date}/{HRDPS_PATH}/"
                try:
                    cycles = sorted(
                        set(
                            re.findall(
                                r'href="((?:00|06|12|18)/)"',
                                self._get(base, session).text,
                            )
                        ),
                        reverse=True,
                    )
                except requests.HTTPError:
                    continue
                for cycle in cycles:
                    hour_url = base + cycle.strip("/") + "/000/"
                    try:
                        listing = self._get(hour_url, session).text
                    except requests.HTTPError:
                        continue
                    template = f"{date}T{cycle.strip('/')}Z_MSC_HRDPS_"
                    if (
                        f"{template}UGRD_AGL-10m_RLatLon0.0225_PT000H.grib2" in listing
                        and f"{template}VGRD_AGL-10m_RLatLon0.0225_PT000H.grib2"
                        in listing
                    ):
                        return date, cycle.strip("/")
        finally:
            if close:
                session.close()
        raise ValueError("No complete HRDPS run advertised on the MSC Datamart")

    def get_download_list(self, options=None) -> List:
        try:
            date, cycle = self._discover()
        except Exception as exc:
            logger.error("HRDPS discovery failed: %s", exc)
            return []
        run = datetime.strptime(f"{date}T{cycle}", "%Y%m%dT%H").replace(
            tzinfo=timezone.utc
        )
        if options is not None:
            return custom_rows(
                self,
                options,
                [(date, cycle, run.strftime("%Y-%m-%d %H:%M UTC"), 48)],
                lambda cycle, fields, cap: range(
                    0, cap + 1, 3 if "wind" not in fields else 1
                ),
                self._estimate_custom_size,
            )
        grib_files = []
        for preset_name, hours in FORECAST_PRESETS.items():
            identifier = json.dumps(
                {"source": "CMC HRDPS", "date": date, "cycle": cycle, "hours": hours}
            )
            grib_files.append(
                [
                    f"HRDPS 2.5km - {preset_name}",
                    f"CMC HRDPS {cycle}Z",
                    self._estimate_size(hours),
                    run.strftime("%Y-%m-%d %H:%M UTC"),
                    identifier,
                ]
            )
        return grib_files

    @staticmethod
    def _estimate_size(hours):
        steps = hours + 1
        pressure_steps = len(range(0, hours + 1, 3))
        size_mb = steps * (UGRD_MB + VGRD_MB) + pressure_steps * PRMSL_MB
        return f"{size_mb:.0f} MB"

    @staticmethod
    def _estimate_custom_size(steps, fields):
        size = len(steps) * (UGRD_MB + VGRD_MB) if "wind" in fields else 0
        if "pressure" in fields:
            size += sum(h % 3 == 0 for h in steps) * PRMSL_MB
        return f"~{size:.1f} MB"

    def _file_url(self, date, cycle, hour, variable):
        lead = f"{hour:03d}"
        hour_dir = f"{DATAMART_URL}{date}/{HRDPS_PATH}/{cycle}/{lead}/"
        if variable == "PRMSL":
            name = f"{date}T{cycle}Z_MSC_HRDPS_PRMSL_MSL_RLatLon0.0225_PT{lead}H.grib2"
        else:
            name = (
                f"{date}T{cycle}Z_MSC_HRDPS_{variable}_AGL-10m_"
                f"RLatLon0.0225_PT{lead}H.grib2"
            )
        return hour_dir + name

    def _download_plan(self, config):
        date, cycle = config["date"], config["cycle"]
        steps, fields = selection(config, range(49), range(config["hours"] + 1))
        plan: list[str] = []
        for hour in steps:
            if "wind" in fields:
                plan.extend(
                    self._file_url(date, cycle, hour, v) for v in ("UGRD", "VGRD")
                )
            if "pressure" in fields and hour % 3 == 0:
                plan.append(self._file_url(date, cycle, hour, "PRMSL"))
        if not plan:
            raise ValueError("No supported HRDPS fields at the selected hours")
        return plan

    def download(
        self,
        identifier: str,
        dest_dir: str,
        percentage_callback: Callable[[int], None],
        cancel=None,
    ) -> str:
        config = json.loads(identifier)
        date, cycle, max_hours = config["date"], config["cycle"], config["hours"]
        plan = self._download_plan(config)

        filename = f"hrdps_{date}_{cycle}z_{max_hours}h.grib2"
        final_path = dest_dir + "/" + filename
        logger.info(
            "Downloading HRDPS %s %sz, %d files for %d forecast hours",
            date,
            cycle,
            len(plan),
            max_hours,
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
                sessions = {}

                def fetch(url):
                    check_cancel()
                    # A session is confined to one worker; requests.Session is
                    # not safe to share between concurrent requests.
                    from threading import get_ident

                    if self._session is not None:
                        session = self._session
                    else:
                        worker_id = get_ident()
                        session = sessions.get(worker_id)
                        if session is None:
                            session = sessions[worker_id] = requests.Session()
                    response = session.get(url, stream=True, timeout=(10, 60))
                    try:
                        response.raise_for_status()
                        chunks = bytearray()
                        for chunk in response.iter_content(chunk_size=65536):
                            check_cancel()
                            if len(chunks) + len(chunk) > MAX_DOWNLOAD_BYTES:
                                raise ValueError(
                                    "Forecast download exceeded 256 MiB; "
                                    "choose a smaller route area"
                                )
                            chunks.extend(chunk)
                        return bytes(chunks)
                    finally:
                        response.close()

                try:
                    bytes_written = write_ordered_payloads(
                        outfile,
                        plan,
                        fetch,
                        check_cancel,
                        percentage_callback,
                        max_workers=(
                            1
                            if self._session is not None
                            else MAX_CONCURRENT_FORECAST_REQUESTS
                        ),
                    )
                finally:
                    if self._session is None:
                        for session in sessions.values():
                            session.close()
            check_cancel()
            if bytes_written == 0:
                raise RuntimeError("HRDPS download returned empty data.")
            os.replace(temporary.name, final_path)
        finally:
            if os.path.exists(temporary.name):
                os.remove(temporary.name)
        logger.info("HRDPS download completed: %s (%d bytes)", filename, bytes_written)
        return final_path
