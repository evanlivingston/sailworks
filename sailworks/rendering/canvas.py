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

"""The drawing seam for CPU-rendered overlays, backed by QPainter.

Drawing modules call ``Canvas`` so one code path paints every overlay. The
seam used to carry a second, pure-cairo backend during issue #267; that
backend is gone and this module is Qt-only.

Qt is imported on first use, never at module import time, so a module that
only needs the seam's interface can be imported in a process that never loads
PySide6.
"""

import math
import sys
from collections import namedtuple

# Qt is imported on first use by ``QPainterCanvas``, never at module import
# time, so a module that only needs the seam's interface can be imported in a
# process that never loads PySide6.
Qt = None
QPointF = None
QRectF = None
QBrush = None
QColor = None
QFont = None
QFontMetricsF = None
QImage = None
QPainter = None
QPainterPath = None
QPen = None


def _qt():
    """Bind the Qt names once, on the first call."""
    global Qt, QPointF, QRectF, QBrush, QColor, QFont, QFontMetricsF, QImage
    global QPainter, QPainterPath, QPen
    if QPainter is not None:
        return
    from PySide6.QtCore import QPointF as _QPointF, QRectF as _QRectF, Qt as _Qt
    from PySide6.QtGui import (
        QBrush as _QBrush,
        QColor as _QColor,
        QFont as _QFont,
        QFontMetricsF as _QFontMetricsF,
        QImage as _QImage,
        QPainter as _QPainter,
        QPainterPath as _QPainterPath,
        QPen as _QPen,
    )

    Qt = _Qt
    QPointF = _QPointF
    QRectF = _QRectF
    QBrush = _QBrush
    QColor = _QColor
    QFont = _QFont
    QFontMetricsF = _QFontMetricsF
    QImage = _QImage
    QPainter = _QPainter
    QPainterPath = _QPainterPath
    QPen = _QPen


# Same six attributes cairo's ``TextExtents`` exposed, so the label code reads
# them by name without a wrapper object.
TextExtents = namedtuple(
    "TextExtents", "width height x_bearing y_bearing x_advance y_advance"
)

# Line joins, named rather than an enum's constants.
JOIN_MITER = "miter"
JOIN_ROUND = "round"
JOIN_BEVEL = "bevel"

# Fill rules, named rather than an enum's constants.
FILL_EVEN_ODD = "even-odd"
FILL_WINDING = "winding"

# Line caps, named rather than an enum's constants.
CAP_BUTT = "butt"
CAP_ROUND = "round"
CAP_SQUARE = "square"

# Font slants and weights, named rather than an enum's constants. ``None``
# means the default, which is normal/normal.
SLANT_NORMAL = "normal"
SLANT_ITALIC = "italic"

WEIGHT_NORMAL = "normal"
WEIGHT_BOLD = "bold"


