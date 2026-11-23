"""Portrayal policy shared by the ENC render backends.

One definition per rule: the layers a cell is built from, the attributes each
portrayed layer needs, the area paint order and whitelist, the DEPARE depth
bands and their colours, the BOYLAT lateral-mark remap and the mesh
simplification budget.

This module says *what* is portrayed, never *how*.  It holds no drawing
context and imports neither Cairo nor Qt, so the Cairo drawer and the
triangle-mesh path can both read it.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Layer rules
# ---------------------------------------------------------------------------

#: ENC layers that are meta or cover the whole cell and are never portrayed.
SKIP_LAYERS = {"DSID", "M_COVR", "M_QUAL", "M_NSYS", "M_CSCL", "M_SDAT", "M_VDAT"}

#: The attributes a portrayed layer needs; every other field is ignored.
ATTRIBUTE_FIELDS = {"DEPARE": ("DRVAL1", "DRVAL2"), "BOYLAT": ("COLOUR",)}

#: S-57 minimum display scale.  A feature belongs on charts equal to or finer
#: than this denominator, so it is omitted when the display scale is coarser:
#: ``display_scale > SCAMIN``.  That is the direction the repo already uses
#: for scale gates - :func:`sailworks.core.encharts.chart_scale_visible` hides
#: a cell when ``display_scale > native_scale * 16`` and
#: :mod:`encsounding` hides soundings when ``scale > SCALE_LIMIT`` - and it is
#: pinned by ``test_enc_mesh.test_scamin_culls_only_at_coarser_scales``.
SCAMIN_FIELD = "SCAMIN"

# ---------------------------------------------------------------------------
# Areas
# ---------------------------------------------------------------------------

#: Area paint order: depth areas first, then dredged areas, then land, then
#: every other area class.
AREA_ORDER = {"DEPARE": 0, "DRGARE": 1, "LNDARE": 2}

#: The polygon classes that are filled.  Every other polygon record is
#: administrative or meta geometry (``ADMARE``, ``SEAARE``, ``MAGVAR``,
#: ``M_NPUB``) and must not be filled: an ``ADMARE`` covers the whole cell and
#: paints over the chart.
AREA_LAYERS = {
    "LNDARE",
    "DEPARE",
    "DRGARE",
    "BUAARE",
    "BUISGL",
    "LAKARE",
    "RIVERS",
    "ACHARE",
    "ANCBRT",
    "RESARE",
    "CTNARE",
}

# ---------------------------------------------------------------------------
# DEPARE depth bands
# ---------------------------------------------------------------------------

#: Band thresholds in metres.
DEPTH_SHALLOW_METRES = 5.0
DEPTH_MEDIUM_METRES = 20.0

#: CM93 reads a missing or null depth as these values.
DEPTH_ABSENT_DRVAL1 = 0
DEPTH_ABSENT_DRVAL2 = 100

BAND_SHALLOW = "shallow"
BAND_MEDIUM = "medium"
BAND_DEEP = "deep"

#: Colours of the two fixed bands, as plain RGBA floats.  The shallow band is
#: not fixed: it comes from the chart palette (``palette.shallow_sea``).
DEPTH_BAND_COLOURS = {
    BAND_MEDIUM: (0.65, 0.85, 0.95, 1.0),  # very light blue
    BAND_DEEP: (0.83, 0.92, 0.93, 1.0),  # near white-blue
}


def depare_band(drval1, drval2) -> str:
    """The depth band of a ``DEPARE`` feature.

    Only numeric values are compared; a value that is neither is ignored, so a
    null ``DRVAL2`` falls through to :data:`BAND_DEEP`.  A caller that treats an
    absent attribute as a default passes :data:`DEPTH_ABSENT_DRVAL1` and
    :data:`DEPTH_ABSENT_DRVAL2`.
    """
    if isinstance(drval1, (int, float)) and drval1 < DEPTH_SHALLOW_METRES:
        return BAND_SHALLOW
    if isinstance(drval2, (int, float)) and drval2 < DEPTH_MEDIUM_METRES:
        return BAND_MEDIUM
    return BAND_DEEP


# ---------------------------------------------------------------------------
# Lateral buoys
# ---------------------------------------------------------------------------

#: BOYLAT ``COLOUR`` values that are remapped, 3 port and 4 starboard.
BOYLAT_PORT_COLOUR = "3"
BOYLAT_STARBOARD_COLOUR = "4"

#: BOYLAT ``COLOUR`` -> S-57 ``CATLAM`` lateral mark category.  Any other
#: colour is not a lateral mark and is portrayed as a special-purpose buoy.
BOYLAT_CATLAM = {BOYLAT_PORT_COLOUR: 1, BOYLAT_STARBOARD_COLOUR: 2}

# ---------------------------------------------------------------------------
# Mesh simplification
# ---------------------------------------------------------------------------

#: Simplification budget for the scene path, in logical pixels of screen space.
#:
#: The cascade floors the budget to a power of two, so the *effective*
#: tolerance is always a bucket below it - a nominal half pixel lands at a
#: quarter pixel wherever the floor bites.  Measured on the two heaviest cells
#: (US3WA1EF, US3WA1ED) at zoom 9.5, 11 and 13, the vertex count falls steeply
#: up to ~0.45 px and flattens past 0.5 px, and 0.6 px is the largest budget
#: that leaves zoom 13 on today's bucket (its next bucket starts at 0.712 px).
#: Any view therefore simplifies by at most 0.6 px of error, and zoom 11 and 13
#: keep exactly the 0.356 px they had under the previous half-pixel rule.
VIEW_TOLERANCE_PIXELS = 0.6
