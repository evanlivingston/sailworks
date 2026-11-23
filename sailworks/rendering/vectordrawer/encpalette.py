"""Palette-derived flat colours for the opt-in ENC triangle mesh.

This resolves the *colour* half of the chart palette and the CM93 depth-area
rules into straight RGBA bytes so per-part colours can be carried in vertex
data.  It deliberately does not reproduce the other half of the Cairo style:

* line width, joins and dash patterns are **not** represented - a part only
  gets one flat colour, so ``DASHED_STYLE`` and ``CONTOUR_STYLE`` keep their
  colour but not their dash or width;
* conditional symbology and text are not styled here at all.  Point markers
  are flat colours only (see :func:`marker_colour`), with no texture or atlas.

The colour rules mirror :meth:`CM93ChartDrawer._get_area_styles` and
``_render_line``.  The depth bands and their colours are shared through
:mod:`sailworks.rendering.scene.policy`; the remaining constants are duplicated
rather than imported because ``cm93chartdrawer`` imports ``cairo`` and the mesh
path must stay usable without it.  Keep them in step if the drawer's palette
changes.

Nothing here is wired into the default renderer; it is opt-in context for
:func:`encmesh.build_scene_mesh`.
"""

from __future__ import annotations

import numpy as np

from sailworks.rendering.scene.policy import (
    BAND_MEDIUM,
    BAND_SHALLOW,
    DEPTH_ABSENT_DRVAL1,
    DEPTH_ABSENT_DRVAL2,
    DEPTH_BAND_COLOURS,
    depare_band,
)
from sailworks.rendering.style import ChartPalette, Style

#: Mirrors of the drawer's module-level styles, as straight RGBA floats.
CONTOUR_COLOUR = (0.5, 0.6, 0.7, 0.6)
DASHED_COLOUR = (0.4, 0.4, 0.5, 0.6)
BUILDUP_FILL = (0.7, 0.65, 0.55, 1.0)
DEFAULT_AREA_FILL = (0.85, 0.85, 0.85, 0.5)
DEFAULT_AREA_STROKE = (0.6, 0.6, 0.6, 0.5)
DEFAULT_LINE = (0.5, 0.5, 0.5, 0.5)
ACHARE_FILL = (0.8, 0.8, 0.95, 0.3)
ACHARE_STROKE = (0.5, 0.5, 0.7, 0.5)
RESARE_FILL = (0.95, 0.85, 0.85, 0.3)
RESARE_STROKE = (0.7, 0.4, 0.4, 0.5)

ROLE_FILL = "fill"
ROLE_OUTLINE = "outline"
ROLE_LINE = "line"
ROLE_MARKER = "marker"
ROLES = (ROLE_FILL, ROLE_OUTLINE, ROLE_LINE, ROLE_MARKER)


#: For callers that ask for every part without resolving a chart palette.
NEUTRAL = (120, 155, 190, 255)


def _byte(value) -> int:
    return int(round(min(max(float(value), 0.0), 1.0) * 255.0))


def rgba(color) -> tuple[int, int, int, int]:
    """Convert a ``CairoStyle``/4-tuple of floats into straight RGBA bytes."""
    if hasattr(color, "color"):
        color = color.color
    red, green, blue, alpha = color[:4]
    return (_byte(red), _byte(green), _byte(blue), _byte(alpha))


#: Mirrors of the drawer's module-level point styles.  The keys are
#: :mod:`encmarkers` kind names; keep the two modules in step.
MARKER_LIGHT = rgba((1.0, 1.0, 0.0, 0.9))
MARKER_BEACON = rgba((0.0, 0.0, 0.0, 0.9))
MARKER_PORT = rgba((0.8, 0.1, 0.1, 0.9))
MARKER_STARBOARD = rgba((0.1, 0.6, 0.1, 0.9))
MARKER_WRECK = rgba((0.3, 0.3, 0.3, 0.8))
MARKER_DEFAULT = rgba((0.3, 0.3, 0.3, 0.6))

_MARKER_COLOURS = {
    "light": MARKER_LIGHT,
    "beacon": MARKER_BEACON,
    "buoy": MARKER_DEFAULT,
    "buoy-port": MARKER_PORT,
    "buoy-starboard": MARKER_STARBOARD,
    "wreck": MARKER_WRECK,
    "landmark": MARKER_BEACON,
    "dot": MARKER_DEFAULT,
}


