"""Offscreen tests for the sounding glyph atlas (``sailworks.qt.glyphatlas``).

The atlas must build without a window and report deterministic cell UVs and
font metrics; nothing here renders or measures presentation.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtGui import QFontMetricsF, QGuiApplication  # noqa: E402

from sailworks.qt.glyphatlas import GLYPHS, GlyphAtlas  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QGuiApplication.instance()
    if app is None:
        try:
            app = QGuiApplication([])
        except Exception as exc:  # pragma: no cover - headless without a platform
            pytest.skip(f"Qt platform unavailable: {exc}")
    return app


def _ink_pixels(atlas: GlyphAtlas, character: str) -> int:
    cell_px = atlas.image.width() // len(GLYPHS)
    index = GLYPHS.index(character)
    count = 0
    for y in range(atlas.image.height()):
        for x in range(index * cell_px, (index + 1) * cell_px):
            if atlas.image.pixelColor(x, y).alpha() > 0:
                count += 1
    return count


def test_atlas_covers_digits_and_dot_with_metrics(qt_app):
    atlas = GlyphAtlas(11)

    assert set(atlas.glyphs) == set("0123456789.")
    assert atlas.ascent > 0.0
    assert atlas.descent >= 0.0
    assert atlas.cell_width > 0.0
    assert atlas.cell_height > 0.0
    for character in "0123456789.":
        glyph = atlas.glyph(character)
        assert glyph is not None
        u0, v0, u1, v1 = glyph.uv
        assert 0.0 <= u0 < u1 <= 1.0
        assert (v0, v1) == (0.0, 1.0)
        assert glyph.advance > 0.0
        assert glyph.width == pytest.approx(atlas.cell_width)
        assert glyph.height == pytest.approx(atlas.cell_height)
    assert atlas.glyph("x") is None
    assert atlas.can_render("12.3")
    assert not atlas.can_render("12.3m")


def test_atlas_uv_cells_tile_without_overlap(qt_app):
    atlas = GlyphAtlas(11)
    edges = sorted((atlas.glyph(character).uv) for character in "0123456789.")
    for (_, _, u1, _), (next_u0, _, _, _) in zip(edges, edges[1:]):
        assert u1 == pytest.approx(next_u0)
    assert edges[0][0] == 0.0
    assert edges[-1][2] == pytest.approx(1.0)


def test_atlas_rasterises_ink_for_digits_and_dot(qt_app):
    atlas = GlyphAtlas(11)

    for character in "0123456789.":
        assert _ink_pixels(atlas, character) > 0, character


def test_atlas_is_deterministic(qt_app):
    first = GlyphAtlas(11)
    second = GlyphAtlas(11)

    assert first.image.size() == second.image.size()
    assert first.ascent == second.ascent
    assert first.cell_width == second.cell_width
    assert first.glyphs == second.glyphs
    assert bytes(first.image.constBits()) == bytes(second.image.constBits())


def test_atlas_scales_image_for_device_pixel_ratio(qt_app):
    logical = GlyphAtlas(11, device_pixel_ratio=1.0)
    retina = GlyphAtlas(11, device_pixel_ratio=2.0)

    assert retina.image.height() > logical.image.height()
    # Layout metrics stay logical so quads do not double in size.
    assert retina.ascent == pytest.approx(logical.ascent, abs=1.0)
    assert retina.cell_width == pytest.approx(logical.cell_width, abs=1.0)
    # Advances are measured on the font the image was rasterised with (22 px
    # at dpr 2) and divided by the ratio, so they are logical pixels and match
    # the glyphs actually in the texture.  Qt's advances are not exactly linear
    # in pixel size, so the dpr=2 atlas lays text out a few percent narrower
    # than a dpr=1 atlas rather than identically; what must never happen is the
    # device-pixel advance leaking through undivided.
    device_advance = sum(
        QFontMetricsF(retina.font).horizontalAdvance(character) for character in "12.3"
    )
    assert retina.advance("12.3") == pytest.approx(device_advance / 2.0)
    assert retina.advance("12.3") == pytest.approx(logical.advance("12.3"), rel=0.2)


def test_atlas_rejects_non_positive_sizes(qt_app):
    with pytest.raises(ValueError):
        GlyphAtlas(0)
    with pytest.raises(ValueError):
        GlyphAtlas(11, device_pixel_ratio=0)
