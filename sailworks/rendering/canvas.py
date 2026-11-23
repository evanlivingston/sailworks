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

"""Backend-neutral drawing seam for CPU-rendered overlays.

This module is migration scaffolding, not a permanent abstraction. Drawing
modules call ``Canvas`` so the same code can run through Cairo today and
through QPainter during Phase 2 of issue #267. The end state is a single
backend: ``CairoCanvas`` is deleted in Phase 5.

``CairoCanvas`` is a pure delegation to a ``cairo.Context`` and must stay
byte-for-byte identical to calling cairo directly. Importing this module does
not import cairo; the caller owns the context and the cairo import, so a
QPainter-only path never loads it.
"""

import math
from collections import namedtuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
)

# Same six attributes cairo's TextExtents exposes, so callers stay backend
# agnostic without a wrapper object.
TextExtents = namedtuple(
    "TextExtents", "width height x_bearing y_bearing x_advance y_advance"
)

# Line joins, named rather than re-exporting either backend's constants.
JOIN_MITER = "miter"
JOIN_ROUND = "round"
JOIN_BEVEL = "bevel"


class Canvas:
    """The drawing operations the dashboard and numeric gauges need.

    Grow this class only when the next migration slice needs another
    operation; do not add speculative methods.
    """

    def save(self):
        raise NotImplementedError

    def restore(self):
        raise NotImplementedError

    def set_source_rgba(self, r, g, b, a):
        raise NotImplementedError

    def set_line_width(self, width):
        raise NotImplementedError

    def set_line_join(self, join):
        raise NotImplementedError

    def set_dash(self, pattern):
        raise NotImplementedError

    def set_font_size(self, size):
        raise NotImplementedError

    def translate(self, x, y):
        raise NotImplementedError

    def rotate(self, radians):
        raise NotImplementedError

    def new_sub_path(self):
        raise NotImplementedError

    def move_to(self, x, y):
        raise NotImplementedError

    def line_to(self, x, y):
        raise NotImplementedError

    def arc(self, xc, yc, radius, angle1, angle2):
        raise NotImplementedError

    def rectangle(self, x, y, width, height):
        raise NotImplementedError

    def close_path(self):
        raise NotImplementedError

    def fill(self):
        raise NotImplementedError

    def fill_preserve(self):
        raise NotImplementedError

    def stroke(self):
        raise NotImplementedError

    def clip(self):
        raise NotImplementedError

    def text_extents(self, text):
        raise NotImplementedError

    def show_text(self, text):
        raise NotImplementedError


class CairoCanvas(Canvas):
    """Pure delegation to a ``cairo.Context``; output must not change."""

    def __init__(self, context):
        self._cr = context

    def save(self):
        self._cr.save()

    def restore(self):
        self._cr.restore()

    def set_source_rgba(self, r, g, b, a):
        self._cr.set_source_rgba(r, g, b, a)

    def set_line_width(self, width):
        self._cr.set_line_width(width)

    def set_line_join(self, join):
        # Imported lazily so importing this module never imports cairo.
        import cairo

        self._cr.set_line_join(
            {
                JOIN_MITER: cairo.LINE_JOIN_MITER,
                JOIN_ROUND: cairo.LINE_JOIN_ROUND,
                JOIN_BEVEL: cairo.LINE_JOIN_BEVEL,
            }[join]
        )

    def set_dash(self, pattern):
        self._cr.set_dash(list(pattern))

    def set_font_size(self, size):
        self._cr.set_font_size(size)

    def translate(self, x, y):
        self._cr.translate(x, y)

    def rotate(self, radians):
        self._cr.rotate(radians)

    def new_sub_path(self):
        self._cr.new_sub_path()

    def move_to(self, x, y):
        self._cr.move_to(x, y)

    def line_to(self, x, y):
        self._cr.line_to(x, y)

    def arc(self, xc, yc, radius, angle1, angle2):
        self._cr.arc(xc, yc, radius, angle1, angle2)

    def rectangle(self, x, y, width, height):
        self._cr.rectangle(x, y, width, height)

    def close_path(self):
        self._cr.close_path()

    def fill(self):
        self._cr.fill()

    def fill_preserve(self):
        self._cr.fill_preserve()

    def stroke(self):
        self._cr.stroke()

    def clip(self):
        self._cr.clip()

    def text_extents(self, text):
        return self._cr.text_extents(text)

    def show_text(self, text):
        self._cr.show_text(text)


