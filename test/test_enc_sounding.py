"""Focused tests for the backend-neutral SOUNDG label slice.

The selection rules here must match ``S57ChartDrawer._render_soundings``:
display-scale gate, half-open viewport cull, finite depth and first-wins
32x16 screen-box dedupe, in source order.
"""

from __future__ import annotations

import math
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from sailworks.rendering.vectordrawer import encsounding


def _iso_multipoint_z(points) -> bytes:
    body = struct.pack("<I", len(points))
    for x, y, z in points:
        body += struct.pack("<B", 1) + struct.pack("<I", 1001)
        body += struct.pack("<ddd", x, y, z)
    return struct.pack("<B", 1) + struct.pack("<I", 1004) + body


def _ewkb_multipoint_z(points) -> bytes:
    body = struct.pack("<I", len(points))
    for x, y, z in points:
        body += struct.pack("<B", 1) + struct.pack("<I", 0x80000001)
        body += struct.pack("<ddd", x, y, z)
    return struct.pack("<B", 1) + struct.pack("<I", 0x80000004) + body


def test_wkb_points_reads_iso_and_ewkb_z():
    points = ((1.5, 2.5, 3.25), (4.0, 5.0, 6.75))

    assert encsounding.wkb_points(_iso_multipoint_z(points)) == points
    assert encsounding.wkb_points(_ewkb_multipoint_z(points)) == points


def test_wkb_points_does_not_invent_depth_for_2d_input():
    two_d = struct.pack("<B", 1) + struct.pack("<I", 1) + struct.pack("<dd", 1.0, 2.0)

    assert encsounding.wkb_points(two_d) == ()


def test_soundings_from_records_keeps_order_and_ignores_other_layers():
    records = (
        SimpleNamespace(source_class="DEPARE", wkb=b"not parsed"),
        SimpleNamespace(
            source_class="SOUNDG", wkb=_iso_multipoint_z(((1.0, 2.0, 3.5),))
        ),
        SimpleNamespace(
            source_class="SOUNDG",
            wkb=_ewkb_multipoint_z(((4.0, 5.0, 6.5), (7.0, 8.0, 9.5))),
        ),
    )

    soundings = encsounding.soundings_from_records(records)

    np.testing.assert_array_equal(
        soundings,
        np.array([[1.0, 2.0, 3.5], [4.0, 5.0, 6.5], [7.0, 8.0, 9.5]]),
    )


def test_soundings_from_records_skips_malformed_blob():
    records = (
        SimpleNamespace(source_class="SOUNDG", wkb=b"\x01\x04"),
        SimpleNamespace(
            source_class="SOUNDG", wkb=_iso_multipoint_z(((1.0, 2.0, 3.5),))
        ),
    )

    np.testing.assert_array_equal(
        encsounding.soundings_from_records(records), np.array([[1.0, 2.0, 3.5]])
    )


def test_scale_gate_hides_soundings_above_100():
    points = [(10.0, 10.0, 3.5)]

    assert encsounding.collect_soundings(points, scale=100.0, width=100, height=100)
    assert (
        encsounding.collect_soundings(points, scale=100.5, width=100, height=100) == ()
    )
    # None follows the mesh builder's "no gates applied" convention.
    assert encsounding.collect_soundings(points, scale=None, width=100, height=100)


def test_viewport_cull_is_half_open():
    points = [
        (-0.1, 10.0, 1.0),
        (0.0, 0.0, 2.0),
        (99.0, 49.0, 3.0),
        (100.0, 10.0, 4.0),
        (10.0, 50.0, 5.0),
    ]

    labels = encsounding.collect_soundings(points, width=100, height=50)

    assert [label.text for label in labels] == ["2.0", "3.0"]
    assert [(label.x, label.y) for label in labels] == [(2.0, 2.0), (101.0, 51.0)]


def test_duplicate_box_collapses_and_different_boxes_survive():
    points = [
        (10.0, 10.0, 3.24),
        (10.0, 12.0, 4.5),  # same 32x16 box: (0, 0)
        (50.0, 10.0, 6.04),  # box (1, 0)
        (10.0, 20.0, 7.0),  # box (0, 1)
    ]

    labels = encsounding.collect_soundings(points, width=200, height=100)

    assert [(label.x, label.y, label.text) for label in labels] == [
        (12.0, 12.0, "3.2"),
        (52.0, 12.0, "6.0"),
        (12.0, 22.0, "7.0"),
    ]
    assert all(label.anchor == "baseline-left" for label in labels)


def test_non_finite_depths_are_skipped():
    points = [
        (10.0, 10.0, math.nan),
        (50.0, 10.0, math.inf),
        (90.0, 10.0, 2.25),
    ]

    labels = encsounding.collect_soundings(points, width=200, height=100)

    assert [(label.x, label.y, label.text) for label in labels] == [(92.0, 12.0, "2.2")]


def test_shared_box_set_is_mutated_for_callers():
    boxes = set()

    labels = encsounding.collect_soundings(
        [(10.0, 10.0, 1.0)], width=100, height=100, boxes=boxes
    )

    assert len(labels) == 1
    assert boxes == {(0, 0)}


def test_project_points_subtracts_origin_then_projects():
    soundings = np.array([[100.0, 200.0, 3.5], [101.0, 202.0, 4.5]])
    seen = {}

    def projection(source):
        seen["dtype"] = source.dtype
        return source * 2.0

    screen = encsounding.project_points(soundings, projection, origin=(100.0, 200.0))

    np.testing.assert_array_equal(screen, np.array([[0.0, 0.0], [2.0, 4.0]]))
    assert seen["dtype"] == np.float64


def test_project_points_rejects_wrong_shape():
    with pytest.raises(ValueError):
        encsounding.project_points(np.zeros((1, 2)), None)
