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

# isort:skip_file

from os import fspath, sep
from importlib.resources import files, as_file


def wind_color(wspeed):  # noqa: C901
    color = "0000CC"
    if wspeed >= 0 and wspeed < 2:
        color = "0000CC"
    elif wspeed >= 2 and wspeed < 4:
        color = "0066FF"
    elif wspeed >= 4 and wspeed < 6:
        color = "00FFFF"
    elif wspeed >= 6 and wspeed < 8:
        color = "00FF66"
    elif wspeed >= 8 and wspeed < 10:
        color = "00CC00"
    elif wspeed >= 10 and wspeed < 12:
        color = "66FF33"
    elif wspeed >= 12 and wspeed < 14:
        color = "CCFF33"
    elif wspeed >= 14 and wspeed < 16:
        color = "FFFF66"
    elif wspeed >= 16 and wspeed < 18:
        color = "FFCC00"
    elif wspeed >= 18 and wspeed < 20:
        color = "FF9900"
    elif wspeed >= 20 and wspeed < 22:
        color = "FF6600"
    elif wspeed >= 22 and wspeed < 24:
        color = "FF3300"
    elif wspeed >= 24 and wspeed < 26:
        color = "FF0000"
    elif wspeed >= 26 and wspeed < 28:
        color = "CC6600"
    elif wspeed >= 28:
        color = "CC0000"

    a = int(color[0:2], 16) / 255.0
    b = int(color[2:4], 16) / 255.0
    c = int(color[4:6], 16) / 255.0
    return (a, b, c)


# Significant wave height palette over 0-4+ m.  This is the single source of
# truth for the CPU painter, the Qt viewport legend and the wave shader-stop
# test; keep all three in lockstep with the buckets below.
WAVE_BUCKET_COLORS = (
    "E6F7FF",  # 0.0 m - calm
    "A6E4FF",  # 0.5 m
    "4DC3FF",  # 1.0 m
    "0096E8",  # 1.5 m
    "0057B7",  # 2.0 m
    "003C8F",  # 2.5 m
    "1F2A7A",  # 3.0 m
    "4B1F8F",  # 3.5 m
    "6E0B5E",  # 4.0+ m
)

# The upper edge (metres) of each bucket.  The final bucket covers 4 m and
# above, so the last colour applies to any height past the last edge.
_WAVE_BUCKET_EDGES = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)


def wave_color(height):
    bucket = 0
    for edge in _WAVE_BUCKET_EDGES:
        if height < edge:
            break
        bucket += 1
    color = WAVE_BUCKET_COLORS[bucket]
    a = int(color[0:2], 16) / 255.0
    b = int(color[2:4], 16) / 255.0
    c = int(color[4:6], 16) / 255.0
    return (a, b, c)


def resource_path(package: str, relative_path: str):
    resource = files(package).joinpath(relative_path)
    with as_file(resource) as path:
        return fspath(path) + (sep if relative_path[-1] in ("/", sep) else "")