class CanvasImage:
    """A decoded raster image, carried between renderer threads and widgets.

    The carrier owns a ``QImage`` (or wraps one the caller owns) and never
    converts on access, so a frame built by a worker is the same object the
    widget layer blits.

    ``width``/``height`` are logical (user-space) pixels: the image's pixel
    size divided by ``device_scale``. The caller keeps the pixel buffer alive
    for as long as the carrier can be drawn; this object does not own it.
    """

    def __init__(self, width, height, device_scale=1.0, qimage=None, buffer=None):
        self._width = width
        self._height = height
        self._device_scale = device_scale
        self._qimage = qimage
        # Kept alive for images built over a caller-provided pixel buffer.
        self._buffer = buffer
        # Converting to premultiplied ARGB32 is the only cached derived view.
        self._premultiplied_image = None

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    def device_scale(self):
        return self._device_scale

    # -- The surface API the renderers call -------------------------------
    #
    # These methods mirror the part of ``cairo.ImageSurface`` the renderers
    # already read, so a renderer needs only a different constructor.

    def get_width(self):
        """Pixel width of the backing image.

        This is the device pixel count, not the logical width: every existing
        call site builds a ``QImage`` from ``get_data()``/``get_stride()`` and
        therefore needs device pixels.
        """
        return self._qimage.width()

    def get_height(self):
        """Pixel height; device pixels, for the reason in :meth:`get_width`."""
        return self._qimage.height()

    def get_stride(self):
        """Bytes per row of :meth:`get_data`."""
        return self._premultiplied().bytesPerLine()

    def get_data(self):
        """Pixel bytes in premultiplied BGRA on a little-endian machine.

        That is what cairo's ``ImageSurface.get_data`` returned for
        ``FORMAT_ARGB32``, and what Qt's ``Format_ARGB32_Premultiplied``
        ``constBits`` stores: both are the packed ``0xAARRGGBB`` word, so the
        byte order is BGRA little-endian / ARGB big-endian. The order is
        asserted in ``test_canvas_equivalence`` rather than assumed. An image
        in another format is converted once, here, so callers always receive
        premultiplied ARGB32.
        """
        return self._premultiplied().constBits()

    def _premultiplied(self):
        """The Qt image in ``Format_ARGB32_Premultiplied``, converting once."""
        if (
            self._premultiplied_image is None
            and self._qimage.format() != QImage.Format.Format_ARGB32_Premultiplied
        ):
            self._premultiplied_image = self._qimage.convertToFormat(
                QImage.Format.Format_ARGB32_Premultiplied
            )
        return self._premultiplied_image or self._qimage

    def flush(self):
        """Flush deferred work.

        QPainter paints into the image synchronously, so there is nothing to
        flush; the method remains because callers end a composition with it.
        """

    def set_device_scale(self, scale):
        """Scale logical pixels to device pixels."""
        if scale == self._device_scale:
            return
        self._qimage.setDevicePixelRatio(scale)
        self._device_scale = scale
        self._width = self.get_width() / scale
        self._height = self.get_height() / scale

    @classmethod
    def from_qimage(cls, image):
        """Wrap a ``QImage``; the image and its buffer must outlive this object."""
        scale = image.devicePixelRatio()
        return cls(
            image.width() / scale,
            image.height() / scale,
            scale,
            qimage=image,
        )

    @classmethod
    def from_png_bytes(cls, data):
        """Decode PNG bytes into a carrier at its natural pixel size.

        The carrier reports ``device_scale`` 1, so a caller that needs a
        different logical size passes it to :meth:`Canvas.draw_image`.

        ``QImage.fromData`` may hand back non-premultiplied ``ARGB32``; it is
        converted here so every consumer, worker thread or widget, receives
        the one premultiplied format the rest of the seam assumes.
        """
        _qt()
        image = QImage.fromData(data)
        if image.isNull():
            raise ValueError("Unable to decode image bytes")
        if image.format() != QImage.Format.Format_ARGB32_Premultiplied:
            image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        return cls.from_qimage(image)

    @classmethod
    def from_rgba(cls, width, height, data, device_scale=1.0):
        """Build a carrier from a row-major, **premultiplied** RGBA byte array.

        ``data`` holds four bytes per pixel in R, G, B, A order, row-major,
        with no padding between rows; a contiguous ``numpy.uint8`` array of
        shape ``(height, width, 4)`` is accepted. ``width``/``height`` are the
        pixel dimensions of that array and ``device_scale`` divides them into
        the logical size the carrier reports.

        The image stores ARGB32, i.e. a packed ``0xAARRGGBB`` word, so the
        memory order depends only on the machine's endianness (BGRA on
        little-endian, ARGB on big-endian). The caller's RGBA rows are
        reordered once, here, and the resulting buffer is held alive by the
        carrier. The channel order is proven by ``test_canvas_equivalence``
        rather than assumed.
        """
        raw = data.tobytes() if hasattr(data, "tobytes") else bytes(data)
        expected = width * height * 4
        if len(raw) != expected:
            raise ValueError(
                f"RGBA data is {len(raw)} bytes, expected {expected} "
                f"for {width}x{height}"
            )
        buffer = bytearray(expected)
        if sys.byteorder == "little":
            # ARGB32 low byte first: B, G, R, A.
            buffer[0::4] = raw[2::4]
            buffer[1::4] = raw[1::4]
            buffer[2::4] = raw[0::4]
            buffer[3::4] = raw[3::4]
        else:
            # ARGB32 high byte first: A, R, G, B.
            buffer[0::4] = raw[3::4]
            buffer[1::4] = raw[0::4]
            buffer[2::4] = raw[1::4]
            buffer[3::4] = raw[2::4]
        logical_width = width / device_scale
        logical_height = height / device_scale
        _qt()
        # ``QImage`` does not own the Python buffer it is built over, and
        # the binding does not guarantee a reference to it, so copy the
        # pixels into an image Qt owns.
        raw_buffer = bytes(buffer)
        image = QImage(
            raw_buffer,
            width,
            height,
            width * 4,
            QImage.Format.Format_ARGB32_Premultiplied,
        ).copy()
        image.setDevicePixelRatio(device_scale)
        return cls(
            logical_width,
            logical_height,
            device_scale,
            qimage=image,
            buffer=raw_buffer,
        )

    @classmethod
    def new(cls, width, height, device_scale=1.0):
        """Create a blank surface of ``width`` x ``height`` logical pixels."""
        return cls.empty(width, height, device_scale)

    @classmethod
    def empty(cls, width, height, device_scale=1.0):
        """Create a blank image of ``width`` x ``height`` logical pixels.

        The pixel buffer is ``width * device_scale`` by ``height *
        device_scale``; the carrier keeps the requested logical size so a
        fractional device scale cannot drift it. The image is transparent and
        can be drawn onto through a canvas built on it.
        """
        pixel_width = max(1, int(round(width * device_scale)))
        pixel_height = max(1, int(round(height * device_scale)))
        _qt()
        image = QImage(
            pixel_width, pixel_height, QImage.Format.Format_ARGB32_Premultiplied
        )
        image.fill(Qt.transparent)
        image.setDevicePixelRatio(device_scale)
        return cls(width, height, device_scale, qimage=image)

    @property
    def qimage(self):
        """The backing ``QImage``."""
        return self._qimage


