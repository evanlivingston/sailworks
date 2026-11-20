"""Focused checks for the opt-in compiled Earcut triangulation backend.

The default hybrid backend (vendored Python ear clipping below 32 ring
vertices, GEOS constrained Delaunay above) must stay unchanged.  Setting
``SAILWORKS_ENC_TRIANGULATOR=native-earcut`` uses the compiled
``mapbox_earcut`` package for every part.  These checks assert that the
triangles tile the polygon exactly: the union of the triangles is compared
against the polygon interior with a symmetric-difference area gate, so holes,
concavities, multipolygons and repaired bowties are covered without any
triangle crossing a ring.

The specific regression pinned here is ring mapping.  ``_rings_for_earcut``
returns hole *start* offsets, while ``mapbox_earcut`` wants cumulative ring
*end* offsets; a polygon whose last hole reaches the end of the vertex array
fails the union check if the two are confused.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon, box

from sailworks.rendering.vectordrawer import _earcut, encmesh
from sailworks.rendering.vectordrawer.encmesh import (
    TRIANGULATOR_HYBRID,
    TRIANGULATOR_NATIVE_EARCUT,
    UnsupportedGeometry,
    triangulate_area,
)

try:
    import mapbox_earcut  # noqa: F401
except ImportError:
    HAVE_NATIVE = False
else:
    HAVE_NATIVE = True

requires_native = pytest.mark.skipif(
    not HAVE_NATIVE, reason="mapbox_earcut is not installed"
)

_ENV = "SAILWORKS_ENC_TRIANGULATOR"

#: The hole at the end of the vertex array is deliberate: ring ends are
#: ``[4, 8, 13]`` for 13 vertices, so mistaking starts for ends is visible.
_TWO_HOLES = Polygon(
    [(0, 0), (12, 0), (12, 10), (0, 10)],
    [
        [(1, 1), (2, 1), (2, 2), (1, 2)],
        [(8, 1), (11, 1), (11, 3), (9, 3), (8, 3)],
    ],
)

_CASES = {
    "concave": Polygon([(0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6)]),
    "hole": Polygon(
        [(0, 0), (10, 0), (10, 10), (0, 10)], [[(2, 2), (4, 2), (4, 4), (2, 4)]]
    ),
    "two_holes": _TWO_HOLES,
    "multipolygon": MultiPolygon(
        [box(0, 0, 3, 3), Polygon([(5, 0), (9, 0), (9, 2), (5, 2)])]
    ),
    # Invalid on purpose: make_valid splits it into two triangles.
    "bowtie": Polygon([(0, 0), (4, 4), (4, 0), (0, 4)]),
}


def _triangles(flat: np.ndarray) -> np.ndarray:
    assert flat.dtype == np.float64
    assert flat.ndim == 2 and flat.shape[1] == 2
    assert flat.shape[0] % 3 == 0
    return flat.reshape(-1, 3, 2)


def _cross(triangles: np.ndarray) -> np.ndarray:
    a = triangles[:, 0]
    b = triangles[:, 1]
    c = triangles[:, 2]
    ab = b - a
    ac = c - a
    return ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]


def _assert_ccw(triangles: np.ndarray) -> None:
    assert triangles.shape[0] > 0
    assert (_cross(triangles) > 0.0).all()


def _assert_tiles(triangles: np.ndarray, geometry) -> None:
    """The triangle union equals the polygon interior, including holes."""
    target = geometry if shapely.is_valid(geometry) else shapely.make_valid(geometry)
    union = shapely.union_all([Polygon(triangle) for triangle in triangles])
    assert union.is_valid
    assert union.area == pytest.approx(target.area, rel=1e-12, abs=1e-12)
    uncovered = union.symmetric_difference(target).area
    assert uncovered == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("name", sorted(_CASES))
@requires_native
def test_native_backend_tiles_the_polygon_exactly(monkeypatch, name):
    monkeypatch.setenv(_ENV, TRIANGULATOR_NATIVE_EARCUT)
    geometry = _CASES[name]
    before = encmesh.native_earcut_calls()
    triangles = _triangles(triangulate_area(geometry))
    parts = len(encmesh._polygons(shapely.make_valid(geometry)))
    assert encmesh.native_earcut_calls() == before + parts
    _assert_ccw(triangles)
    _assert_tiles(triangles, geometry)


@requires_native
def test_native_backend_leaves_holes_uncovered(monkeypatch):
    monkeypatch.setenv(_ENV, TRIANGULATOR_NATIVE_EARCUT)
    triangles = _triangles(triangulate_area(_TWO_HOLES))
    union = shapely.union_all([Polygon(triangle) for triangle in triangles])
    _assert_tiles(triangles, _TWO_HOLES)
    for hole in _TWO_HOLES.interiors:
        hole_polygon = Polygon(hole)
        assert union.intersection(hole_polygon).area == pytest.approx(0.0, abs=1e-9)
        for x, y in list(hole.coords)[:-1]:
            assert not union.contains(Point(x, y))


@requires_native
def test_native_backend_is_used_for_small_parts(monkeypatch):
    """The hybrid threshold does not apply: a 4-vertex square is compiled."""
    monkeypatch.setenv(_ENV, TRIANGULATOR_NATIVE_EARCUT)
    before = encmesh.native_earcut_calls()
    triangles = _triangles(triangulate_area(box(0, 0, 1, 1)))
    assert encmesh.native_earcut_calls() == before + 1
    _assert_ccw(triangles)
    _assert_tiles(triangles, box(0, 0, 1, 1))


class _RecordingEarcut:
    """Stand-in for mapbox_earcut that records the ring array it receives."""

    def __init__(self):
        self.calls = []

    def triangulate_float64(self, vertices, rings):
        self.calls.append((vertices.copy(), rings.copy()))
        hole_starts = rings[:-1].tolist()
        return _earcut.earcut(vertices.ravel().tolist(), hole_starts, 2)


def test_ring_offsets_are_cumulative_ends(monkeypatch):
    """Starts must become ends: [4, 8] -> [4, 8, 13] for 13 vertices."""
    backend = _RecordingEarcut()
    monkeypatch.setitem(sys.modules, "mapbox_earcut", backend)
    monkeypatch.setenv(_ENV, TRIANGULATOR_NATIVE_EARCUT)

    before = encmesh.native_earcut_calls()
    triangles = _triangles(triangulate_area(_TWO_HOLES))
    assert encmesh.native_earcut_calls() == before + 1
    assert len(backend.calls) == 1
    vertices, rings = backend.calls[0]
    assert vertices.shape == (13, 2)
    assert vertices.dtype == np.float64
    assert rings.dtype == np.uint32
    # Hole starts 4 and 8, total 13: cumulative ends, not the raw starts.
    assert rings.tolist() == [4, 8, 13]
    _assert_tiles(triangles, _TWO_HOLES)


def test_default_backend_is_hybrid_and_needs_no_native_package(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "mapbox_earcut", None)
    assert encmesh.triangulator_backend() == TRIANGULATOR_HYBRID
    before = encmesh.native_earcut_calls()
    triangles = _triangles(triangulate_area(box(0, 0, 1, 1)))
    assert encmesh.native_earcut_calls() == before
    _assert_ccw(triangles)
    _assert_tiles(triangles, box(0, 0, 1, 1))


@pytest.mark.parametrize("value", ["", "hybrid", " HYBRID "])
def test_explicit_hybrid_backend(monkeypatch, value):
    monkeypatch.setenv(_ENV, value)
    assert encmesh.triangulator_backend() == TRIANGULATOR_HYBRID


def test_missing_native_dependency_is_actionable(monkeypatch):
    monkeypatch.setenv(_ENV, TRIANGULATOR_NATIVE_EARCUT)
    # Works whether or not the package is installed: a ``None`` entry makes
    # ``import mapbox_earcut`` raise ImportError.
    monkeypatch.setitem(sys.modules, "mapbox_earcut", None)
    before = encmesh.native_earcut_calls()
    with pytest.raises(UnsupportedGeometry) as excinfo:
        triangulate_area(box(0, 0, 1, 1))
    message = str(excinfo.value)
    assert "mapbox_earcut" in message
    assert f"{_ENV}={TRIANGULATOR_NATIVE_EARCUT}" in message
    assert "pip install mapbox_earcut" in message
    assert "unset" in message
    assert encmesh.native_earcut_calls() == before


def test_unknown_backend_is_an_explicit_error(monkeypatch):
    monkeypatch.setenv(_ENV, "gpu-earcut")
    with pytest.raises(UnsupportedGeometry) as excinfo:
        triangulate_area(box(0, 0, 1, 1))
    message = str(excinfo.value)
    assert _ENV in message
    assert TRIANGULATOR_HYBRID in message
    assert TRIANGULATOR_NATIVE_EARCUT in message


@requires_native
def test_native_backend_rejects_empty_geometry(monkeypatch):
    monkeypatch.setenv(_ENV, TRIANGULATOR_NATIVE_EARCUT)
    with pytest.raises(UnsupportedGeometry, match="no polygonal area"):
        triangulate_area(Polygon())
