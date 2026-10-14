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
    def __init__(self, connection_manager):
        self.boat_info = None
        self._connection_manager = connection_manager

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

    def _draw_connection_status(self, cr, panel_x, panel_y, panel_w):
        """Draw a compact connection status line above the gauge panel.

        Shows a single summary row normally, expanding only when a
        connection is down to show which one has the problem.
        """
        from .numericgauge import _rounded_rect

        cm = self._connection_manager
        statuses = cm.get_status()
        polling = cm.polling_active

        row_h = 18
        pad = 6

        # Collect disconnected sources for the detail rows
        disconnected = [s for s in statuses if not s["connected"]] if statuses else []

        # How many rows: 1 summary + 1 per disconnected source
        n_rows = 1 + len(disconnected)
        conn_panel_h = n_rows * row_h + pad * 2
        cy = panel_y - conn_panel_h - 4

        # Background
        cr.set_source_rgba(0.03, 0.03, 0.03, 0.55)
        _rounded_rect(cr, panel_x, cy, panel_w, conn_panel_h, 6)
        cr.fill()

        # --- Summary row ---
        ry = cy + pad

        if not polling and not statuses:
            # Data disabled
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.8)
            cr.arc(panel_x + pad + 5, ry + 8, 3.5, 0, 2 * math.pi)
            cr.fill()
            cr.set_font_size(10)
            cr.move_to(panel_x + pad + 14, ry + 12)
            cr.show_text("Data off")
            return

        if not statuses:
            # Polling active but no connections configured
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.8)
            cr.arc(panel_x + pad + 5, ry + 8, 3.5, 0, 2 * math.pi)
            cr.fill()
            cr.set_font_size(10)
            cr.move_to(panel_x + pad + 14, ry + 12)
            cr.show_text("No connections")
            return

        total = len(statuses)
        connected = sum(1 for s in statuses if s["connected"])
        total_pkts = sum(s["packets"] for s in statuses)

        # Dot: green if all connected, red if any down
        if connected == total:
            cr.set_source_rgba(0.2, 0.9, 0.4, 0.9)
        else:
            cr.set_source_rgba(1.0, 0.3, 0.2, 0.9)
        cr.arc(panel_x + pad + 5, ry + 8, 3.5, 0, 2 * math.pi)
        cr.fill()

        # Summary text
        cr.set_source_rgba(0.85, 0.85, 0.85, 1.0)
        cr.set_font_size(10)
        summary = "%d/%d connected" % (connected, total)
        cr.move_to(panel_x + pad + 14, ry + 12)
        cr.show_text(summary)

        # Packet count (right-aligned)
        pkt_text = "%d pkt" % total_pkts
        cr.set_font_size(9)
        pkt_ext = cr.text_extents(pkt_text)
        cr.set_source_rgba(0.4, 0.7, 0.9, 1.0)
        cr.move_to(panel_x + panel_w - pad - pkt_ext.width, ry + 12)
        cr.show_text(pkt_text)

        # --- Detail rows for disconnected sources only ---
        for i, s in enumerate(disconnected):
            ry = cy + pad + (i + 1) * row_h

            cr.set_source_rgba(1.0, 0.3, 0.2, 0.7)
            cr.arc(panel_x + pad + 15, ry + 8, 2.5, 0, 2 * math.pi)
            cr.fill()

            cr.set_source_rgba(0.7, 0.7, 0.7, 0.9)
            cr.set_font_size(9)
            cr.move_to(panel_x + pad + 22, ry + 11)
            cr.show_text(s["name"])

    def _draw_dashboard(self, gpsmap, cr):
        """Draw the instrument dashboard panel on the map."""
        self._update_gauge_values()

        width = float(gpsmap.get_allocated_width())
        height = float(gpsmap.get_allocated_height())

        cols = len(self._gauges)
        gauge_w = NumericGauge.WIDTH
        gauge_h = NumericGauge.HEIGHT
        gauge_gap = 4

        total_w = cols * gauge_w + (cols - 1) * gauge_gap
        panel_padding = 6

        # Center horizontally at the bottom of the map
        panel_x = (width - total_w) / 2.0 - panel_padding
        panel_y = height - gauge_h - panel_padding * 2 - 12
        panel_w = total_w + panel_padding * 2

        # Draw panel background with rounded corners
        from .numericgauge import _rounded_rect

        cr.set_source_rgba(0.03, 0.03, 0.03, 0.55)
        _rounded_rect(cr, panel_x, panel_y, panel_w, gauge_h + panel_padding * 2, 8)
        cr.fill()

        # Draw each gauge
        for i, gauge in enumerate(self._gauges):
            gx = panel_x + panel_padding + i * (gauge_w + gauge_gap)
            gy = panel_y + panel_padding
            gauge.draw(cr, gx, gy)

        # Draw connection status above the gauge panel
        self._draw_connection_status(cr, panel_x, panel_y, panel_w)
