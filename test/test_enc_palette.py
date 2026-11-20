"""Focused checks for palette-derived ENC mesh colours (``encpalette``).

These cover the CM93 fill/stroke rules, DEPARE depth bands, the flat line
colours, the straight-RGBA conversion and ordered per-part expansion.  They do
not render and do not claim that width or dash matches the Cairo drawer.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from sailworks.rendering.vectordrawer import encpalette

#: cm93 palette bytes, written out rather than derived so a palette change has
#: to be reflected here deliberately.
CM93_SHALLOW = (0x73, 0xB6, 0xEF, 255)
CM93_SEA = (0xD4, 0xEA, 0xEE, 255)
CM93_LAND_STROKE = (0x52, 0x5A, 0x5C, 255)
CM93_LAND_FILL = (0xC9, 0xB9, 0x7A, 255)
DEPTH_MEDIUM = (166, 217, 242, 255)
DEPTH_DEEP = (212, 235, 237, 255)
CONTOUR = (128, 153, 178, 153)


def test_cm93_land_fill_and_stroke():
    fill, stroke = encpalette.area_colours("LNDARE", None, None, "cm93")
    assert fill == CM93_LAND_FILL
    assert stroke == CM93_LAND_STROKE
    assert encpalette.line_colour("COALNE", "cm93") == CM93_LAND_STROKE


def test_depare_depth_bands():
    shallow, _ = encpalette.area_colours("DEPARE", 2.0, 10.0, "cm93")
    medium, _ = encpalette.area_colours("DEPARE", 10.0, 15.0, "cm93")
    deep, _ = encpalette.area_colours("DEPARE", 30.0, 50.0, "cm93")
    assert shallow == CM93_SHALLOW
    assert medium == DEPTH_MEDIUM
    assert deep == DEPTH_DEEP


def test_absent_depth_values_keep_cm93_defaults():
    # CM93 reads DRVAL1 as 0 and DRVAL2 as 100, so an unbanded DEPARE is shallow.
    assert encpalette.area_colours("DEPARE", None, None, "cm93")[0] == CM93_SHALLOW
    assert encpalette.area_colours("DEPARE", 10.0, None, "cm93")[0] == DEPTH_DEEP


def test_area_branches_match_the_drawer():
    assert encpalette.area_colours("LAKARE", None, None, "cm93")[0] == CM93_SEA
    assert encpalette.area_colours("DRGARE", None, None, "cm93") == (
        DEPTH_MEDIUM,
        CONTOUR,
    )
    assert encpalette.area_colours("UNKNOWN", None, None, "cm93") == (
        (217, 217, 217, 128),
        (153, 153, 153, 128),
    )


def test_line_colours_are_flat_and_keep_alpha():
    # Colour only: no width or dash is present in the four-byte result.
    assert encpalette.line_colour("NAVLNE", "cm93") == (102, 102, 128, 153)
    assert encpalette.line_colour("DEPCNT", "cm93") == CONTOUR
    assert encpalette.line_colour("UNKNOWN", "cm93") == (128, 128, 128, 128)
    assert len(encpalette.line_colour("NAVLNE", "cm93")) == 4


def test_part_colour_selects_fill_outline_or_line():
    fill = encpalette.part_colour("LNDARE", encpalette.ROLE_FILL, None, None, "cm93")
    outline = encpalette.part_colour(
        "LNDARE", encpalette.ROLE_OUTLINE, None, None, "cm93"
    )
    line = encpalette.part_colour("COALNE", encpalette.ROLE_LINE, None, None, "cm93")
    assert fill == CM93_LAND_FILL
    assert outline == CM93_LAND_STROKE
    assert line == CM93_LAND_STROKE
    with pytest.raises(ValueError):
        encpalette.part_colour("LNDARE", "shading", None, None, "cm93")


def test_colours_are_clamped_to_bytes():
    assert encpalette.rgba((1.5, -0.5, 0.5, 2.0)) == (255, 0, 128, 255)


def test_palette_name_and_object_are_both_accepted():
    from sailworks.rendering.style import Style

    assert encpalette.resolve_palette("cm93") is Style.chart_palettes["cm93"]
    assert (
        encpalette.resolve_palette(Style.chart_palettes["dark"])
        is Style.chart_palettes["dark"]
    )
    with pytest.raises(ValueError, match="unknown chart palette"):
        encpalette.resolve_palette("missing")
    with pytest.raises(TypeError):
        encpalette.resolve_palette(42)


def test_vertex_colours_expand_parts_in_order():
    parts = (
        SimpleNamespace(first_vertex=0, vertex_count=3, colour=(1, 2, 3, 255)),
        SimpleNamespace(first_vertex=3, vertex_count=3, colour=(4, 5, 6, 128)),
        SimpleNamespace(first_vertex=6, vertex_count=3, colour=None),
    )
    colours = encpalette.vertex_colours(parts, 9)
    assert colours.shape == (9, 4)
    assert colours.dtype == np.uint8
    np.testing.assert_array_equal(colours[0], [1, 2, 3, 255])
    np.testing.assert_array_equal(colours[2], [1, 2, 3, 255])
    np.testing.assert_array_equal(colours[3], [4, 5, 6, 128])
    np.testing.assert_array_equal(colours[5], [4, 5, 6, 128])
    np.testing.assert_array_equal(colours[6], encpalette.NEUTRAL)


def test_vertex_colours_reject_a_part_outside_the_buffer():
    parts = (SimpleNamespace(first_vertex=6, vertex_count=6, colour=(1, 2, 3, 255)),)
    with pytest.raises(ValueError, match="outside"):
        encpalette.vertex_colours(parts, 9)
