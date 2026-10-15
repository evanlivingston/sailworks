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

import logging
import os
from shutil import copyfile
from typing import Dict, List

import weatherrouting

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.core.gribsources import (
    ECCHRDPSource,
    NOAAGFSSource,
    NOAAHRRRSource,
    OpenSkironSource,
)
from gweatherrouting.core.storage import GRIB_DIR, Storage

logger = logging.getLogger("gweatherrouting")

GRIB_EXTENSIONS = (".grib", ".grb", ".grib2", ".grb2")


class GribManagerStorage(Storage):
    def __init__(self):
        Storage.__init__(self, "grib-manager")
        self.opened = []
        self.load_or_save_default()


class GribManager(weatherrouting.Grib):
    def __init__(self):
        self.storage = GribManagerStorage()
        self.grib_files = None

        self.gribs: List[Grib] = []
        self.timeframe = [0, 0]

        self.sources = [
            OpenSkironSource(),
            NOAAGFSSource(),
            NOAAHRRRSource(),
            ECCHRDPSource(),
        ]

        self.local_gribs = []
        self.refresh_local_gribs()

        for x in self.storage.opened:
            self.enable(x)

    def get_source(self, source_name):
        for s in self.sources:
            if s.name == source_name:
                return s
        return None

    def refresh_local_gribs(self):
        self.local_gribs = []
        for x in os.listdir(GRIB_DIR):
            if not any(x.endswith(ext) for ext in GRIB_EXTENSIONS):
                continue

            m = Grib.parse_metadata(GRIB_DIR + "/" + x)
            self.local_gribs.append(m)

    def store_opened_gribs(self):
        ss: List = []
        for x in self.gribs:
            try:
                ss.index(x.name)
            except Exception:
                ss.append(x.name)
        self.storage.opened = ss

    def load(self, path):
        logger.info("Loading grib %s", path)
        self.gribs.append(Grib.parse(path))

    def change_state(self, name, state):
        if not state:
            self.disable(name)
        else:
            self.enable(name)

    def enable(self, name):
        self.load(GRIB_DIR + "/" + name)
        self.store_opened_gribs()

    def disable(self, name):
        for x in self.gribs:
            if x.name == name:
                self.gribs.remove(x)
                self.store_opened_gribs()

    def is_enabled(self, name) -> bool:
        for x in self.gribs:
            if x.name == name:
                return True
        return False

    def has_grib(self) -> bool:
        return len(self.gribs) > 0

    def weather_sampler(self):
        signature = tuple(self.gribs)
        if getattr(self, "_sampling_signature", None) != signature:
            self._sampling_signature = signature
            self._weather_sampler = WeatherSampler(self.gribs)
        return self._weather_sampler

    def get_wind_at(self, t, lat: float, lon: float):
        """Use the newest enabled model run; older runs fill coverage gaps.

        Equal reference times retain enabled-file order. Download time does not
        affect priority, and sampling does not reorder the user's enabled list.
        """
        if self.gribs and all(isinstance(g, Grib) for g in self.gribs):
            return self.weather_sampler().wind_at(t, lat, lon)
        for x in sorted(
            self.gribs, key=lambda g: g.start_time.timestamp(), reverse=True
        ):
            try:
                answer = x.get_wind_at(t, lat, lon)
                if answer is not None:
                    return answer
            except Exception:
                pass

    def get_wind(self, t, bounds) -> List:
        if self.gribs and all(isinstance(g, Grib) for g in self.gribs):
            return self.weather_sampler().grid("wind", t, bounds)
        g: List = []

        for x in self.gribs:
            try:
                g = g + x.get_wind(t, bounds)
            except Exception:
                pass
        return g

    def get_wind_2d(self, tt, bounds):
        dd = sorted(self.get_wind(tt, bounds), key=lambda x: x[2][1])

        ddict: Dict = {}
        for x in dd:
            if x[2][1] not in ddict:
                ddict[x[2][1]] = []
            ddict[x[2][1]].append(x)

        ddlist = []
        for x, v in ddict.items():
            ddlist.append(sorted(v, key=lambda x: x[2][0]))

        return ddlist

    def get_download_list(self, source_name=None, force=False):
        """Get available GRIB files from all sources or a specific source.

        Args:
            source_name: If provided, only query this source. Otherwise query all.
            force: Force refresh of cached lists.

        Returns:
            List of [filename, source, size, date, identifier] entries.
        """
        if not self.grib_files or force:
            self.grib_files = []
            for source in self.sources:
                if source_name and source.name != source_name:
                    continue
                try:
                    self.grib_files.extend(source.get_download_list())
                except Exception as e:
                    logger.error(
                        "Failed to get download list from %s: %s", source.name, str(e)
                    )

        return self.grib_files

    def remove(self, name):
        if self.is_enabled(name):
            self.disable(name)
        os.remove(GRIB_DIR + "/" + name)

    def import_grib(self, path):
        try:
            name = path.split("/")[-1]
            logger.info("Importing grib %s", path)
            copyfile(path, GRIB_DIR + "/" + name)
            self.enable(name)
            return True
        except Exception as e:
            logger.error(str(e))

    def download(self, identifier, percentage_callback, callback):
        """Download a GRIB file using the appropriate source.

        The identifier determines which source handles the download.
        JSON identifiers carry a "source" name (older GFS ones default
        to NOAA GFS); URLs are handled by OpenSkiron.
        """
        try:
            # Determine which source to use based on the identifier
            source = None
            if identifier.startswith("{"):
                try:
                    import json

                    name = json.loads(identifier).get("source", "NOAA GFS")
                except ValueError:
                    name = "NOAA GFS"
                source = self.get_source(name)
            else:
                # URL = OpenSkiron
                source = self.get_source("OpenSkiron")

            if source is None:
                logger.error("No source found for identifier: %s", identifier)
                callback(False)
                return

            final_path = source.download(identifier, GRIB_DIR, percentage_callback)
            self.load(final_path)
            callback(True)
        except Exception as e:
            logger.error("Download failed: %s", str(e))
            callback(False)
