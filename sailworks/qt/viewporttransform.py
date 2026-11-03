"""Reference-local transforms for retained geographic viewport layers.

This module deliberately has no Qt dependency.  The viewport owns one
``PanReference`` per map and layers retain anchors relative to that reference;
scene adapters may apply the resulting translation without changing layer
content or camera selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

WORLD_PIXELS = 256.0
MAX_LAT = 85.051129  # matches MercatorProjection.MAX_LAT exactly


class CameraChange(str, Enum):
    PAN = "pan"
    REFERENCE_INVALIDATION = "reference-invalidation"


def wrap_longitude(longitude: float) -> float:
    """Return longitude in [-180, 180), preserving a continuous seam choice."""
    return (float(longitude) + 180.0) % 360.0 - 180.0


def continuous_longitude(longitude: float, near: float) -> float:
    """Choose the equivalent longitude nearest ``near``."""
    longitude = float(longitude)
    return longitude + 360.0 * round((float(near) - longitude) / 360.0)


def _world_x(longitude: float, zoom: float) -> float:
    return (float(longitude) + 180.0) / 360.0 * WORLD_PIXELS * 2.0 ** float(zoom)


def _world_y(latitude: float, zoom: float) -> float:
    latitude = max(-MAX_LAT, min(MAX_LAT, float(latitude)))
    radians = math.radians(latitude)
    mercator = math.log(math.tan(math.pi / 4.0 + radians / 2.0))
    return (1.0 - mercator / math.pi) / 2.0 * WORLD_PIXELS * 2.0 ** float(zoom)


@dataclass(frozen=True)
class PanReference:
    """Camera identity against which retained geographic content is placed."""

    center_lat: float
    center_lon: float
    zoom: float
    width: float
    height: float
    dpr: float = 1.0

    @classmethod
    def from_camera(cls, camera, dpr: float = 1.0) -> "PanReference":
        return cls(
            float(camera.center_lat),
            float(camera.center_lon),
            float(camera.zoom),
            float(camera.width),
            float(camera.height),
            float(dpr),
        )

    def translation_to(self, camera, dpr: float | None = None) -> tuple[float, float]:
        """Logical-pixel translation that moves this reference to ``camera``.

        The current longitude is made continuous relative to the reference so
        crossing +/-180 degrees never makes retained content jump a world wide.
        """
        candidate_zoom = float(camera.zoom)
        candidate_width = float(camera.width)
        candidate_height = float(camera.height)
        candidate_dpr = self.dpr if dpr is None else float(dpr)
        if candidate_zoom != self.zoom:
            raise ValueError("incompatible zoom for translation_to")
        if candidate_width != self.width or candidate_height != self.height:
            raise ValueError("incompatible viewport size for translation_to")
        if candidate_dpr != self.dpr:
            raise ValueError("incompatible dpr for translation_to")
        current_lon = continuous_longitude(camera.center_lon, self.center_lon)
        return (
            _world_x(self.center_lon, self.zoom) - _world_x(current_lon, self.zoom),
            _world_y(self.center_lat, self.zoom)
            - _world_y(camera.center_lat, self.zoom),
        )

    def classify(self, camera, dpr: float | None = None) -> CameraChange:
        """Only translation at unchanged logical viewport/scale preserves it."""
        candidate_dpr = self.dpr if dpr is None else float(dpr)
        if (
            float(camera.zoom) == self.zoom
            and float(camera.width) == self.width
            and float(camera.height) == self.height
            and candidate_dpr == self.dpr
        ):
            return CameraChange.PAN
        return CameraChange.REFERENCE_INVALIDATION


@dataclass(frozen=True)
class LayerPlacement:
    """A layer anchor in bounded logical pixels local to a ``PanReference``."""

    x: float
    y: float
    reference: PanReference

    def screen_anchor(self, camera, dpr: float | None = None) -> tuple[float, float]:
        dx, dy = self.reference.translation_to(camera, dpr)
        return (self.x + dx, self.y + dy)


def reference_local_point(reference, camera, x, y, dpr: float | None = None):
    """Convert a current-camera point to coordinates at ``reference``."""
    if reference is None:
        return float(x), float(y)
    dx, dy = reference.translation_to(camera, dpr)
    return float(x) - dx, float(y) - dy


def zoom_preview_transform(
    anchor: PanReference, target: PanReference
) -> tuple[float, float, float] | None:
    """Scale and offset that map ``anchor``-frame pixels into ``target`` pixels.

    A retained barb atlas was projected at the anchor camera, so its centre is
    the anchor frame's screen centre.  A zoom scales every anchor position
    about that centre by ``2 ** (target.zoom - anchor.zoom)``; the projected
    position of the same geographic centre in the target camera supplies the
    shift.  The longitude is made continuous against the target centre, so a
    preview that crosses the antimeridian never jumps a world width.

    Only the anchor position is scaled.  The symbol's own quad (its local
    +/-32 pixel offsets) is added afterwards by the shader, so every preview
    symbol keeps its screen size.

    ``None`` means the two frames have different viewport sizes, so no
    screen placement exists and the preview must not be shown.
    """
    if float(anchor.width) != float(target.width) or float(anchor.height) != float(
        target.height
    ):
        return None
    scale = 2.0 ** (float(target.zoom) - float(anchor.zoom))
    longitude = continuous_longitude(anchor.center_lon, target.center_lon)
    centre_x = (
        _world_x(longitude, target.zoom)
        - _world_x(target.center_lon, target.zoom)
        + float(target.width) / 2.0
    )
    centre_y = (
        _world_y(anchor.center_lat, target.zoom)
        - _world_y(target.center_lat, target.zoom)
        + float(target.height) / 2.0
    )
    return (
        scale,
        centre_x - scale * float(anchor.width) / 2.0,
        centre_y - scale * float(anchor.height) / 2.0,
    )


@dataclass(frozen=True)
class PaddedPanWindow:
    """Retained raster window that can translate until its padding is used."""

    reference: PanReference
    padding: float

    def placement(self, camera, dpr: float | None = None):
        """Return translation and whether the retained window must be rebased."""
        if self.reference.classify(camera, dpr) != CameraChange.PAN:
            return 0.0, 0.0, True
        dx, dy = self.reference.translation_to(camera, dpr)
        return dx, dy, abs(dx) > self.padding or abs(dy) > self.padding