class Canvas:
    """The drawing operations the dashboard and numeric gauges need.

    Grow this class only when the next migration slice needs another
    operation; do not add speculative methods.
    """

    @classmethod
    def for_image(cls, image):
        """A canvas painting onto the ``CanvasImage``'s ``QImage``.

        The renderers build an image with :meth:`CanvasImage.new` and ask for
        the canvas here.
        """
        return QPainterCanvas(image.qimage)

    def finish(self):
        """Complete the image: Qt ends the painter."""

    def save(self):
        raise NotImplementedError

    def restore(self):
        raise NotImplementedError

    def set_source_rgba(self, r, g, b, a):
        raise NotImplementedError

    def set_source_rgb(self, r, g, b):
        raise NotImplementedError

    def set_line_width(self, width):
        raise NotImplementedError

    def set_line_join(self, join):
        raise NotImplementedError

    def set_line_cap(self, cap):
        raise NotImplementedError

    def set_dash(self, pattern):
        raise NotImplementedError

    def set_font_size(self, size):
        raise NotImplementedError

    def set_font_face(self, family, slant=None, weight=None):
        raise NotImplementedError

    def translate(self, x, y):
        raise NotImplementedError

    def rotate(self, radians):
        raise NotImplementedError

    def scale(self, sx, sy):
        raise NotImplementedError

    def draw_image(
        self,
        image,
        x,
        y,
        width=None,
        height=None,
        alpha=1.0,
        smooth=False,
        pad=False,
    ):
        """Draw a :class:`CanvasImage` at ``(x, y)``.

        ``width``/``height`` scale the image into that destination size; an
        omitted dimension keeps the image's logical size. ``smooth`` selects
        bilinear (True) or nearest (False) sampling. ``smooth=None`` asks for
        the pre-seam default: Cairo's untouched ``FILTER_GOOD`` (what pre-seam
        code inherited by never calling ``set_filter``), mapped to Qt's
        smoothing transform. ``pad`` extends the edge pixels when the
        pattern is sampled outside the image; ``False`` leaves the area
        outside it transparent.
        """
        raise NotImplementedError

    def new_sub_path(self):
        raise NotImplementedError

    def new_path(self):
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

    def set_fill_rule(self, rule):
        raise NotImplementedError

    def fill(self):
        raise NotImplementedError

    def fill_preserve(self):
        raise NotImplementedError

    def stroke(self):
        raise NotImplementedError

    def paint(self):
        raise NotImplementedError

    def stroke_preserve(self):
        raise NotImplementedError

    def clip(self):
        raise NotImplementedError

    def device_scale(self):
        raise NotImplementedError

    def text_extents(self, text):
        raise NotImplementedError

    def show_text(self, text):
        raise NotImplementedError

    def text_path(self, text):
        """Append the glyph outlines of ``text`` to the current path.

        The path then carries the text, so ``fill``/``stroke``/``stroke_preserve``
        can draw it — the outlined isobar labels depend on this.
        """
        raise NotImplementedError


