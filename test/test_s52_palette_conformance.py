"""S-52 conformance checks for the ENC chart palettes.

The expected colours here come from the official-format IHO colour tables that
already ship with the chart symbol set (``sailworks/data/s57/chartsymbols.xml``),
not from the renderer's own constants.  A palette that drifts from those tables
therefore fails, instead of the test restating whatever the renderer happens to
emit.

Colours are resolved behaviourally through the public
:func:`encpalette.area_colours` entry point so the checks stay valid whether or
not the depth-band constants are still module-level (Phase 0 moves them).  This
only compares colours; it does not render, and it deliberately does not claim
that width, dash or contour colours match the S-52 presentation library.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from sailworks.rendering.vectordrawer import encpalette

REPO_ROOT = Path(__file__).resolve().parents[1]
CHARTSYMBOLS = REPO_ROOT / "sailworks" / "data" / "s57" / "chartsymbols.xml"

#: The five S-52 presentation libraries carried by the symbol set.  Each table
#: in this copy carries 63 named colours (the issued set does not include a
#: 64th here); the integrity test also pins that all five agree.
TABLE_NAMES = ("DAY_BRIGHT", "DAY_BLACKBACK", "DAY_WHITEBACK", "DUSK", "NIGHT")
COLOURS_PER_TABLE = 63

#: Tokens the palette assertions depend on; a table missing one is truncated.
REQUIRED_TOKENS = ("DEPVS", "DEPMS", "DEPDW", "CSTLN", "LANDA")

#: The app's three-band DEPARE ramp is interpolated rather than taken from a
#: direct S-52 token, so its fill is a recorded deviation.  The register is the
#: single place a mismatch is permitted: changing either palette or the XML
#: fails the test until the deviation is updated deliberately.
#:
#: ``fields`` is empty today: every palette field asserted below matches its
#: token exactly.  ``areas`` holds the derived depth bands, keyed by band name,
#: holding the fill the renderer must produce and the signed delta from the
#: S-52 token, computed as ``fill - token`` per channel (positive means the app
#: is lighter).
DEVIATIONS = {
    "fields": {},
    "areas": {
        "DEPARE/medium": {
            "token": "DEPMS",
            "fill": (166, 217, 242),
            "delta": (+14, +20, 0),
            "reason": (
                "the app uses a three-band ramp with interpolated values and no "
                "direct S-52 token for the medium band"
            ),
        },
        "DEPARE/deep": {
            "token": "DEPDW",
            "fill": (212, 235, 237),
            "delta": (0, +1, -1),
            "reason": (
                "the app uses a three-band ramp with interpolated values and no "
                "direct S-52 token for the deep band"
            ),
        },
    },
}

#: Depth bands exercised through ``area_colours``: (band, drval1, drval2).
BANDS = (
    ("DEPARE/shallow", 3.0, 4.0),
    ("DEPARE/medium", 10.0, 15.0),
    ("DEPARE/deep", 30.0, 40.0),
)

#: ``(palette, field, S-52 token, expected bytes)``.  The tokens are the four
#: fields the palette actually carries; the values are asserted to equal the
#: XML, so they are stated here only to keep the expectation readable.
EXACT_CASES = (
    ("cm93", "shallow_sea", "DEPVS", (115, 182, 239)),
    ("cm93", "sea", "DEPDW", (212, 234, 238)),
    ("cm93", "land_stroke", "CSTLN", (82, 90, 92)),
    ("cm93", "land_fill", "LANDA", (201, 185, 122)),
    ("dark", "shallow_sea", "DEPVS", (22, 35, 47)),
    ("dark", "sea", "DEPDW", (7, 7, 7)),
    ("dark", "land_stroke", "CSTLN", (54, 60, 61)),
    ("dark", "land_fill", "LANDA", (44, 41, 27)),
)

#: Which XML table each palette is expected to reproduce.
PALETTE_TABLES = {"cm93": "DAY_BRIGHT", "dark": "DUSK"}


@pytest.fixture(scope="module")
def s52_tables():
    """The colour tables from ``chartsymbols.xml``, parsed once."""
    assert CHARTSYMBOLS.is_file(), f"missing S-52 colour tables at {CHARTSYMBOLS}"
    root = ET.parse(CHARTSYMBOLS).getroot()
    tables = {}
    for table in root.iter("color-table"):
        colours = {}
        for colour in table.findall("color"):
            colours[colour.get("name")] = (
                int(colour.get("r")),
                int(colour.get("g")),
                int(colour.get("b")),
            )
        tables[table.get("name")] = colours
    return tables


def _resolve(palette: str, field: str) -> tuple[int, int, int, int]:
    """Resolve one palette field to straight RGBA bytes through the renderer."""
    if field == "shallow_sea":
        return encpalette.area_colours("DEPARE", 3.0, 4.0, palette)[0]
    if field == "sea":
        return encpalette.area_colours("LAKARE", None, None, palette)[0]
    if field == "land_stroke":
        return encpalette.area_colours("LNDARE", None, None, palette)[1]
    if field == "land_fill":
        return encpalette.area_colours("LNDARE", None, None, palette)[0]
    raise AssertionError(f"unknown palette field {field!r}")


# ---------------------------------------------------------------------------
# 1. Table integrity
# ---------------------------------------------------------------------------


def test_colour_tables_are_complete(s52_tables):
    assert set(s52_tables) == set(TABLE_NAMES)
    sizes = {name: len(s52_tables[name]) for name in TABLE_NAMES}
    assert sizes == dict.fromkeys(
        TABLE_NAMES, COLOURS_PER_TABLE
    ), f"colour table sizes changed: {sizes}"
    for name in TABLE_NAMES:
        missing = [t for t in REQUIRED_TOKENS if t not in s52_tables[name]]
        assert not missing, f"{name} is missing {missing}"


def test_every_colour_has_components(s52_tables):
    root = ET.parse(CHARTSYMBOLS).getroot()
    for table in root.iter("color-table"):
        for colour in table.findall("color"):
            for channel in ("r", "g", "b"):
                assert (
                    colour.get(channel) is not None
                ), f"{table.get('name')}/{colour.get('name')} has no {channel!r}"
            for value in (colour.get("r"), colour.get("g"), colour.get("b")):
                assert 0 <= int(value) <= 255


# ---------------------------------------------------------------------------
# 2-3. Palette fields reproduce their S-52 table exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("palette, field, token, expected", EXACT_CASES)
def test_palette_field_matches_s52_token(s52_tables, palette, field, token, expected):
    table = s52_tables[PALETTE_TABLES[palette]]
    assert (
        table[token] == expected
    ), f"{token} changed in chartsymbols.xml: {table[token]} != {expected}"

    resolved = _resolve(palette, field)
    entry = DEVIATIONS["fields"].get((palette, field))
    if entry is None:
        assert resolved == (*expected, 255), (
            f"{palette}.{field} resolved to {resolved}, "
            f"expected {token} {expected} opaque"
        )
        return

    # A registered field mismatch still has to be the declared one.
    assert resolved == (*entry["fill"], 255), (
        f"{palette}.{field} resolved to {resolved}, "
        f"the register records {entry['fill']}"
    )
    delta = tuple(actual - baseline for actual, baseline in zip(resolved, expected))
    assert delta == entry["delta"], (
        f"{palette}.{field} delta from {token} is {delta}, "
        f"the register records {entry['delta']}"
    )


# ---------------------------------------------------------------------------
# 4. Derived depth bands are recorded deviations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("band, drval1, drval2", BANDS)
def test_derived_depth_band_is_a_recorded_deviation(s52_tables, band, drval1, drval2):
    fill = encpalette.area_colours("DEPARE", drval1, drval2, "cm93")[0]
    assert fill[3] == 255

    if band == "DEPARE/shallow":
        token = "DEPVS"
        assert fill == (*s52_tables["DAY_BRIGHT"][token], 255)
        return

    deviation = DEVIATIONS["areas"][band]
    expected_token = s52_tables["DAY_BRIGHT"][deviation["token"]]
    assert fill == (
        *deviation["fill"],
        255,
    ), f"{band} fill is {fill}, the register records {deviation['fill']}"
    delta = tuple(actual - baseline for actual, baseline in zip(fill, expected_token))
    assert delta == deviation["delta"], (
        f"{band} delta from {deviation['token']} is {delta}, "
        f"the register records {deviation['delta']}"
    )


# ---------------------------------------------------------------------------
# 5. The register is the only place a mismatch is allowed
# ---------------------------------------------------------------------------


def test_register_only_covers_asserted_fields(s52_tables):
    """No dormant deviation: every registered entry is exercised above."""
    asserted_fields = {(palette, field) for palette, field, _, _ in EXACT_CASES}
    assert set(DEVIATIONS["fields"]) <= asserted_fields
    asserted_bands = {band for band, _, _ in BANDS}
    assert set(DEVIATIONS["areas"]) <= asserted_bands
    for entry in DEVIATIONS["areas"].values():
        assert entry["reason"], "a registered deviation needs a reason"