class QPainterCanvas(Canvas):
    """Paints onto an ARGB32-premultiplied ``QImage`` through ``QPainter``.

    The image is cleared to transparent on construction and is complete once
    ``finish()`` has been called. Cairo has no equivalent of ``finish`` because
    a surface is flushed by its owner.
    """

    def __init__(self, image):
        self._image = image
        self._image.fill(Qt.transparent)
        self._painter = QPainter(image)
        self._painter.setRenderHint(QPainter.Antialiasing, True)
        self._painter.setRenderHint(QPainter.TextAntialiasing, True)
        self._path = self._new_path()
        self._started = False
        self._color = QColor.fromRgbF(0.0, 0.0, 0.0, 1.0)
        self._line_width = 1.0
        self._join = Qt.MiterJoin
        self._dash = []
        self._font = QFont()
        self._current = QPointF(0.0, 0.0)

    @staticmethod
    def _new_path():
        path = QPainterPath()
        # Cairo's default fill rule is winding (non-zero); Qt defaults to
        # odd-even, so set it explicitly.
        path.setFillRule(Qt.WindingFill)
        return path

    def finish(self):
        """End the painter so every operation has reached the image."""
        if self._painter.isActive():
            self._painter.end()

    def save(self):
        self._painter.save()

    def restore(self):
        self._painter.restore()

    def set_source_rgba(self, r, g, b, a):
        self._color = QColor.fromRgbF(r, g, b, a)

    def set_line_width(self, width):
        self._line_width = width

    def set_line_join(self, join):
        self._join = {
            JOIN_MITER: Qt.MiterJoin,
            JOIN_ROUND: Qt.RoundJoin,
            JOIN_BEVEL: Qt.BevelJoin,
        }[join]

    def set_dash(self, pattern):
        """Set a Cairo-style dash pattern, mapping it onto Qt's pen units.

        Cairo measures dash lengths in user-space units, independent of the pen
        width. ``QPen.setDashPattern`` measures them in multiples of the pen
        width, so each length is divided by the current width. A zero or unset
        width is treated as 1.0, Qt's default width and the closest mapping
        that cannot divide by zero. An empty pattern means a solid line. Cairo
        repeats an odd-length pattern while Qt ignores one, so an odd list is
        doubled; ``[8.0]`` keeps its 8-on/8-off meaning.
        """
        if not pattern:
            self._dash = []
            return
        width = self._line_width
        if width <= 0:
            width = 1.0
        values = [float(length) / width for length in pattern]
        if len(values) % 2:
            values = values * 2
        self._dash = values

    def set_font_size(self, size):
        font = QFont(self._font)
        font.setPixelSize(max(1, int(round(size))))
        self._font = font

    def translate(self, x, y):
        self._painter.translate(x, y)

    def rotate(self, radians):
        self._painter.rotate(math.degrees(radians))

    def new_sub_path(self):
        # Cairo starts a fresh subpath with no current point. QPainterPath
        # always has one, so the closest equivalent for this operation set is
        # an empty path; both modules call new_sub_path at the start of a path.
        self._path = self._new_path()
        self._started = False

    def move_to(self, x, y):
        self._path.moveTo(x, y)
        self._started = True
        self._current = QPointF(x, y)

    def line_to(self, x, y):
        self._path.lineTo(x, y)
        self._started = True
        self._current = QPointF(x, y)

    def arc(self, xc, yc, radius, angle1, angle2):
        rect = QRectF(xc - radius, yc - radius, radius * 2.0, radius * 2.0)
        if not self._started:
            # QPainterPath always has a current point at (0, 0), so arcTo would
            # draw a line from the origin. Cairo's arc after new_sub_path has
            # no current point; start the subpath at the arc itself.
            self._path.moveTo(
                xc + radius * math.cos(angle1), yc + radius * math.sin(angle1)
            )
            self._started = True
        # Cairo measures radians clockwise with y down; Qt measures degrees
        # counter-clockwise, so both the start and the sweep flip sign.
        self._path.arcTo(rect, -math.degrees(angle1), -math.degrees(angle2 - angle1))
        self._current = QPointF(
            xc + radius * math.cos(angle2), yc + radius * math.sin(angle2)
        )

    def rectangle(self, x, y, width, height):
        self._path.addRect(QRectF(x, y, width, height))
        self._started = True

    def close_path(self):
        self._path.closeSubpath()

    def fill(self):
        self._painter.fillPath(self._path, QBrush(self._color))
        self._path = self._new_path()
        self._started = False

    def fill_preserve(self):
        self._painter.fillPath(self._path, QBrush(self._color))

    def stroke(self):
        pen = QPen(self._color)
        pen.setWidthF(self._line_width)
        # Cairo's defaults: butt caps, miter joins.
        pen.setCapStyle(Qt.FlatCap)
        pen.setJoinStyle(self._join)
        if self._dash:
            pen.setDashPattern(self._dash)
        else:
            pen.setStyle(Qt.SolidLine)
        self._painter.strokePath(self._path, pen)
        self._path = self._new_path()
        self._started = False

    def clip(self):
        self._painter.setClipPath(self._path, Qt.IntersectClip)
        # cairo_clip consumes the current path (cairo_clip_preserve is the one
        # that keeps it), so drop it here too.
        self._path = self._new_path()
        self._started = False

    def text_extents(self, text):
        metrics = QFontMetricsF(self._font)
        bounds = metrics.boundingRect(text)
        return TextExtents(
            width=bounds.width(),
            height=bounds.height(),
            x_bearing=0.0,
            y_bearing=bounds.y(),
            x_advance=metrics.horizontalAdvance(text),
            y_advance=0.0,
        )

    def show_text(self, text):
        self._painter.setFont(self._font)
        self._painter.setPen(self._color)
        self._painter.drawText(self._current, text)
