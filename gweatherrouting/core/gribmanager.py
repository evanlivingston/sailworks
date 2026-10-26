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

from typing import Dict, List

import weatherrouting
from weatherrouting import Grib

from gweatherrouting.core.gribsampling import WeatherSampler


class GribManager(weatherrouting.Grib):
    """Transient sampler interface for published GRIB-backed model forecasts.

    Persistent forecast selection, import, download, and deletion are owned by
    the immutable forecast-analysis store, not by this adapter.
    """

    def __init__(self):
        self.gribs: List = []
        self.timeframe = [0, 0]

    def has_grib(self) -> bool:
        return bool(self.gribs)

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
        if not self.gribs:
            return []
        if all(isinstance(g, Grib) for g in self.gribs):
            return self.weather_sampler().grid("wind", t, bounds)
        values = []
        for x in self.gribs:
            try:
                values.extend(x.get_wind(t, bounds))
            except Exception:
                continue
        return values

    def get_wind_2d(self, tt, bounds):
        values = sorted(self.get_wind(tt, bounds), key=lambda x: x[2][1])
        grouped: Dict[float, list] = {}
        for value in values:
            grouped.setdefault(value[2][1], []).append(value)
        return [sorted(row, key=lambda x: x[2][0]) for row in grouped.values()]
