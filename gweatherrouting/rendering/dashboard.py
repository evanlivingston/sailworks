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

"""Production instrument values and Cairo dashboard shared by GTK and Qt."""

import math

from .numericgauge import NumericGauge


class DashboardRenderer:
    def __init__(self):
        self.boat_info = None

        # Dashboard gauges
        self._gauges = [
            NumericGauge("SOG", "kts", "%.1f"),
            NumericGauge("COG", "\u00b0", "%.0f"),
            NumericGauge("AWS", "kts", "%.1f"),
            NumericGauge("AWA", "\u00b0", "%.0f"),
            # NumericGauge("AWD", "\u00b0", "%.0f"),
            NumericGauge("TWS", "kts", "%.1f"),
            # NumericGauge("TWD", "\u00b0", "%.0f"),
            NumericGauge("TWA", "\u00b0", "%.0f"),
            NumericGauge("Depth", "m", "%.1f"),
        ]

    def _update_gauge_values(self):
        """Update gauge values from boat_info."""
        bi = self.boat_info
        if bi is None:
            return

        tws = bi.tws
        twa = bi.twa

        # If no true wind data, calculate from apparent wind + SOG
        if (
            tws is None
            and bi.aws is not None
            and bi.awa is not None
            and bi.sog is not None
        ):
            awa_rad = math.radians(bi.awa)
            tws = math.sqrt(
                bi.aws**2 + bi.sog**2 - 2 * bi.aws * bi.sog * math.cos(awa_rad)
            )
            # TWA from law of sines / atan2
            twa_y = bi.aws * math.sin(awa_rad)
            twa_x = bi.aws * math.cos(awa_rad) - bi.sog
            twa = math.degrees(math.atan2(twa_y, twa_x))
            if twa < 0:
                twa += 360

        gauge_map = {
            "SOG": bi.sog,
            "COG": bi.cog,
            "AWS": bi.aws,
            "AWA": bi.awa,
            "TWS": tws,
            "TWA": twa,
            "Depth": bi.depth,
        }

        for gauge in self._gauges:
            if gauge.label in gauge_map:
                gauge.set_value(gauge_map[gauge.label])

    def _draw_dashboard(self, gpsmap, cr):
        """Draw the instrument dashboard panel on the map."""
        self._update_gauge_values()

        width = float(gpsmap.get_allocated_width())
        height = float(gpsmap.get_allocated_height())

        gauge_w = NumericGauge.WIDTH
        gauge_h = NumericGauge.HEIGHT
        gauge_gap = 4
        panel_padding = 6
        cols = max(
            1,
            min(
                len(self._gauges),
                int((width - panel_padding * 2 + gauge_gap) // (gauge_w + gauge_gap)),
            ),
        )
        rows = math.ceil(len(self._gauges) / cols)

        total_w = cols * gauge_w + (cols - 1) * gauge_gap
        total_h = rows * gauge_h + (rows - 1) * gauge_gap

        # Center horizontally at the bottom of the map
        panel_x = (width - total_w) / 2.0 - panel_padding
        panel_y = height - total_h - panel_padding * 2 - 12
        panel_w = total_w + panel_padding * 2

        # Draw panel background with rounded corners
        from .numericgauge import _rounded_rect

        cr.set_source_rgba(0.03, 0.03, 0.03, 0.55)
        _rounded_rect(cr, panel_x, panel_y, panel_w, total_h + panel_padding * 2, 8)
        cr.fill()

        # Draw each gauge
        for i, gauge in enumerate(self._gauges):
            gx = panel_x + panel_padding + (i % cols) * (gauge_w + gauge_gap)
            gy = panel_y + panel_padding + (i // cols) * (gauge_h + gauge_gap)
            gauge.draw(cr, gx, gy)
