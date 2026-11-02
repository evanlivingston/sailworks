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

"""Forecast time controls shared by the GTK and Qt frontends.

Inspect only enabled-source metadata; this does not open forecast datasets.
"""

from datetime import datetime

TIME_UNITS = {
    "30s": 30,
    "1m": 1 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "3h": 180 * 60,
    "6h": 360 * 60,
    "12h": 720 * 60,
    "24h": 1440 * 60,
    "1d": 1440 * 60,
}


def forecast_intervals(gribs=None, marine=None):
    intervals = []
    if gribs:
        for grib in gribs.gribs:
            start = grib.start_time.timestamp()
            intervals.append((start, start + grib.last_forecast * 3600))
    if marine:
        intervals.extend(
            (float(f.times[0]), float(f.times[-1]))
            for f in marine.fields.fields.values()
            if f.id not in marine.fields.excluded
        )
        intervals.extend(
            (d.requested_start.timestamp(), d.requested_end.timestamp())
            for d in marine.ordered_datasets()
            if marine.is_enabled(d.id)
        )
    return tuple(sorted(intervals))


def forecast_bounds(intervals):
    return (
        (min(a for a, b in intervals), max(b for a, b in intervals))
        if intervals
        else None
    )


def step_forecast_time(clock, seconds, bounds=None):
    if bounds:
        start, end = bounds
        stamp = max(start, min(end, clock.time.timestamp() + seconds))
        clock.set_time(datetime.fromtimestamp(stamp))
    else:
        clock.increase(seconds=seconds)
