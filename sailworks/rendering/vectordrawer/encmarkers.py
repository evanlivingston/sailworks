"""Procedural point-marker triangles for the opt-in ENC triangle mesh.

Increment 1 of point symbols: no textures, atlas or symbol-name table.  The
templates mirror the Cairo point shapes in :mod:`cm93chartdrawer` (``_draw_light``,
``_draw_beacon``, ``_draw_buoy_lateral``, ``_draw_wreck`` and the landmark
square) as flat triangle lists a backend can upload with the area/line parts.

Coordinates are local *pixels* with the origin at the feature anchor, ``+x`` to
the right and ``+y`` down, matching the Cairo path.  A template is a tuple of
triangles, each a tuple of three ``(x, y)`` pairs.  Circles (lights, buoys and
the generic dot) are fixed fans of :data:`DISC_SEGMENTS` triangles, not curves
flattened to a tolerance, because this slice carries no textures and no
screen-constant line width.  Stroked Cairo shapes become filled ones: a beacon
is one filled triangle, the wreck X is two thin bars, and the landmark is a
filled 4x4 square.

Kind names are also the keys for :func:`encpalette.marker_colour`.  Nothing
here is wired into the default renderer or the viewport.
"""

from __future__ import annotations

import math

LIGHT = "light"
BEACON = "beacon"
BUOY = "buoy"
BUOY_PORT = "buoy-port"
BUOY_STARBOARD = "buoy-starboard"
WRECK = "wreck"
LANDMARK = "landmark"
DOT = "dot"

#: Fixed segment count for every circular template (8 triangles per disc).
DISC_SEGMENTS = 8
#: The generic dot is hidden above this display scale in the Cairo drawer.
DOT_MAX_SCALE = 100.0

_BEACON_NAMES = frozenset({"BCNCAR", "BCNLAT", "BCNISD", "BCNSAW", "BCNSPP"})
_BUOY_NAMES = frozenset({"BOYCAR", "BOYISD", "BOYSAW", "BOYSPP"})
_WRECK_NAMES = frozenset({"WRECKS", "UWTROC", "OBSTRN"})
_LANDMARK_NAMES = frozenset({"LNDMRK", "SILTNK", "CHIMNY", "TOWERS"})

#: BOYLAT ``COLOUR`` values remapped by the S-57 drawer (3 port, 4 starboard).
_PORT_COLOUR = "3"
_STARBOARD_COLOUR = "4"


def _disc(radius):
    """A fixed ``DISC_SEGMENTS`` fan centred on the anchor."""
    step = 2.0 * math.pi / DISC_SEGMENTS
    triangles = []
    for index in range(DISC_SEGMENTS):
        start = index * step
        stop = start + step
        triangles.append(
            (
                (0.0, 0.0),
                (radius * math.cos(start), radius * math.sin(start)),
                (radius * math.cos(stop), radius * math.sin(stop)),
            )
        )
    return tuple(triangles)


def _rays(inner, outer, half_width):
    """Eight rays from ``inner`` to ``outer`` radius, as thin triangles."""
    triangles = []
    for index in range(8):
        angle = index * math.pi / 4.0
        cosine = math.cos(angle)
        sine = math.sin(angle)
        # Tangential offset, so each ray is a triangle with its base on the disc.
        tangent_x, tangent_y = -sine * half_width, cosine * half_width
        triangles.append(
            (
                (inner * cosine - tangent_x, inner * sine - tangent_y),
                (inner * cosine + tangent_x, inner * sine + tangent_y),
                (outer * cosine, outer * sine),
            )
        )
    return tuple(triangles)


#: A V band: apex near (0, -3), arms down to (+/-3, 2), 1.6 px thick vertically.
_CHEVRON = (
    ((-3.0, 2.0), (0.0, -3.0), (0.0, -1.4)),
    ((-3.0, 2.0), (0.0, -1.4), (-3.0, 3.6)),
    ((3.0, 2.0), (0.0, -1.4), (0.0, -3.0)),
    ((3.0, 2.0), (3.0, 3.6), (0.0, -1.4)),
)

