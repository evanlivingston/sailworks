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

import datetime

from sailworks.core.utils import EventDispatcher


def naive_wall_time(value: datetime.datetime) -> datetime.datetime:
    """Return a datetime without its timezone, preserving the displayed time."""
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def utc_to_wall(value: datetime.datetime) -> datetime.datetime:
    """Convert a naive-UTC racing timestamp to the local wall clock."""
    if value is None:
        return None
    epoch = value.replace(tzinfo=datetime.timezone.utc).timestamp()
    return datetime.datetime.fromtimestamp(epoch).replace(tzinfo=None)


def wall_to_utc(value: datetime.datetime) -> datetime.datetime:
    """Convert a local wall-clock time back to a naive-UTC timestamp."""
    if value is None:
        return None
    epoch = value.timestamp()
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).replace(
        tzinfo=None
    )


class TimeControl(EventDispatcher):
    DFORMAT = "%Y/%m/%d, %H:%M"

    def __init__(self):
        EventDispatcher.__init__(self)
        self._time: datetime.datetime
        self.now()

    def now(self):
        self._time = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
        self.dispatch("time_change", self.time)

    @property
    def time(self):
        return naive_wall_time(self._time)

    def get_timestamp(self) -> int:
        return int(self.time.timestamp())

    def set_time(self, v):
        self._time = v
        self.dispatch("time_change", self.time)

    def decrease(self, hours=0, minutes=0, seconds=0):
        self._time -= datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds)
        self.dispatch("time_change", self.time)

    def increase(self, hours=0, minutes=0, seconds=0):
        self._time += datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds)
        self.dispatch("time_change", self.time)
