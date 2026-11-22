"""Single definition of the packed GPU line record.

This mirrors the ``segmentData`` payload read by
``native/routing/qt/gpulineitem.h``: ``N`` records of twelve little-endian
float32 values, no header and no padding, so one record is 48 bytes:

    [0]  x0        segment start longitude, degrees
    [1]  y0        segment start latitude, degrees
    [2]  x1        segment end longitude, degrees
    [3]  y1        segment end latitude, degrees
    [4]  width     stroke width in screen pixels
    [5]  r         red    0..1
    [6]  g         green  0..1
    [7]  b         blue   0..1
    [8]  a         alpha  0..1
    [9]  dash_on   ink length in screen pixels, 0 with dash_off for solid
    [10] dash_off  gap length in screen pixels, 0 with dash_on for solid
    [11] phase     dash phase at the segment start, normalised world units

:class:`sailworks.rendering.vectordrawer.encmesh.LineSegments` builds its
arrays and its packed buffer from these constants, and the test that checks
the header against them reads the same values, so the layout is stated once
in Python.
"""

from __future__ import annotations

import numpy as np

#: Field names in record order; the tuple order *is* the wire order.
FIELD_NAMES: tuple[str, ...] = (
    "x0",
    "y0",
    "x1",
    "y1",
    "width",
    "r",
    "g",
    "b",
    "a",
    "dash_on",
    "dash_off",
    "phase",
)

#: Record field name -> float index, for reading a packed buffer by name.
FIELD_INDEX: dict[str, int] = {name: index for index, name in enumerate(FIELD_NAMES)}

#: Element type of every field on the wire.
DTYPE = np.dtype(np.float32)

#: Float32 values per record (12).
FLOATS: int = len(FIELD_NAMES)

#: Bytes per record (48): twelve float32 with no padding.
BYTES: int = FLOATS * DTYPE.itemsize

#: Endpoint group: ``x0, y0, x1, y1``.
ENDPOINT_NAMES: tuple[str, ...] = FIELD_NAMES[0:4]
ENDPOINT_FLOATS: int = len(ENDPOINT_NAMES)
ENDPOINT_SLICE = slice(0, ENDPOINT_FLOATS)

#: Stroke width, one float32 after the endpoints.
WIDTH_INDEX: int = FIELD_INDEX["width"]

#: Colour group: straight ``r, g, b, a`` in 0..1.
COLOUR_NAMES: tuple[str, ...] = FIELD_NAMES[5:9]
COLOUR_CHANNELS: int = len(COLOUR_NAMES)
COLOUR_SLICE = slice(WIDTH_INDEX + 1, WIDTH_INDEX + 1 + COLOUR_CHANNELS)

#: Dash pattern and phase, appended after the original nine-float prefix.
DASH_ON_INDEX: int = FIELD_INDEX["dash_on"]
DASH_OFF_INDEX: int = FIELD_INDEX["dash_off"]
PHASE_INDEX: int = FIELD_INDEX["phase"]