#: The Cairo X is stroked 1 px wide from (+/-3, +/-3); these bars are 1 px wide.
_CROSS_OFFSET = 0.5 / math.sqrt(2.0)
_O = _CROSS_OFFSET
_CROSS = (
    ((-3.0 - _O, -3.0 + _O), (3.0 - _O, 3.0 + _O), (3.0 + _O, 3.0 - _O)),
    ((-3.0 - _O, -3.0 + _O), (3.0 + _O, 3.0 - _O), (-3.0 + _O, -3.0 - _O)),
    ((-3.0 - _O, 3.0 - _O), (3.0 - _O, -3.0 - _O), (3.0 + _O, -3.0 + _O)),
    ((-3.0 - _O, 3.0 - _O), (3.0 + _O, -3.0 + _O), (-3.0 + _O, 3.0 + _O)),
)

#: Filled 4x4 squares replacing the Cairo 4x4 stroke.
_LANDMARK = (
    ((-2.0, -2.0), (2.0, -2.0), (2.0, 2.0)),
    ((-2.0, -2.0), (2.0, 2.0), (-2.0, 2.0)),
)

_TEMPLATES = {
    LIGHT: _disc(4.0) + _rays(4.0, 7.0, 0.35),
    BEACON: (((0.0, -5.0), (-4.0, 3.0), (4.0, 3.0)),),
    BUOY: _disc(3.0),
    BUOY_PORT: _CHEVRON,
    BUOY_STARBOARD: _CHEVRON,
    WRECK: _CROSS,
    LANDMARK: _LANDMARK,
    DOT: _disc(1.5),
}

KINDS = (LIGHT, BEACON, BUOY, BUOY_PORT, BUOY_STARBOARD, WRECK, LANDMARK, DOT)


def triangles(kind) -> tuple:
    """The local-pixel triangle template for ``kind``."""
    try:
        return _TEMPLATES[kind]
    except KeyError:
        raise ValueError(f"unknown marker kind {kind!r}: {', '.join(KINDS)}") from None


def _first_colour(colours) -> str | None:
    """Normalise a ``COLOUR`` attribute (list or scalar) to a string."""
    if colours is None:
        return None
    if isinstance(colours, (list, tuple)):
        if not colours:
            return None
        colours = colours[0]
    return str(colours)


def classify(source_class, colours=None) -> str | None:
    """Marker kind for a point feature, or ``None`` when it draws no marker.

    Mirrors :meth:`CM93ChartDrawer._render_point` and the S-57 BOYLAT remap:
    ``COLOUR`` 3 is port (red) and 4 is starboard (green); any other BOYLAT
    colour falls back to the generic buoy, exactly as the drawer rewrites the
    object to ``BOYSPP``.  Unknown point classes get the generic :data:`DOT`
    rather than being dropped.
    """
    if source_class == "SOUNDG":
        return None
    if source_class == "LIGHTS":
        return LIGHT
    if source_class in _BEACON_NAMES:
        return BEACON
    if source_class == "BOYLAT":
        colour = _first_colour(colours)
        if colour == _PORT_COLOUR:
            return BUOY_PORT
        if colour == _STARBOARD_COLOUR:
            return BUOY_STARBOARD
        return BUOY
    if source_class in _BUOY_NAMES:
        return BUOY
    if source_class in _WRECK_NAMES:
        return WRECK
    if source_class in _LANDMARK_NAMES:
        return LANDMARK
    return DOT


def is_visible(kind, scale=None) -> bool:
    """Whether ``kind`` survives the drawer's scale gates.

    Only the generic dot is gated (hidden above :data:`DOT_MAX_SCALE`).  A
    ``scale`` of ``None`` means unknown, so nothing is gated; the caller must
    evaluate this per camera when it knows the view scale.
    """
    if kind == DOT and scale is not None:
        return float(scale) <= DOT_MAX_SCALE
    return True
