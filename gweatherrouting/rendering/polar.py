# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""Toolkit-independent polar drawing and hit testing, shared by GTK and Qt.

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
import math
from typing import List, Optional, Tuple

from weatherrouting import Polar
from gweatherrouting.core.polarperformance import PerformanceSample, wind_band

logger = logging.getLogger("gweatherrouting")

POLAR_COLORS = [
    (1.0, 0.2, 0.2),  # red
    (0.2, 0.6, 1.0),  # blue
    (0.2, 0.8, 0.2),  # green
    (1.0, 0.6, 0.0),  # orange
    (0.8, 0.2, 0.8),  # purple
    (0.0, 0.8, 0.8),  # cyan
    (1.0, 1.0, 0.2),  # yellow
    (1.0, 0.4, 0.6),  # pink
    (0.6, 0.4, 0.2),  # brown
    (0.4, 1.0, 0.6),  # mint
    (0.6, 0.6, 1.0),  # lavender
    (1.0, 0.8, 0.4),  # gold
]


class PolarRenderer:
    def queue_draw(self):
        raise NotImplementedError

    def init_polar_renderer(self, polar_manager, full_circle=False):
        self.polar_manager = polar_manager
        self.polar = None
        self.full_circle = full_circle
        self.samples: Tuple[PerformanceSample, ...] = ()
        self.wind_filter = None
        self.hover_wind: Optional[float] = None
        self.isolated_wind: Optional[float] = None
        self.hover_text: Optional[str] = None
        self._point_positions: List[Tuple[float, float, PerformanceSample]] = []
        self._curve_segments: List[Tuple[float, float, float, float, float]] = []
        self._chart_center = (0.0, 0.0)
        self.mousePos = None

    def on_mouse_move(self, widget, event):
        self.mousePos = (event.x, event.y)
        self.hover_text, self.hover_wind = self._hover_details(event.x, event.y)
        self.queue_draw()

    def on_mouse_leave(self, widget, event=None):
        self.mousePos = None
        self.hover_text = None
        self.hover_wind = None
        self.queue_draw()

    def on_mouse_press(self, widget, event):
        # Click a windspeed curve to isolate it (highlight only it); click the
        # same isolated windspeed again to unisolate. Clicking empty space does
        # nothing. Hover still works transiently on top of isolation.
        wind, _angle = self._curve_hover(event.x, event.y) or (None, None)
        if wind is None:
            return
        if self.isolated_wind == wind:
            self.isolated_wind = None
        else:
            self.isolated_wind = wind
        self.queue_draw()

    def load_polar(self, polar_file):
        polar = None
        try:
            polar = Polar(self.polar_manager.get_path(polar_file))
        except Exception:
            logger.error("Error loading polar file %s", polar_file)

        self.set_polar(polar)

    def set_polar(self, polar):
        self.polar = polar
        self.isolated_wind = None
        self.on_mouse_leave(self, None)
        self._curve_segments = []
        self._point_positions = []
        self.queue_draw()

    def set_performance(self, samples, wind_filter=None):
        self.samples = samples
        if wind_filter != self.wind_filter:
            self.on_mouse_leave(self, None)
        self.wind_filter = wind_filter
        self._curve_segments = []
        self._point_positions = []
        self.queue_draw()

    def _hover_details(self, x, y):
        curve = self._curve_hover(x, y)
        if curve is not None:
            tws, angle = curve
            return f"TWS {tws:g} kt · TWA {angle:.1f}°", tws
        if not self._point_positions:
            return None, None
        px, py, sample = min(
            self._point_positions, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2
        )
        if math.hypot(px - x, py - y) > 10:
            return None, None
        band = wind_band(sample.tws, self.polar.tws) if self.polar else None
        return (
            f"TWA {sample.twa:.1f}° · TWS {sample.tws:.1f} kt\n"
            f"{sample.basis.upper()} {sample.speed:.2f} kt",
            self.polar.tws[band] if band is not None else None,
        )

    def _curve_hover(self, x, y):
        closest = None
        distance = 6.0
        for ax, ay, bx, by, tws in self._curve_segments:
            vx, vy = bx - ax, by - ay
            length_squared = vx * vx + vy * vy
            if length_squared == 0:
                continue
            fraction = max(0, min(1, ((x - ax) * vx + (y - ay) * vy) / length_squared))
            px, py = ax + fraction * vx, ay + fraction * vy
            separation = math.hypot(x - px, y - py)
            if separation < distance:
                distance = separation
                cx, cy = self._chart_center
                angle = math.degrees(math.atan2(px - cx, cy - py)) % 360
                closest = (tws, angle)
        return closest

    def draw_polar(self, cr, width, height):
        self._point_positions = []
        self._curve_segments = []
        cr.set_source_rgb(0.15, 0.17, 0.20)
        cr.paint()
        if not self.polar:
            return
        max_r, margin = 80.0, 25.0
        logical_w = (2 if self.full_circle else 1) * max_r + 2 * margin
        logical_h = 2 * max_r + 2 * margin
        s = min(width / logical_w, height / logical_h)
        if s <= 0:
            return
        dx = (width - logical_w * s) / 2
        dy = (height - logical_h * s) / 2
        cr.save()
        cr.translate(dx, dy)
        cr.scale(s, s)
        cx = margin + (max_r if self.full_circle else 0)
        cy = max_r + margin
        self._chart_center = (dx + cx * s, dy + cy * s)
        max_speed = max(
            (v for row in self.polar.speed_table for v in row if math.isfinite(v)),
            default=1,
        )
        max_speed = max(max_speed, max((p.speed for p in self.samples), default=0), 1)
        # Boat speed defines the radius for both the curves and observations.
        ring_step = max(1, math.ceil(max_speed / 8))
        max_speed = math.ceil(max_speed / ring_step) * ring_step
        speed_scale = max_r / max_speed
        self._draw_grid(cr, cx, cy, max_r, max_speed, ring_step, speed_scale)
        self._draw_curves(cr, cx, cy, speed_scale, s, dx, dy)
        self._draw_samples(cr, cx, cy, speed_scale, s, dx, dy)
        cr.restore()
        self._draw_hover(cr, width, height)

    def _draw_hover(self, cr, width, height):
        # Draw on pointer motion instead of waiting for GTK's tooltip timeout.
        if self.hover_text is None or self.mousePos is None:
            return
        cr.save()
        cr.set_font_size(12)
        lines = self.hover_text.splitlines()
        box_w = max(cr.text_extents(line)[2] for line in lines) + 16
        box_h = 18 * len(lines) + 12
        x = min(self.mousePos[0] + 14, width - box_w - 4)
        y = min(self.mousePos[1] + 18, height - box_h - 4)
        x, y = max(4, x), max(4, y)
        cr.set_source_rgba(0.06, 0.07, 0.09, 0.97)
        cr.rectangle(x, y, box_w, box_h)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.4)
        cr.set_line_width(1)
        cr.stroke()
        cr.set_source_rgb(1, 1, 1)
        for i, line in enumerate(lines):
            cr.move_to(x + 8, y + 19 + i * 18)
            cr.show_text(line)
        cr.restore()

    def _draw_grid(self, cr, cx, cy, max_r, max_speed, ring_step, speed_scale):
        for speed in range(ring_step, int(max_speed) + 1, ring_step):
            r = speed * speed_scale
            cr.set_line_width(0.2)
            cr.set_source_rgba(1, 1, 1, 0.15)
            cr.set_dash([2, 2])
            cr.arc(
                cx,
                cy,
                r,
                -math.pi / 2,
                3 * math.pi / 2 if self.full_circle else math.pi / 2,
            )
            cr.stroke()
            cr.set_dash([])
            cr.set_source_rgba(1, 1, 1, 0.6)
            cr.set_font_size(5)
            cr.move_to(cx + 2, cy - r + 5)
            cr.show_text(f"{speed} kt")
        for angle in range(0, 360 if self.full_circle else 181, 30):
            rad = math.radians(angle)
            cr.set_line_width(0.2)
            cr.set_source_rgba(1, 1, 1, 0.15)
            cr.move_to(cx, cy)
            cr.line_to(cx + math.sin(rad) * max_r, cy - math.cos(rad) * max_r)
            cr.stroke()
            cr.set_font_size(5)
            cr.set_source_rgba(1, 1, 1, 0.65)
            cr.move_to(
                cx + math.sin(rad) * (max_r + 9) - 4,
                cy - math.cos(rad) * (max_r + 9) + 2,
            )
            cr.show_text(f"{angle}°")

    def _draw_curves(self, cr, cx, cy, speed_scale, scale, dx, dy):
        assert self.polar is not None
        polar = self.polar
        indices = sorted(
            range(len(polar.tws)),
            key=lambda i: polar.tws[i] == self.hover_wind,
        )
        for i in indices:
            if self.wind_filter is not None and i != self.wind_filter:
                continue
            color = POLAR_COLORS[i % len(POLAR_COLORS)]
            focused = (
                self.polar.tws[i] == self.hover_wind
                or self.polar.tws[i] == self.isolated_wind
            )
            dimmed = (
                self.hover_wind is not None or self.isolated_wind is not None
            ) and not focused
            cr.set_source_rgba(*color, 0.15 if dimmed else 0.9)
            cr.set_line_width(1.0 if focused else 0.7)
            for side in ((1, -1) if self.full_circle else (1,)):
                previous = None
                for angle, row in zip(self.polar.twa, self.polar.speed_table):
                    if i >= len(row) or not math.isfinite(row[i]) or row[i] <= 0:
                        previous = None
                        continue
                    px = cx + side * speed_scale * row[i] * math.sin(angle)
                    py = cy - speed_scale * row[i] * math.cos(angle)
                    if previous is None:
                        cr.move_to(px, py)
                    else:
                        cr.line_to(px, py)
                        self._curve_segments.append(
                            (
                                dx + previous[0] * scale,
                                dy + previous[1] * scale,
                                dx + px * scale,
                                dy + py * scale,
                                self.polar.tws[i],
                            )
                        )
                    previous = (px, py)
                cr.stroke()

    def _draw_samples(self, cr, cx, cy, speed_scale, scale, dx, dy):
        assert self.polar is not None
        for sample in self.samples:
            # Skip observations from when the boat is not moving, or when there
            # is no wind (a polar with zero wind has no meaningful position).
            if sample.speed <= 0 or sample.tws <= 0:
                continue
            band = wind_band(sample.tws, self.polar.tws)
            if band is None or (
                self.wind_filter is not None and band != self.wind_filter
            ):
                continue
            # The zero-wind polar band includes small positive readings below
            # its midpoint boundary; they are not useful performance samples.
            if self.polar.tws[band] <= 0:
                continue
            # Isolation means show only that wind band's observations, rather
            # than merely dimming the other points.
            if (
                self.isolated_wind is not None
                and self.polar.tws[band] != self.isolated_wind
            ):
                continue
            angle = math.radians(sample.twa)
            px = cx + speed_scale * sample.speed * math.sin(angle)
            py = cy - speed_scale * sample.speed * math.cos(angle)
            color = POLAR_COLORS[band % len(POLAR_COLORS)]
            band_tws = self.polar.tws[band]
            focused = band_tws == self.hover_wind or band_tws == self.isolated_wind
            dimmed = (
                self.hover_wind is not None or self.isolated_wind is not None
            ) and not focused
            cr.set_source_rgba(*color, 0.15 if dimmed else 0.8)
            cr.arc(px, py, 0.65, 0, 2 * math.pi)
            cr.fill()
            self._point_positions.append((dx + px * scale, dy + py * scale, sample))
