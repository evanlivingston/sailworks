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


class CairoStyle:
    def __init__(self, color=None, line_width=None, font_size=None, dash=None):
        self.color = color
        self.line_width = line_width
        self.font_size = font_size
        self.dash = dash

    def apply(self, cr):
        if self.line_width:
            cr.set_line_width(self.line_width)
        if self.font_size:
            cr.set_font_size(self.font_size)

        if self.dash is not None and type(self.dash) is float:
            cr.set_dash([self.dash])
        elif self.dash is not None and type(self.dash) is not float:
            cr.set_dash(self.dash)

        if self.color:
            cr.set_source_rgba(
                self.color[0], self.color[1], self.color[2], self.color[3]
            )


class ChartPalette:
    def __init__(self, shallow, sea, landstroke, landfill):
        self.shallow_sea = shallow
        self.sea = sea
        self.land_stroke = landstroke
        self.land_fill = landfill


class Style:
    @staticmethod
    def reset_dash(cr):
        cr.set_dash([])

    chart_palettes = {
        "cm93": ChartPalette(
            CairoStyle(color=(0x73 / 255, 0xB6 / 255, 0xEF / 255, 1.0)),
            CairoStyle(color=(0xD4 / 255, 0xEA / 255, 0xEE / 255, 1.0)),
            CairoStyle(color=(0x52 / 255.0, 0x5A / 255.0, 0x5C / 255.0, 1.0)),
            CairoStyle(color=(0xC9 / 255.0, 0xB9 / 255.0, 0x7A / 255.0, 1.0)),
        ),
        "navionics": ChartPalette(
            CairoStyle(color=(0x20 / 255, 0xB0 / 255, 0xF8 / 255, 1.0)),
            CairoStyle(color=(0xA0 / 255, 0xD8 / 255, 0xF8 / 255, 1.0)),
            CairoStyle(color=(0x3E / 255.0, 0x3A / 255.0, 0x1C / 255.0, 1.0)),
            CairoStyle(color=(0xF8 / 255.0, 0xE8 / 255.0, 0x70 / 255.0, 1.0)),
        ),
        "dark": ChartPalette(
            CairoStyle(color=(0x16 / 255, 0x23 / 255, 0x2F / 255, 1.0)),
            CairoStyle(color=(0x07 / 255, 0x07 / 255, 0x07 / 255, 1.0)),
            CairoStyle(color=(0x36 / 255.0, 0x3C / 255.0, 0x3D / 255.0, 1.0)),
            CairoStyle(color=(0x2C / 255.0, 0x29 / 255.0, 0x1B / 255.0, 1.0)),
        ),
        "default": ChartPalette(
            CairoStyle(color=(0x6C / 255, 0x6C / 255, 0xA4 / 255, 1.0)),
            CairoStyle(color=(0x6C / 255, 0x6C / 255, 0xA4 / 255, 1.0)),
            CairoStyle(color=(0xCC / 255.0, 0x33 / 255.0, 0x33 / 255.0, 1.0)),
            CairoStyle(color=(0xE7 / 255.0, 0xDD / 255.0, 0x1D / 255.0, 1.0)),
        ),
    }

    class Measure:
        Line = CairoStyle(color=(0, 0, 0, 1), line_width=0.6)
        Font = CairoStyle(color=(0, 0, 0, 1), font_size=10)

    class Track:
        Mob = CairoStyle(color=(1, 0, 0, 1), line_width=1)

        LogTrack = CairoStyle(color=(0, 0.5, 0.5, 0.5), line_width=1)

        TrackActive = CairoStyle(
            color=(0x91 / 255.0, 0x32 / 255.0, 0x1C / 255.0, 1), line_width=1, dash=8.0
        )
        TrackActiveFont = CairoStyle(
            color=(0x11 / 255, 0x11 / 255, 0x11 / 255, 0.7), font_size=10
        )
        TrackActivePoiFont = CairoStyle(
            color=(0x11 / 255, 0x11 / 255, 0x11 / 255, 0.7), font_size=10
        )

        TrackItemHL = CairoStyle(color=(1, 0, 0, 1), line_width=2)
        TrackItemHLFont = CairoStyle(color=(1, 0, 0, 0.9), font_size=10)

        TrackInactive = CairoStyle(
            color=(0x91 / 255.0, 0x4A / 255.0, 0x7C / 255.0, 0.6),
            line_width=0.8,
            dash=8.0,
        )
        TrackInactiveFont = CairoStyle(
            color=(0x11 / 255, 0x11 / 255, 0x11 / 255, 0.7), font_size=10
        )
        TrackInactivePoiFont = CairoStyle(
            color=(0x11 / 255, 0x11 / 255, 0x11 / 255, 0.3), font_size=10
        )

        RoutingTrack = CairoStyle(color=(1, 0, 0, 0.8), line_width=1)
        RoutingTrackFont = CairoStyle(
            color=(0x11 / 255, 0x11 / 255, 0x11 / 255, 0.7), font_size=10
        )
        RoutingTrackCircle = CairoStyle(color=(1, 1, 1, 1), line_width=1)

        RoutingTrackHL = CairoStyle(color=(1, 0, 0, 1), line_width=2)
        RoutingTrackFontHL = CairoStyle(
            color=(0x11 / 255, 0x11 / 255, 0x11 / 255, 0.7), font_size=10
        )
        RoutingTrackCircleHL = CairoStyle(color=(1, 1, 1, 1), line_width=2)

        RoutingBoat = CairoStyle(color=(0, 0.4, 0, 1.0), line_width=5)

    class Poi:
        Dot = CairoStyle(color=(0x11 / 255, 0x11 / 255, 0x11 / 255, 1))
        Quad = CairoStyle(color=(0x11 / 255, 0x11 / 255, 0x11 / 255, 1), line_width=0.3)
        QuadInt = CairoStyle(color=(0xFF / 255, 0xFF / 255, 0xFF / 255, 0.5))
        Font = CairoStyle(color=(0x11 / 255, 0x11 / 255, 0x11 / 255, 0.7), font_size=10)

        DotHL = CairoStyle(color=(1, 0, 0, 1))
        QuadHL = CairoStyle(color=(1, 0, 0, 1), line_width=1.5)
        QuadIntHL = CairoStyle(color=(1, 0.9, 0.9, 0.7))
        FontHL = CairoStyle(color=(1, 0, 0, 0.9), font_size=10)


