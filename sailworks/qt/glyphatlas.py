"""Offscreen glyph atlas for GPU-rendered ENC sounding labels.

The Cairo ENC drawer draws soundings with ``cr.show_text``.  The opt-in Qt
scene-graph path cannot read that surface back, and QML text items would cost
one item per label.  Instead this module rasterises a fixed digit-plus-dot
atlas once with :class:`QPainter` and :class:`QFont`, and exposes the per-glyph
UV rectangle and metrics so a textured node can lay out the same strings as
quads.

The atlas is deliberately tiny and deterministic: one ``QImage``, one fixed
grid, one glyph per cell.  Glyphs are painted white on transparent pixels;
callers that need a tint bake it in through ``ink`` or leave the default dark
ink baked into the premultiplied image.  Nothing here touches QML, the default
renderer or a GPU: the image is created offscreen and can be built without a
window, which is what the focused tests exercise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen

#: Characters the sounding format (``f"{depth:.1f}"``) can emit.
GLYPHS = "0123456789."


@dataclass(frozen=True)
class Glyph:
    """One atlas cell: normalised texture coordinates and layout metrics.

    ``uv`` is ``(u0, v0, u1, v1)`` with ``(0, 0)`` at the image's top-left, the
    orientation ``QQuickWindow.createTextureFromImage`` samples.  ``advance``,
    ``width`` and ``height`` are in logical pixels (device-independent), so a
    caller lays out at the same size regardless of the render device ratio.
    """

    character: str
    uv: tuple[float, float, float, float]
    advance: float
    width: float
    height: float


class GlyphAtlas:
    """A fixed grid of sounding glyphs rasterised into one ``QImage``.

    ``pixel_size`` is the logical font size (the drawer uses 11).  The image is
    rendered at ``pixel_size * device_pixel_ratio`` device pixels so retina
    output is crisp, while all reported metrics stay in logical pixels.
    """

    def __init__(
        self,
        pixel_size: float = 11.0,
        *,
        device_pixel_ratio: float = 1.0,
        ink: QColor | None = None,
        font_family: str | None = None,
    ) -> None:
        if not float(pixel_size) > 0.0:
            raise ValueError("pixel_size must be positive")
        if not float(device_pixel_ratio) > 0.0:
            raise ValueError("device_pixel_ratio must be positive")
        self._pixel_size = float(pixel_size)
        self._dpr = float(device_pixel_ratio)

        font = QFont() if font_family is None else QFont(font_family)
        font.setPixelSize(max(1, int(round(self._pixel_size * self._dpr))))
        metrics = QFontMetricsF(font)

        cell_width = int(
            math.ceil(max(metrics.horizontalAdvance(character) for character in GLYPHS))
        )
        cell_height = int(math.ceil(metrics.height()))
        cell_width = max(1, cell_width)
        cell_height = max(1, cell_height)
        image_width = cell_width * len(GLYPHS)

        image = QImage(
            image_width,
            cell_height,
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setFont(font)
            # Premultiplied ink on transparent pixels: the node draws the
            # texture straight, so the baked colour is the label colour.
            painter.setPen(
                QPen(QColor(32, 32, 32, 255) if ink is None else QColor(ink))
            )
            for index, character in enumerate(GLYPHS):
                painter.drawText(
                    QPointF(index * cell_width, metrics.ascent()), character
                )
        finally:
            painter.end()

        self._font = font
        self._image = image
        self._ascent = float(metrics.ascent()) / self._dpr
        self._descent = float(metrics.descent()) / self._dpr
        self._cell_width = cell_width / self._dpr
        self._cell_height = cell_height / self._dpr
        glyphs = {}
        for index, character in enumerate(GLYPHS):
            u0 = index * cell_width / image_width
            u1 = (index + 1) * cell_width / image_width
            glyphs[character] = Glyph(
                character=character,
                uv=(u0, 0.0, u1, 1.0),
                advance=float(metrics.horizontalAdvance(character)) / self._dpr,
                width=self._cell_width,
                height=self._cell_height,
            )
        self._glyphs = glyphs

    @property
    def image(self) -> QImage:
        """The rasterised atlas; the caller owns texture creation from it."""
        return self._image

    @property
    def font(self) -> QFont:
        return self._font

    @property
    def glyphs(self) -> dict[str, Glyph]:
        return dict(self._glyphs)

    def glyph(self, character: str) -> Glyph | None:
        """The glyph for ``character``, or ``None`` when the atlas has none."""
        return self._glyphs.get(character)

    @property
    def pixel_size(self) -> float:
        return self._pixel_size

    @property
    def device_pixel_ratio(self) -> float:
        return self._dpr

    @property
    def ascent(self) -> float:
        """Distance from the baseline up to the glyph cell's top, logical px."""
        return self._ascent

    @property
    def descent(self) -> float:
        return self._descent

    @property
    def cell_width(self) -> float:
        return self._cell_width

    @property
    def cell_height(self) -> float:
        return self._cell_height

    def advance(self, text: str) -> float:
        """Total logical advance of ``text`` in this atlas, unknown glyphs 0."""
        total = 0.0
        for character in text:
            glyph = self._glyphs.get(character)
            if glyph is not None:
                total += glyph.advance
        return total

    def can_render(self, text: str) -> bool:
        return all(character in self._glyphs for character in text)
