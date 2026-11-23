"""Procedural point-marker triangles for the opt-in ENC triangle mesh.

Increment 1 of point symbols: no textures, atlas or symbol-name table.  The
templates mirror the Cairo point shapes in :mod:`cm93chartdrawer` (``_draw_light``,
``_draw_beacon``, ``_draw_buoy_lateral``, ``_draw_wreck`` and the landmark
square) as flat triangle lists a backend can upload with the area/line parts.

Coordinates are local *pixels* with the origin at the feature anchor, ``+x`` to
the right and ``+y`` down, matching the Cairo path.  A template is a tuple of
triangles, each a tuple of three ``(x, y)`` pairs.  Circles (lights, buoys,
lateral buoys and the generic dot) are fixed fans of :data:`DISC_SEGMENTS`
triangles, not curves flattened to a tolerance, because this slice carries no
textures and no screen-constant line width.  That is an inscribed-polygon
approximation of a circle: the extremes of the Cairo arc are kept and the
mid-chord sits at ``radius * cos(pi / DISC_SEGMENTS)``.

Stroked Cairo shapes become filled ones, but the *ink* they cover is kept: a
closed stroke spreads half its width either side of the path and leaves the
interior unpainted, so the beacon is the hollow 1.5 px band between those two
boundaries, the wreck X is two 1 px bars, and the landmark is the 4x4 stroked
square's 1 px ring.  Buoyage colour is per kind (:func:`encpalette.marker_colour`); a lateral
buoy is the same r=3 disc as the generic buoy, red for port and green for
starboard, exactly as ``_draw_buoy_lateral`` fills it.

Kind names are also the keys for :func:`encpalette.marker_colour`.  Nothing
here is wired into the default renderer or the viewport.
"""

from __future__ import annotations

import math

from sailworks.rendering.scene.policy import (
    BOYLAT_PORT_COLOUR,
    BOYLAT_STARBOARD_COLOUR,
)

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


def _scaled_about_incenter(path, factor):
    """``path`` scaled about its incenter by ``factor``, as a tuple.

    Dilating or eroding a triangle edge by edge keeps its angles, so both
    boundaries of a stroke are the same triangle scaled about its incenter.
    The miter limit is not modelled; at the drawers' 1.5 px stroke on an 8 px
    triangle the joins are far inside it.
    """
    (ax, ay), (bx, by), (cx, cy) = path
    opposite_a = math.hypot(bx - cx, by - cy)
    opposite_b = math.hypot(ax - cx, ay - cy)
    opposite_c = math.hypot(ax - bx, ay - by)
    perimeter = opposite_a + opposite_b + opposite_c
    center_x = (opposite_a * ax + opposite_b * bx + opposite_c * cx) / perimeter
    center_y = (opposite_a * ay + opposite_b * by + opposite_c * cy) / perimeter
    return tuple(
        (
            center_x + (x - center_x) * factor,
            center_y + (y - center_y) * factor,
        )
        for x, y in path
    )


def _inradius(path):
    """The inradius of a triangle ``path``."""
    (ax, ay), (bx, by), (cx, cy) = path
    opposite_a = math.hypot(bx - cx, by - cy)
    opposite_b = math.hypot(ax - cx, ay - cy)
    opposite_c = math.hypot(ax - bx, ay - by)
    perimeter = opposite_a + opposite_b + opposite_c
    area = abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) / 2.0
    return 2.0 * area / perimeter


def _stroke_ring(path, half_width):
    """The ink a closed Cairo ``stroke()`` covers, as a ring of triangles.

    A closed stroke is hollow: it spreads ``half_width`` either side of the
    path and leaves the interior unpainted.  The outer and inner boundaries
    are the path scaled about its incenter, and the band between them is one
    quad per edge, split into two triangles.
    """
    inradius = _inradius(path)
    outer = _scaled_about_incenter(path, 1.0 + half_width / inradius)
    inner = _scaled_about_incenter(path, 1.0 - half_width / inradius)
    triangles = []
    for index in range(len(path)):
        following = (index + 1) % len(path)
        o0, o1 = outer[index], outer[following]
        i0, i1 = inner[index], inner[following]
        triangles.append((o0, o1, i0))
        triangles.append((o1, i1, i0))
    return tuple(triangles)


#: The Cairo beacon path: apex (0, -5), base corners (+/-4, 3).
_BEACON_PATH = ((0.0, -5.0), (-4.0, 3.0), (4.0, 3.0))
#: The Cairo beacon stroke width; the ink reaches 0.75 px beyond the path.
_BEACON_STROKE = 1.5

#: The Cairo landmark is a 4x4 stroked square, 1 px wide: a 1 px ring whose
#: outer edge is the path plus the stroke's half width, so 2.5 px from centre.
_LANDMARK = (
    # Top bar.
    ((-2.5, -2.5), (2.5, -2.5), (2.5, -1.5)),
    ((-2.5, -2.5), (2.5, -1.5), (-2.5, -1.5)),
    # Bottom bar.
    ((-2.5, 1.5), (2.5, 1.5), (2.5, 2.5)),
    ((-2.5, 1.5), (2.5, 2.5), (-2.5, 2.5)),
    # Left bar, between the horizontal ones so the corners are covered once.
    ((-2.5, -1.5), (-1.5, -1.5), (-1.5, 1.5)),
    ((-2.5, -1.5), (-1.5, 1.5), (-2.5, 1.5)),
    # Right bar.
    ((1.5, -1.5), (2.5, -1.5), (2.5, 1.5)),
    ((1.5, -1.5), (2.5, 1.5), (1.5, 1.5)),
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

_TEMPLATES = {
    LIGHT: _disc(4.0) + _rays(4.0, 7.0, 0.25),
    BEACON: _stroke_ring(_BEACON_PATH, _BEACON_STROKE / 2.0),
    BUOY: _disc(3.0),
    # The Cairo lateral buoy is the generic r=3 disc in the port or starboard
    # colour; only ``encpalette.marker_colour`` tells the three apart.
    BUOY_PORT: _disc(3.0),
    BUOY_STARBOARD: _disc(3.0),
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
        if colour == BOYLAT_PORT_COLOUR:
            return BUOY_PORT
        if colour == BOYLAT_STARBOARD_COLOUR:
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