def marker_colour(kind) -> tuple[int, int, int, int]:
    """Straight RGBA bytes for a :mod:`encmarkers` marker kind."""
    try:
        return _MARKER_COLOURS[kind]
    except KeyError:
        raise ValueError(f"unknown marker kind {kind!r}") from None


def resolve_palette(palette) -> ChartPalette:
    """Accept a palette name or a :class:`ChartPalette`."""
    if isinstance(palette, ChartPalette):
        return palette
    if isinstance(palette, str):
        try:
            return Style.chart_palettes[palette]
        except KeyError:
            names = ", ".join(sorted(Style.chart_palettes))
            raise ValueError(f"unknown chart palette {palette!r}: {names}") from None
    raise TypeError("palette must be a ChartPalette or a known palette name")


def _depth_style(drval1, drval2, palette) -> tuple:
    """The DEPARE band's fill, with the mesh path's absent-value defaults."""
    band = depare_band(
        DEPTH_ABSENT_DRVAL1 if drval1 is None else drval1,
        DEPTH_ABSENT_DRVAL2 if drval2 is None else drval2,
    )
    if band == BAND_SHALLOW:
        return palette.shallow_sea
    return DEPTH_BAND_COLOURS[band]


def area_colours(source_class, drval1, drval2, palette):
    """Return ``(fill, stroke)`` RGBA bytes for an area feature."""
    palette = resolve_palette(palette)
    if source_class == "LNDARE":
        return rgba(palette.land_fill), rgba(palette.land_stroke)
    if source_class == "DEPARE":
        return rgba(_depth_style(drval1, drval2, palette)), rgba(CONTOUR_COLOUR)
    if source_class == "DRGARE":
        return rgba(DEPTH_BAND_COLOURS[BAND_MEDIUM]), rgba(CONTOUR_COLOUR)
    if source_class in ("BUAARE", "BUISGL"):
        return rgba(BUILDUP_FILL), rgba(palette.land_stroke)
    if source_class in ("LAKARE", "RIVERS"):
        return rgba(palette.sea), rgba(CONTOUR_COLOUR)
    if source_class in ("ACHARE", "ANCBRT"):
        return rgba(ACHARE_FILL), rgba(ACHARE_STROKE)
    if source_class in ("RESARE", "CTNARE"):
        return rgba(RESARE_FILL), rgba(RESARE_STROKE)
    return rgba(DEFAULT_AREA_FILL), rgba(DEFAULT_AREA_STROKE)


def line_colour(source_class, palette) -> tuple[int, int, int, int]:
    """Return the flat stroke colour for a linear feature.

    Width and dash are intentionally not part of the result.
    """
    palette = resolve_palette(palette)
    if source_class == "COALNE":
        return rgba(palette.land_stroke)
    if source_class == "DEPCNT":
        return rgba(CONTOUR_COLOUR)
    if source_class in ("NAVLNE", "FERYRT", "TSELNE"):
        return rgba(DASHED_COLOUR)
    return rgba(DEFAULT_LINE)


def part_colour(source_class, role, drval1, drval2, palette):
    """Resolve one mesh part's straight RGBA bytes from its source feature."""
    if role == ROLE_LINE:
        return line_colour(source_class, palette)
    if role not in (ROLE_FILL, ROLE_OUTLINE):
        raise ValueError(f"unknown style role {role!r}")
    fill, stroke = area_colours(source_class, drval1, drval2, palette)
    return fill if role == ROLE_FILL else stroke


def vertex_colours(parts, vertex_count, default=NEUTRAL) -> np.ndarray:
    """Expand ordered :class:`encmesh.MeshPart` colours into an ``(N, 4)`` array.

    Parts are applied in their existing order, so a colour never reorders or
    merges a feature run.  A part without a colour keeps the caller's default.
    """
    default = tuple(int(channel) for channel in default[:4])
    colours = np.empty((int(vertex_count), 4), dtype=np.uint8)
    colours[:] = default
    for part in parts:
        start = int(part.first_vertex)
        stop = start + int(part.vertex_count)
        if start < 0 or stop > colours.shape[0]:
            raise ValueError("mesh part lies outside the vertex array")
        colour = getattr(part, "colour", None)
        if colour is not None:
            colours[start:stop] = tuple(int(channel) for channel in colour[:4])
    return colours
