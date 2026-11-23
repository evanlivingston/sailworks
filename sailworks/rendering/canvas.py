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

import io
import math
import sys
from collections import namedtuple

# Qt is imported on first use by ``QPainterCanvas``, never at module import
# time, so a module that only needs the seam's interface (or the Cairo
# backend) can be imported in a process that never loads PySide6. This mirrors
# how the Cairo backend binds pycairo.
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


# Same six attributes cairo's TextExtents exposes, so callers stay backend
# agnostic without a wrapper object.
TextExtents = namedtuple(
    "TextExtents", "width height x_bearing y_bearing x_advance y_advance"
)

# Line joins, named rather than re-exporting either backend's constants.
JOIN_MITER = "miter"
JOIN_ROUND = "round"
JOIN_BEVEL = "bevel"

# Fill rules, named rather than re-exporting either backend's constants.
FILL_EVEN_ODD = "even-odd"
FILL_WINDING = "winding"

# Line caps, named rather than re-exporting either backend's constants.
CAP_BUTT = "butt"
CAP_ROUND = "round"
CAP_SQUARE = "square"

# Font slants and weights, named rather than re-exporting either backend's
# constants. ``None`` means the backend's default, which is normal/normal.
SLANT_NORMAL = "normal"
SLANT_ITALIC = "italic"

WEIGHT_NORMAL = "normal"
WEIGHT_BOLD = "bold"


