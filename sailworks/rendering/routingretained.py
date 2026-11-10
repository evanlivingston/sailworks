"""Retained routing overlay imagery.

The Qt viewport deliberately avoids a Python ``QQuickItem`` subclass: a Python
item makes scene-graph sync take the GIL on the render thread and can deadlock
the threaded render loop (documented in ``marinequick``). Routing geometry is
therefore retained as projected, style-independent primitives and painted into
a reusable image on demand, following the existing marine/wind portrayal.

Geometry is built once per result and never rebuilt for a camera move or a
style change; only the projected image is redrawn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPainter, QPen

from sailworks.rendering.routingoverlay import RoutingOverlayModel


@dataclass
class RetainedCounters:
    geometry_builds: int = 0
    renders: int = 0
    style_changes: int = 0


class RetainedRoutingLayer:
    """Model-space routing primitives plus an image-backed portrayal."""

    def __init__(self, colour: QColor | None = None, width: float = 1.5):
        self._model: Optional[RoutingOverlayModel] = None
        self._segments: Tuple[
            Tuple[Tuple[float, float], Tuple[float, float], str, str], ...
        ] = ()
        self._closed: Tuple[bool, ...] = ()
        self._colour = QColor(colour or QColor(0, 0, 204, 140))
        self._width = width
        self.counters = RetainedCounters()
        self._image = QImage()

    @property
    def counters_snapshot(self) -> RetainedCounters:
        return RetainedCounters(
            self.counters.geometry_builds,
            self.counters.renders,
            self.counters.style_changes,
        )

    def set_model(self, model: RoutingOverlayModel) -> None:
        if not isinstance(model, RoutingOverlayModel):
            raise TypeError("model must be a RoutingOverlayModel")
        segments = []
        for polyline in model.polylines:
            segments.extend(
                (
                    tuple(first),
                    tuple(second),
                    polyline.layer_id,
                    polyline.kind,
                )
                for first, second in zip(polyline.points, polyline.points[1:])
            )
        self._model = model
        self._segments = tuple(segments)
        self.counters.geometry_builds += 1

    def set_style(self, colour: QColor | None = None, width: float | None = None):
        if colour is not None:
            self._colour = QColor(colour)
        if width is not None:
            if width <= 0:
                raise ValueError("width must be positive")
            self._width = width
        self.counters.style_changes += 1

    def render(self, camera, width: int, height: int, dpr: float = 1.0) -> QImage:
        """Project retained primitives into an image; never rebuilds geometry."""
        if width <= 0 or height <= 0:
            raise ValueError("render size must be positive")
        pixel_width = max(1, int(round(width * dpr)))
        pixel_height = max(1, int(round(height * dpr)))
        if (
            self._image.size()
            != QImage(pixel_width, pixel_height, QImage.Format.Format_ARGB32).size()
        ):
            self._image = QImage(pixel_width, pixel_height, QImage.Format.Format_ARGB32)
        self._image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(self._image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(dpr, dpr)
        painter.setPen(QPen(self._colour, self._width))
        model = self._model
        for first, second, layer_id, kind in self._segments:
            if model is not None:
                if kind == "trace" and not model.traces_visible:
                    continue
                if kind == "contour" and not model.is_layer_visible(layer_id):
                    continue
            start = camera.convert_geographic_to_screen(first[0], first[1])
            end = camera.convert_geographic_to_screen(second[0], second[1])
            painter.drawLine(QPointF(start[0], start[1]), QPointF(end[0], end[1]))
        painter.end()
        self.counters.renders += 1
        return self._image
