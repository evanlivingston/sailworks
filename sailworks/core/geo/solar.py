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

"""Solar elevation for daylight portrayal.

The NOAA solar-position approximation is good to about a tenth of a degree for
this century, which is far finer than the twilight bands it feeds. It needs no
ephemeris data and no dependency, so a recorded layer epoch and a position are
enough to say whether a contour point was sailed in daylight.
"""

import math

# Standard sunrise/sunset: the sun's upper limb touches the horizon with
# refraction taken into account.
SUNRISE_ELEVATION = -0.833
# Civil twilight ends when the sun is six degrees below the horizon.
CIVIL_TWILIGHT_ELEVATION = -6.0

# Tokens mirror the registered ``day_night`` categories.
DAY = "day"
CIVIL_TWILIGHT = "civil_twilight"
NIGHT = "night"


def solar_elevation(latitude, longitude, epoch_s):
    """Solar elevation in degrees at a position and UTC epoch.

    Returns ``None`` when the position or time is unusable, so a gap is
    reported as unknown rather than as a plausible daylight value.
    """
    try:
        latitude = float(latitude)
        longitude = float(longitude)
        epoch_s = float(epoch_s)
    except (TypeError, ValueError):
        return None
    if not all(map(math.isfinite, (latitude, longitude, epoch_s))):
        return None
    # Julian day, then days since J2000.0.
    days = epoch_s / 86400.0 + 2440587.5
    elapsed = days - 2451545.0
    mean_longitude = (280.460 + 0.9856474 * elapsed) % 360.0
    mean_anomaly = math.radians((357.528 + 0.9856003 * elapsed) % 360.0)
    ecliptic = math.radians(
        mean_longitude
        + 1.915 * math.sin(mean_anomaly)
        + 0.020 * math.sin(2.0 * mean_anomaly)
    )
    obliquity = math.radians(23.439 - 0.0000004 * elapsed)
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic))
    right_ascension = math.atan2(
        math.cos(obliquity) * math.sin(ecliptic), math.cos(ecliptic)
    )
    sidereal = (18.697374558 + 24.06570982441908 * elapsed) % 24.0
    hour_angle = math.radians((sidereal * 15.0 + longitude) % 360.0) - right_ascension
    phi = math.radians(latitude)
    sine = math.sin(phi) * math.sin(declination) + math.cos(phi) * math.cos(
        declination
    ) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, sine))))


def daylight_class(elevation):
    """Day/night token for a solar elevation in degrees, or ``None``."""
    if elevation is None:
        return None
    if elevation > SUNRISE_ELEVATION:
        return DAY
    if elevation > CIVIL_TWILIGHT_ELEVATION:
        return CIVIL_TWILIGHT
    return NIGHT


def daylight_class_at(epoch_s, latitude, longitude):
    """Day/night token for a position and UTC epoch, or ``None``."""
    return daylight_class(solar_elevation(latitude, longitude, epoch_s))
