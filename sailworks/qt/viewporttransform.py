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


def enc_scene_offset(
    published: tuple[float, float],
) -> tuple[float, float]:
    """Item x/y that keeps a camera-absolute ENC scene geographically pinned.

    ``mapContent`` carries the retained map's pan translation, so
    reference-local layers (world imagery, the raster chart) stay anchored.
    An ENC scene is not reference-local: its projection already maps degrees
    through a camera.  Compensating by that same translation leaves the scene
    at the camera's screen position instead of a pan away from it.
    """
    return (-float(published[0]), -float(published[1]))


def same_frame(first, second) -> bool:
    """Whether two cameras share the scale a frame's matrix can absorb.

    A pure pan is affine in the retained reference frame, so the matrix carries
    it exactly.  A different zoom is a different world-pixel scale and needs a
    matrix solved against a projection made at that scale, not a translation.
    """
    if first is None or second is None:
        return False
    return (
        float(first.zoom) == float(second.zoom)
        and float(first.width) == float(second.width)
        and float(first.height) == float(second.height)
    )


def enc_scene_placement(
    reference, frame_camera, dpr: float | None = None
) -> tuple[float, float]:
    """Item x/y that cancels ``mapContent``'s pan for a composed ENC frame.

    ``reference`` is the ``PanReference`` the retained map is currently
    translated against; ``frame_camera`` is the camera the item's matrix was
    solved for.  ``mapContent`` adds exactly the translation between the two on
    top of the item's own projection, so cancelling it is what makes the drawn
    position agree with the camera.  A frame composed at another scale has no
    placing translation (the caller re-composes it first), and no reference at
    all means there is nothing to cancel.
    """
    if reference is None or frame_camera is None:
        return (0.0, 0.0)
    try:
        offset = reference.translation_to(frame_camera, dpr)
    except ValueError:
        return (0.0, 0.0)
    return enc_scene_offset(offset)


def enc_scene_pan(
    reference, frame_camera, camera, dpr: float | None = None
) -> tuple[float, float] | None:
    """How far the presented frame has been carried since it was composed.

    The item's own projection is composed for ``frame_camera`` and never
    re-projected for a pan, so the whole translation between that camera and
    ``camera`` is what ``mapContent`` has moved it by.  ``None`` when the two
    are not related by a translation at all (a scale or size change).
    """
    if reference is None or frame_camera is None or camera is None:
        return None
    try:
        now = reference.translation_to(camera, dpr)
        composed = reference.translation_to(frame_camera, dpr)
    except ValueError:
        return None
    return (float(now[0] - composed[0]), float(now[1] - composed[1]))


#: Fraction of a viewport's shorter axis composed past each edge.  Half the
#: shorter axis makes the built region two viewports across on both axes, so an
#: ordinary drag stays inside the chart the frame already holds.
ENC_COMPOSE_FRACTION = 0.5

#: Fraction of the composed margin a pan may consume before a replacement is
#: requested.  Asking at half the margin leaves the other half as slack for the
#: asynchronous composer: the replacement normally lands before the leading
#: edge reaches the boundary of the frame on screen.
ENC_COMPOSE_TRIGGER = 0.5

#: Environment variable that replaces the composed ring with a fixed logical
#: pixel margin.  Unset in every shipped configuration, so behaviour is
#: unchanged; a malformed or non-positive value is ignored rather than allowed
#: to break the viewport.  It exists so ``tools/bench_enc_explorer.py`` can
#: measure the coverage/cost curve of larger rings.
ENC_COMPOSE_MARGIN_ENV = "SAILWORKS_ENC_COMPOSE_MARGIN"


def enc_compose_margin_override() -> float | None:
    """The fixed ring from :data:`ENC_COMPOSE_MARGIN_ENV`, or ``None``."""
    import math
    import os

    raw = os.environ.get(ENC_COMPOSE_MARGIN_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or not value > 0.0:
        return None
    return value


def enc_compose_margin(camera) -> float:
    """Logical pixels the ENC composition reaches past each viewport edge.

    The scene layer plans and meshes over a viewport grown by this much, and
    the item culls against the same ring.  The floor is the raster composition's
    ``MARGIN``: it is never worth composing less than that, and a viewport too
    small to scale one cell still needs the padding the planner expects.

    :data:`ENC_COMPOSE_MARGIN_ENV` overrides the rule when set; unset means this
    function's result is exactly what it was before the override existed.
    """
    from sailworks.rendering.overlayrenderer import MARGIN

    override = enc_compose_margin_override()
    if override is not None:
        return override

    width = float(getattr(camera, "width", 0.0) or 0.0)
    height = float(getattr(camera, "height", 0.0) or 0.0)
    span = min(width, height)
    if not span > 0.0:
        return float(MARGIN)
    return max(float(MARGIN), ENC_COMPOSE_FRACTION * span)


def enc_scene_cover_stale(
    reference, frame_camera, camera, margin: float, dpr: float | None = None
) -> bool:
    """Whether the composed frame is close to exhausting its padding.

    A composition reaches ``margin`` logical pixels past the viewport it was
    planned for, so a pan within that padding is still fully drawn.  Asking for
    a replacement once :data:`ENC_COMPOSE_TRIGGER` of the margin is consumed
    gives the composer the remaining margin as slack; waiting until the margin
    is exhausted guarantees a visible gap for the whole recompose.
    """
    pan = enc_scene_pan(reference, frame_camera, camera, dpr)
    if pan is None:
        return True
    return max(abs(pan[0]), abs(pan[1])) > ENC_COMPOSE_TRIGGER * float(margin)


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