class QPainterCanvas(Canvas):
    """Paints onto an ARGB32-premultiplied ``QImage`` through ``QPainter``.

    The image is cleared to transparent on construction and is complete once
    ``finish()`` has been called.
    """

    def __init__(self, image):
        _qt()
        self._image = image
        self._image.fill(Qt.transparent)
        self._painter = QPainter(image)
        self._painter.setRenderHint(QPainter.Antialiasing, True)
        self._painter.setRenderHint(QPainter.TextAntialiasing, True)
        self._fill_rule = Qt.WindingFill
        self._path = self._new_path(self._fill_rule)
        self._started = False
        self._color = QColor(0, 0, 0, 255)
        self._line_width = 1.0
        self._join = Qt.MiterJoin
        self._cap = Qt.FlatCap
        self._dash = []
        self._font = QFont()
        self._current = QPointF(0.0, 0.0)

    @staticmethod
    def _new_path(fill_rule=None):
        _qt()
        if fill_rule is None:
            # Cairo's default fill rule is winding (non-zero).
            fill_rule = Qt.WindingFill
        path = QPainterPath()
        # Cairo's default fill rule is winding (non-zero); Qt defaults to
        # odd-even, so set it explicitly.
        path.setFillRule(fill_rule)
        return path

    def finish(self):
        """End the painter so every operation has reached the image."""
        if self._painter.isActive():
            self._painter.end()

    def save(self):
        self._painter.save()

    def restore(self):
        self._painter.restore()

    @staticmethod
    def _channel(value):
        """Map a Cairo float channel onto 8 bits the way Cairo does.

        Cairo rounds the float to 16 bits and the 8-bit target then drops the
        low byte, so ``0.95`` becomes 243 rather than the 242 that
        ``round(0.95 * 255)`` gives, and ``0.7`` becomes 179 rather than 178.
        ``QColor.fromRgbF`` follows neither rule, which cost one blue level on
        every DEPARE fill. Reproduce Cairo's two steps, then clamp.
        """
        level = int(value * 65535.0 + 0.5) >> 8
        if level < 0:
            return 0
        if level > 255:
            return 255
        return level

    def set_source_rgba(self, r, g, b, a):
        self._color = QColor(
            self._channel(r),
            self._channel(g),
            self._channel(b),
            self._channel(a),
        )

    def set_source_rgb(self, r, g, b):
        self.set_source_rgba(r, g, b, 1.0)

    def set_line_width(self, width):
        self._line_width = width

    def set_line_join(self, join):
        self._join = {
            JOIN_MITER: Qt.MiterJoin,
            JOIN_ROUND: Qt.RoundJoin,
            JOIN_BEVEL: Qt.BevelJoin,
        }[join]

    def set_line_cap(self, cap):
        self._cap = {
            CAP_BUTT: Qt.FlatCap,
            CAP_ROUND: Qt.RoundCap,
            CAP_SQUARE: Qt.SquareCap,
        }[cap]

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

    def set_font_face(self, family, slant=None, weight=None):
        font = QFont(self._font)
        font.setFamily(family)
        if slant is not None:
            font.setStyle(
                {
                    SLANT_NORMAL: QFont.Style.StyleNormal,
                    SLANT_ITALIC: QFont.Style.StyleItalic,
                }[slant]
            )
        if weight is not None:
            font.setWeight(
                {
                    WEIGHT_NORMAL: QFont.Weight.Normal,
                    WEIGHT_BOLD: QFont.Weight.Bold,
                }[weight]
            )
        self._font = font

    def translate(self, x, y):
        self._painter.translate(x, y)

    def rotate(self, radians):
        self._painter.rotate(math.degrees(radians))

    def scale(self, sx, sy):
        self._painter.scale(sx, sy)

    def draw_image(
        self,
        image,
        x,
        y,
        width=None,
        height=None,
        alpha=1.0,
        smooth=False,
        pad=False,
    ):
        """Draw a :class:`CanvasImage` through the painter's current transform.

        Cairo's extend-pad paints the nearest edge colour outside the source
        bounds; Qt has no equivalent pattern extend. For the scaled blits this
        seam is used for, every sample inside the destination rect reads
        inside the source, so clipping to that rect reproduces the visible
        interior exactly. The edge colour is not painted outside the rect, so
        ``pad=True`` is a faithful emulation inside the destination and only an
        approximation beyond it.
        """
        qimage = image.qimage
        target_width = image.width if width is None else width
        target_height = image.height if height is None else height
        target = QRectF(x, y, target_width, target_height)
        self._painter.save()
        self._painter.setOpacity(alpha)
        if smooth is None:
            # Cairo's untouched default is FILTER_GOOD, a smoothing filter.
            # Map an unspecified filter to Qt's smoothing transform rather
            # than to nearest, which would visibly block a scaled raster.
            self._painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        else:
            self._painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
        if pad:
            self._painter.setClipRect(target, Qt.IntersectClip)
        self._painter.drawImage(target, qimage)
        self._painter.restore()

    def new_sub_path(self):
        # Cairo starts a fresh subpath with no current point. QPainterPath
        # always has one, so the closest equivalent for this operation set is
        # an empty path; both modules call new_sub_path at the start of a path.
        self._path = self._new_path(self._fill_rule)
        self._started = False

    def new_path(self):
        # cairo_new_path clears the current path entirely; the fill rule is
        # context state, so it survives and seeds the fresh path.
        self._path = self._new_path(self._fill_rule)
        self._started = False

    def set_fill_rule(self, rule):
        # Cairo applies the rule at fill time; QPainterPath stores it on the
        # path, so record it for later paths and apply it to the current one.
        self._fill_rule = {
            FILL_EVEN_ODD: Qt.OddEvenFill,
            FILL_WINDING: Qt.WindingFill,
        }[rule]
        self._path.setFillRule(self._fill_rule)

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
        self._path = self._new_path(self._fill_rule)
        self._started = False

    def fill_preserve(self):
        self._painter.fillPath(self._path, QBrush(self._color))

    def _stroke_path(self):
        pen = QPen(self._color)
        pen.setWidthF(self._line_width)
        pen.setCapStyle(self._cap)
        pen.setJoinStyle(self._join)
        if self._dash:
            pen.setDashPattern(self._dash)
        else:
            pen.setStyle(Qt.SolidLine)
        self._painter.strokePath(self._path, pen)

    def stroke(self):
        self._stroke_path()
        self._path = self._new_path(self._fill_rule)
        self._started = False

    def paint(self):
        """Fill the whole clip with the current source, in device space.

        Cairo's ``paint`` ignores the CTM, so the transform is reset before
        filling and restored afterwards.
        """
        self._painter.save()
        self._painter.resetTransform()
        self._painter.fillRect(self._image.rect(), self._color)
        self._painter.restore()

    def stroke_preserve(self):
        # cairo_stroke_preserve leaves the current path intact.
        self._stroke_path()

    def clip(self):
        self._painter.setClipPath(self._path, Qt.IntersectClip)
        # cairo_clip consumes the current path (cairo_clip_preserve is the one
        # that keeps it), so drop it here too.
        self._path = self._new_path(self._fill_rule)
        self._started = False

    def device_scale(self):
        return self._image.devicePixelRatio()

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

    def text_path(self, text):
        """Append glyph outlines at the current point, as ``cairo_text_path``.

        ``QPainterPath.addText`` places baseline text at an absolute origin, so
        the glyphs are built at the origin and translated to the current point
        before being merged into the active path. Cairo leaves the current
        point at the end of the text, and so does this.
        """
        glyphs = QPainterPath()
        glyphs.addText(0.0, 0.0, self._font, text)
        glyphs.translate(self._current.x(), self._current.y())
        self._path.addPath(glyphs)
        self._path.setFillRule(self._fill_rule)
        self._started = True
        self._current = glyphs.currentPosition()
