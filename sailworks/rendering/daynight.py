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

"""Day/night portrayal of isochrone geometry.

A contour is an instantaneous frontier, so it can cross the terminator: one
layer holds points in daylight and points in darkness. Colours are therefore
resolved per point from the layer's recorded epoch and the point's position,
and only the palette lives in the metric registry.
"""

from ..core.geo.solar import daylight_class_at
from .palettes import colour_for, legend_for, metric_label


def day_night_colour(epoch_s, point, longitude_index=1):
    """Colour for one contour point, or the unknown colour without a fix.

    ``point`` is ``(latitude, longitude)``. A missing epoch or position stays
    unknown rather than being assumed to be day or night.
    """
    try:
        latitude = float(point[0])
        longitude = float(point[longitude_index])
    except (IndexError, TypeError, ValueError):
        return colour_for("day_night", None)
    token = daylight_class_at(epoch_s, latitude, longitude)
    return colour_for("day_night", token)


def day_night_segment_colours(epoch_s, points):
    """One colour per segment; the segment takes its leading point's colour."""
    return tuple(
        day_night_colour(epoch_s, first) for first, _second in zip(points, points[1:])
    )


def day_night_legend():
    """Registry legend for the mode, including the ``Unknown`` row."""
    return {
        "title": metric_label("day_night"),
        "entries": [[label, list(colour)] for label, colour in legend_for("day_night")],
    }