SPEED_BANDS = (
    (2.0, (0.3, 0.5, 1.0, 0.9)),
    (4.0, (0.2, 0.7, 0.9, 0.9)),
    (6.0, (0.2, 0.85, 0.5, 0.9)),
    (8.0, (0.5, 0.9, 0.2, 0.9)),
    (10.0, (0.9, 0.85, 0.1, 0.9)),
    (12.0, (1.0, 0.6, 0.1, 0.9)),
    (None, (1.0, 0.3, 0.1, 0.9)),
)
SPEED_BAND_LABELS = (
    "0–2 kt",
    "2–4 kt",
    "4–6 kt",
    "6–8 kt",
    "8–10 kt",
    "10–12 kt",
    "12+ kt",
)

ROUTE_UNKNOWN_COLOUR = (0.55, 0.55, 0.55, 0.9)
ROUTE_UPWIND_COLOUR = (0.2, 0.55, 0.95, 0.9)
ROUTE_DOWNWIND_COLOUR = (0.95, 0.55, 0.15, 0.9)
ROUTE_PORT_COLOUR = (0.9, 0.25, 0.35, 0.9)
ROUTE_STARBOARD_COLOUR = (0.2, 0.7, 0.35, 0.9)
# Sail-change markers use a warm, high-contrast colour so a square on a leg
# boundary is distinct from the circular waypoint dots in every colour mode.
ROUTE_SAIL_CHANGE_COLOUR = (0.98, 0.82, 0.2, 1.0)


def route_speed_color(speed):
    """Map boat speed (kts) to a color gradient: blue (slow) -> green -> yellow -> red (fast)."""
    for limit, colour in SPEED_BANDS:
        if limit is None or speed < limit:
            return colour
    return SPEED_BANDS[-1][1]


def route_wind_angle_colour(twa):
    """Upwind below 90° TWA, downwind at or above it; None means unknown."""
    if twa is None:
        return ROUTE_UNKNOWN_COLOUR
    try:
        value = abs(float(twa))
    except (TypeError, ValueError):
        return ROUTE_UNKNOWN_COLOUR
    return ROUTE_UPWIND_COLOUR if value < 90.0 else ROUTE_DOWNWIND_COLOUR


def route_tack_colour(tack):
    """Colour by the recorded leg tack only; never infer it from signed TWA."""
    if not isinstance(tack, str):
        return ROUTE_UNKNOWN_COLOUR
    value = tack.strip().lower()
    if value.startswith("port"):
        return ROUTE_PORT_COLOUR
    if value.startswith("starboard"):
        return ROUTE_STARBOARD_COLOUR
    return ROUTE_UNKNOWN_COLOUR


def route_leg_colour(mode, speed=None, twa=None, tack=None):
    """Colour for one leg in the requested mode, grey when its metric is absent."""
    if mode == "speed":
        return route_speed_color(0 if speed is None else speed)
    if mode == "wind_angle":
        return route_wind_angle_colour(twa)
    if mode == "tack":
        return route_tack_colour(tack)
    return ROUTE_UNKNOWN_COLOUR


def route_legend_entries(mode):
    """Ordered ``(label, rgba)`` legend entries matching the segment palette."""
    if mode == "wind_angle":
        return (
            ("Upwind (<90° TWA)", ROUTE_UPWIND_COLOUR),
            ("Downwind (≥90° TWA)", ROUTE_DOWNWIND_COLOUR),
            ("Unknown", ROUTE_UNKNOWN_COLOUR),
        )
    if mode == "tack":
        return (
            ("Port", ROUTE_PORT_COLOUR),
            ("Starboard", ROUTE_STARBOARD_COLOUR),
            ("Unknown", ROUTE_UNKNOWN_COLOUR),
        )
    return tuple(
        (label, colour)
        for label, (_limit, colour) in zip(SPEED_BAND_LABELS, SPEED_BANDS)
    )


def route_legend_title(mode):
    from sailworks.core.geo.routing import ROUTE_COLOUR_MODE_LABELS

    return ROUTE_COLOUR_MODE_LABELS.get(mode, "Speed")