class CanvasImage:
    """A decoded raster image, carried between the two canvas backends.

    The carrier keeps whichever backend object built it and never converts on
    access, so Qt code never receives a cairo surface and Cairo code never
    receives a ``QImage``. Asking for the other backend's view raises instead
    of silently copying, which keeps a wrong-backend draw a visible error.

    ``width``/``height`` are logical (user-space) pixels: the backend pixel
    size divided by ``device_scale``. The caller keeps the pixel buffer alive
    for as long as the carrier can be drawn; this object does not own it.
    """

    def __init__(
        self, width, height, device_scale=1.0, surface=None, qimage=None, buffer=None
    ):
        self._width = width
        self._height = height
        self._device_scale = device_scale
        self._surface = surface
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

    @property
    def backend(self):
        """``"cairo"`` or ``"qt"``, whichever backend built this carrier."""
        return "cairo" if self._qimage is None else "qt"

    def device_scale(self):
        return self._device_scale

    # -- The surface API the renderers call -------------------------------
    #
    # These methods mirror the part of ``cairo.ImageSurface`` the renderers
    # and their Qt consumers already read, so a renderer switches backend by
    # changing its constructor and nothing else.

    def get_width(self):
        """Pixel width, as cairo's ``ImageSurface.get_width`` reports it.

        This is the device pixel count, not the logical width: every existing
        call site builds a ``QImage`` from ``get_data()``/``get_stride()`` and
        therefore needs device pixels.
        """
        if self._qimage is not None:
            return self._qimage.width()
        return self._surface.get_width()

    def get_height(self):
        """Pixel height; device pixels, for the reason in :meth:`get_width`."""
        if self._qimage is not None:
            return self._qimage.height()
        return self._surface.get_height()

    def get_stride(self):
        """Bytes per row of :meth:`get_data`."""
        if self._qimage is not None:
            return self._premultiplied().bytesPerLine()
        return self._surface.get_stride()

    def get_data(self):
        """Pixel bytes in premultiplied BGRA on a little-endian machine.

        That is what cairo's ``ImageSurface.get_data`` returns for
        ``FORMAT_ARGB32``, and what Qt's ``Format_ARGB32_Premultiplied``
        ``constBits`` stores: both are the packed ``0xAARRGGBB`` word, so the
        byte order is BGRA little-endian / ARGB big-endian. The order is
        asserted in ``test_canvas_equivalence`` rather than assumed. A Qt
        image decoded in another format is converted once, here, so callers
        always receive premultiplied ARGB32.
        """
        if self._qimage is not None:
            return self._premultiplied().constBits()
        return self._surface.get_data()

    def _premultiplied(self):
        """The Qt image in ``Format_ARGB32_Premultiplied``, converting once."""
        if self._qimage is None:
            raise RuntimeError("CanvasImage is cairo-backed, not Qt-backed")
        _qt()
        if (
            self._premultiplied_image is None
            and self._qimage.format() != QImage.Format.Format_ARGB32_Premultiplied
        ):
            self._premultiplied_image = self._qimage.convertToFormat(
                QImage.Format.Format_ARGB32_Premultiplied
            )
        return self._premultiplied_image or self._qimage

    def flush(self):
        """Flush deferred work; Qt paints immediately, cairo may defer."""
        if self._surface is not None:
            self._surface.flush()

    def set_device_scale(self, scale):
        """Scale logical pixels to device pixels, matching cairo's method."""
        if scale == self._device_scale:
            return
        if self._qimage is not None:
            self._qimage.setDevicePixelRatio(scale)
        else:
            self._surface.set_device_scale(scale, scale)
        self._device_scale = scale
        self._width = self.get_width() / scale
        self._height = self.get_height() / scale

    @classmethod
    def from_cairo_surface(cls, surface):
        """Wrap a cairo image surface; the surface must outlive this object."""
        scale = max(surface.get_device_scale())
        return cls(
            surface.get_width() / scale,
            surface.get_height() / scale,
            scale,
            surface=surface,
        )

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
    def from_png_bytes(cls, data, backend="cairo"):
        """Decode PNG bytes into a carrier at its natural pixel size.

        ``backend`` selects the decoder. It defaults to Cairo because that is
        the backend the renderers built on this seam use today; pass ``"qt"``
        to build the Qt view in a Qt-only process. The carrier reports
        ``device_scale`` 1, so a caller that needs a different logical size
        passes it to :meth:`Canvas.draw_image`.
        """
        if backend == "cairo":
            # Imported lazily so importing this module never imports cairo.
            import cairo

            try:
                surface = cairo.ImageSurface.create_from_png(io.BytesIO(data))
            except cairo.Error as error:
                # The seam owns the backend's exception so callers never have
                # to import cairo just to catch a corrupt image.
                raise ValueError(str(error)) from error
            return cls.from_cairo_surface(surface)
        if backend == "qt":
            _qt()
            image = QImage.fromData(data)
            if image.isNull():
                raise ValueError("Unable to decode image bytes")
            return cls.from_qimage(image)
        raise ValueError(f"Unknown canvas backend: {backend!r}")

    @classmethod
    def from_rgba(cls, width, height, data, device_scale=1.0, backend="cairo"):
        """Build a carrier from a row-major, **premultiplied** RGBA byte array.

        ``data`` holds four bytes per pixel in R, G, B, A order, row-major,
        with no padding between rows; a contiguous ``numpy.uint8`` array of
        shape ``(height, width, 4)`` is accepted. ``width``/``height`` are the
        pixel dimensions of that array and ``device_scale`` divides them into
        the logical size the carrier reports, mirroring
        :meth:`from_cairo_surface`.

        Both backends store ARGB32, i.e. a packed ``0xAARRGGBB`` word, so the
        memory order is the same on both and depends only on the machine's
        endianness (BGRA on little-endian, ARGB on big-endian). The caller's
        RGBA rows are reordered once, here, and the resulting buffer is held
        alive by the carrier. The channel order is proven by
        ``test_canvas_equivalence`` rather than assumed.
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
        if backend == "cairo":
            # Imported lazily so importing this module never imports cairo.
            import cairo

            surface = cairo.ImageSurface.create_for_data(
                memoryview(buffer), cairo.FORMAT_ARGB32, width, height
            )
            surface.set_device_scale(device_scale, device_scale)
            return cls(
                logical_width,
                logical_height,
                device_scale,
                surface=surface,
                buffer=buffer,
            )
        if backend == "qt":
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
        raise ValueError(f"Unknown canvas backend: {backend!r}")

    @classmethod
    def new(cls, width, height, device_scale=1.0, backend="cairo"):
        """Create a blank surface of ``width`` x ``height`` logical pixels.

        This is the constructor the renderers call, so ``backend`` here is the
        single switch that moves one of them: ``"cairo"`` builds the previous
        ``cairo.ImageSurface`` and ``"qt"`` builds the production
        ``QImage``. It defaults to cairo until a renderer is measured against
        the cairo output and flipped.
        """
        return cls.empty(width, height, device_scale, backend)

    @classmethod
    def empty(cls, width, height, device_scale=1.0, backend="cairo"):
        """Create a blank image of ``width`` x ``height`` logical pixels.

        The pixel buffer is ``width * device_scale`` by ``height *
        device_scale``; the carrier keeps the requested logical size so a
        fractional device scale cannot drift it. The image is transparent and
        can be drawn onto through a canvas built on it.
        """
        pixel_width = max(1, int(round(width * device_scale)))
        pixel_height = max(1, int(round(height * device_scale)))
        if backend == "cairo":
            # Imported lazily so importing this module never imports cairo.
            import cairo

            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pixel_width, pixel_height)
            surface.set_device_scale(device_scale, device_scale)
            return cls(width, height, device_scale, surface=surface)
        if backend == "qt":
            _qt()
            image = QImage(
                pixel_width, pixel_height, QImage.Format.Format_ARGB32_Premultiplied
            )
            image.fill(Qt.transparent)
            image.setDevicePixelRatio(device_scale)
            return cls(width, height, device_scale, qimage=image)
        raise ValueError(f"Unknown canvas backend: {backend!r}")

    @property
    def surface(self):
        """The cairo surface, or a clear error when this is a Qt image."""
        if self._surface is None:
            raise RuntimeError(
                "CanvasImage was built from a QImage; ask for .qimage instead"
            )
        return self._surface

    @property
    def qimage(self):
        """The Qt image, or a clear error when this is a cairo surface."""
        if self._qimage is None:
            raise RuntimeError(
                "CanvasImage was built from a cairo surface; ask for .surface instead"
            )
        return self._qimage


class Canvas:
    """The drawing operations the dashboard and numeric gauges need.

    Grow this class only when the next migration slice needs another
    operation; do not add speculative methods.
    """

    @classmethod
    def for_image(cls, image):
        """A canvas drawing onto ``image`` in that carrier's own backend.

        The renderers build an image with :meth:`CanvasImage.new` and ask for
        the canvas here, so switching a renderer's backend is one argument at
        the constructor and this factory follows it.
        """
        if not isinstance(image, CanvasImage):
            image = CanvasImage.from_cairo_surface(image)
        if image.backend == "qt":
            return QPainterCanvas(image.qimage)
        # Imported lazily so importing this module never imports cairo.
        import cairo

        return CairoCanvas(cairo.Context(image.surface))

    def finish(self):
        """Complete the image. Cairo flushes at the owner; Qt ends the painter."""

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
        the backend's historical default: Cairo's untouched ``FILTER_GOOD``
        (what pre-seam code inherited by never calling ``set_filter``) and
        Qt's smoothing transform. ``pad`` extends the edge pixels when the
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


class CairoCanvas(Canvas):
    """Pure delegation to a ``cairo.Context``; output must not change."""

    def __init__(self, context):
        self._cr = context

    @property
    def context(self):
        """The wrapped ``cairo.Context``, for the one cairo-only operation.

        The wind colour mesh uses ``cairo.MeshPattern``, which has no Qt
        equivalent, so it is not part of the seam: ``CairoCanvas`` keeps it and
        reaches its own context here. This property is that single documented
        escape hatch and disappears with this class in Phase 5. Do not route
        ordinary drawing through it.
        """
        return self._cr

    def save(self):
        self._cr.save()

    def restore(self):
        self._cr.restore()

    def set_source_rgba(self, r, g, b, a):
        self._cr.set_source_rgba(r, g, b, a)

    def set_source_rgb(self, r, g, b):
        self._cr.set_source_rgb(r, g, b)

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

    def set_line_cap(self, cap):
        # Imported lazily so importing this module never imports cairo.
        import cairo

        self._cr.set_line_cap(
            {
                CAP_BUTT: cairo.LINE_CAP_BUTT,
                CAP_ROUND: cairo.LINE_CAP_ROUND,
                CAP_SQUARE: cairo.LINE_CAP_SQUARE,
            }[cap]
        )

    def set_dash(self, pattern):
        self._cr.set_dash(list(pattern))

    def set_font_size(self, size):
        self._cr.set_font_size(size)

    def set_font_face(self, family, slant=None, weight=None):
        # Imported lazily so importing this module never imports cairo.
        import cairo

        self._cr.select_font_face(
            family,
            {
                None: cairo.FONT_SLANT_NORMAL,
                SLANT_NORMAL: cairo.FONT_SLANT_NORMAL,
                SLANT_ITALIC: cairo.FONT_SLANT_ITALIC,
            }[slant],
            {
                None: cairo.FONT_WEIGHT_NORMAL,
                WEIGHT_NORMAL: cairo.FONT_WEIGHT_NORMAL,
                WEIGHT_BOLD: cairo.FONT_WEIGHT_BOLD,
            }[weight],
        )

    def translate(self, x, y):
        self._cr.translate(x, y)

    def rotate(self, radians):
        self._cr.rotate(radians)

    def scale(self, sx, sy):
        self._cr.scale(sx, sy)

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
        # Imported lazily so importing this module never imports cairo.
        import cairo

        surface = image.surface
        self._cr.save()
        self._cr.translate(x, y)
        if width is not None or height is not None:
            target_width = image.width if width is None else width
            target_height = image.height if height is None else height
            self._cr.scale(target_width / image.width, target_height / image.height)
        self._cr.set_source_surface(surface, 0, 0)
        pattern = self._cr.get_source()
        if smooth is not None:
            pattern.set_filter(
                cairo.FILTER_BILINEAR if smooth else cairo.FILTER_NEAREST
            )
        pattern.set_extend(cairo.EXTEND_PAD if pad else cairo.EXTEND_NONE)
        if alpha == 1.0:
            self._cr.paint()
        else:
            self._cr.paint_with_alpha(alpha)
        self._cr.restore()

    def new_sub_path(self):
        self._cr.new_sub_path()

    def new_path(self):
        self._cr.new_path()

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

    def set_fill_rule(self, rule):
        # Imported lazily so importing this module never imports cairo.
        import cairo

        self._cr.set_fill_rule(
            {
                FILL_EVEN_ODD: cairo.FILL_RULE_EVEN_ODD,
                FILL_WINDING: cairo.FILL_RULE_WINDING,
            }[rule]
        )

    def fill(self):
        self._cr.fill()

    def fill_preserve(self):
        self._cr.fill_preserve()

    def stroke(self):
        self._cr.stroke()

    def paint(self):
        self._cr.paint()

    def stroke_preserve(self):
        self._cr.stroke_preserve()

    def clip(self):
        self._cr.clip()

    def device_scale(self):
        return max(self._cr.get_target().get_device_scale())

    def text_extents(self, text):
        return self._cr.text_extents(text)

    def show_text(self, text):
        self._cr.show_text(text)

    def text_path(self, text):
        self._cr.text_path(text)


class QPainterCanvas(Canvas):
    """Paints onto an ARGB32-premultiplied ``QImage`` through ``QPainter``.

    The image is cleared to transparent on construction and is complete once
    ``finish()`` has been called. Cairo has no equivalent of ``finish`` because
    a surface is flushed by its owner.
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
