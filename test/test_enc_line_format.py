"""The Python GPU line record matches the Qt item's packed layout.

``encmesh.LineSegments.packed()`` reaches
``native/routing/qt/gpulineitem.h`` as raw bytes, so the field order, the float
count and the record stride are a wire contract that nothing checks at run
time. This test parses the header's own declarations and compares them with
:mod:`lineformat`, so a header edit that reorders, resizes or renumbers the
record fails here instead of drawing garbage.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from sailworks.rendering.vectordrawer import lineformat

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "native" / "routing" / "qt" / "gpulineitem.h"

#: ``//   [4]  w   stroke width ...`` - one documented field per record index.
_FIELD_LINE = re.compile(r"^//\s+\[(\d+)\]\s+(\w+)", re.MULTILINE)

#: ``int recordStride_ = 48;`` - the stride the item actually reads.
_RECORD_STRIDE = re.compile(r"\brecordStride_\s*=\s*(\d+)")

#: ``... no header and no padding (stride 48 bytes):`` - the class comment.
_COMMENT_STRIDE = re.compile(r"no padding \(stride (\d+) bytes\)")

#: The header abbreviates the words the Python constants spell out.
_HEADER_ALIASES = {"w": "width"}


@pytest.fixture(scope="module")
def header_text() -> str:
    assert HEADER.is_file(), f"packed line header moved: {HEADER}"
    return HEADER.read_text(encoding="utf-8")


def test_record_stride_matches_python(header_text: str) -> None:
    strides = _RECORD_STRIDE.findall(header_text)
    assert strides, (
        "gpulineitem.h no longer declares recordStride_ = <bytes>; update "
        "sailworks/rendering/vectordrawer/lineformat.py and this test together"
    )
    declared = int(strides[0])
    assert declared == lineformat.BYTES, (
        f"gpulineitem.h recordStride_ = {declared}, "
        f"lineformat.BYTES = {lineformat.BYTES}"
    )

    documented = _COMMENT_STRIDE.findall(header_text)
    assert documented, (
        "gpulineitem.h class comment no longer documents the record stride; "
        "update lineformat.py and this test together"
    )
    assert int(documented[0]) == lineformat.BYTES, (
        f"gpulineitem.h documents a {documented[0]}-byte record, "
        f"lineformat.BYTES = {lineformat.BYTES}"
    )

    assert lineformat.FLOATS == 12
    assert lineformat.BYTES == 48
    assert declared == lineformat.FLOATS * np.dtype(np.float32).itemsize


def test_documented_fields_match_python(header_text: str) -> None:
    documented = [
        (int(index), name) for index, name in _FIELD_LINE.findall(header_text)
    ]
    assert documented, (
        "gpulineitem.h no longer documents the record fields as '// [n] name'; "
        "update lineformat.py and this test together"
    )

    indices = [index for index, _ in documented]
    assert indices == list(range(lineformat.FLOATS)), (
        f"gpulineitem.h documents fields at {indices}, "
        f"lineformat.FLOATS = {lineformat.FLOATS}"
    )

    header_fields = [_HEADER_ALIASES.get(name, name) for _, name in documented]
    assert header_fields == list(lineformat.FIELD_NAMES), (
        f"gpulineitem.h field order {header_fields} != "
        f"lineformat.FIELD_NAMES {list(lineformat.FIELD_NAMES)}"
    )
