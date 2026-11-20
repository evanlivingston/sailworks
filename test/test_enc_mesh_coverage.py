"""Focused checks for ENC coverage clipping and Qt item state.

These cover exact M_COVR clipping, CATCOV 1/2/absent semantics, a required
authoritative region for quilt clipping, boundary-stroke clipping after width
expansion, and stale-item state.  Mesh geometry construction lives in
:mod:`test_enc_mesh`.
"""

from __future__ import annotations

import os

# Must precede any PySide6 import, including the lazy imports in ``_qt``, so
# the headless platform is chosen before Qt initialises its platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, Polygon, box

from sailworks.rendering.vectordrawer.encmesh import (
    AREA_OUTLINE,
    EMPTY_MESH,
    LINE,
    UnsupportedGeometry,
    build_scene_mesh,
    coverage_from_datasource,
)

pytestmark = pytest.mark.skipif(
    not hasattr(shapely, "constrained_delaunay_triangles"),
    reason="GEOS constrained Delaunay triangulation unavailable",
)


@dataclass(frozen=True)
class _Record:
    layer_index: int
    sequence: int
    fid: int
    source_class: str
    wkb: bytes


def _scene(*specs):
    """A minimal stand-in for ``encscene.CellScene``: (layer, class, geometry)."""
    records = []
    for sequence, (layer_index, source_class, geometry) in enumerate(specs):
        records.append(
            _Record(
                layer_index=layer_index,
                sequence=sequence,
                fid=1000 + sequence,
                source_class=source_class,
                wkb=shapely.to_wkb(geometry),
            )
        )
    return SimpleNamespace(records=tuple(records))


def _triangles(source) -> np.ndarray:
    positions = getattr(source, "positions", source)
    assert positions.shape[0] % 3 == 0
    return positions.reshape(-1, 3, 2)


def _area(triangles: np.ndarray) -> float:
    a = triangles[:, 0]
    b = triangles[:, 1]
    c = triangles[:, 2]
    ab = b - a
    ac = c - a
    cross = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
    return float(np.abs(cross).sum() / 2.0)


def test_coverage_clipping_is_exact():
    hole = [(2, 2), (8, 2), (8, 8), (2, 8)]
    area = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [hole])
    coverage = box(0, 0, 5, 10)
    mesh = build_scene_mesh(
        _scene((0, "DEPARE", area)),
        stroke_width=0.1,
        coverage=coverage,
        area_outlines=False,
    )
    assert mesh.coverage_applied is True
    expected = area.intersection(coverage).area
    assert expected == pytest.approx(32.0)
    assert _area(_triangles(mesh)) == pytest.approx(expected, rel=1e-5)


def test_feature_outside_coverage_is_not_an_error():
    mesh = build_scene_mesh(
        _scene((0, "DEPARE", box(20, 20, 21, 21)), (0, "DEPARE", box(0, 0, 1, 1))),
        stroke_width=0.1,
        coverage=box(0, 0, 5, 5),
        area_outlines=False,
    )
    assert [part.sequence for part in mesh.parts] == [1]
    assert mesh.unsupported == ()


class _FakeMGeometry:
    def __init__(self, geometry):
        self._geometry = geometry

    def IsEmpty(self):
        return self._geometry.is_empty

    def ExportToWkb(self):
        return shapely.to_wkb(self._geometry)


class _FakeMFeature:
    def __init__(self, geometry, catcov):
        self._geometry = geometry
        self._catcov = catcov

    def GetGeometryRef(self):
        return _FakeMGeometry(self._geometry)

    def GetField(self, index):
        return self._catcov


class _FakeMDefn:
    def __init__(self, catcov_index):
        self._catcov_index = catcov_index

    def GetFieldIndex(self, name):
        assert name == "CATCOV"
        return self._catcov_index


class _FakeMLayer:
    def __init__(self, features, catcov_index=0):
        self._features = tuple(features)
        self._catcov_index = catcov_index

    def SetSpatialFilter(self, value):
        pass

    def SetAttributeFilter(self, value):
        pass

    def ResetReading(self):
        pass

    def GetLayerDefn(self):
        return _FakeMDefn(self._catcov_index)

    def __iter__(self):
        return iter(self._features)


class _FakeDatasource:
    def __init__(self, layer):
        self._layer = layer

    def GetLayerByName(self, name):
        return self._layer if name == "M_COVR" else None


def _coverage(*specs, catcov_index=0):
    """A datasource whose M_COVR layer holds ``(geometry, CATCOV)`` features."""
    features = [_FakeMFeature(geometry, catcov) for geometry, catcov in specs]
    return _FakeDatasource(_FakeMLayer(features, catcov_index=catcov_index))


def test_catcov_absent_is_unknown_coverage():
    # An M_COVR polygon with no CATCOV value says nothing; it must not become
    # an invented clip polygon.
    assert coverage_from_datasource(_coverage((box(0, 0, 10, 10), None))) is None


def test_catcov_field_missing_is_unknown_coverage():
    assert (
        coverage_from_datasource(_coverage((box(0, 0, 10, 10), 1), catcov_index=-1))
        is None
    )


def test_catcov_two_only_is_not_full_coverage():
    assert coverage_from_datasource(_coverage((box(0, 0, 10, 10), 2))) is None


def test_catcov_one_is_positive_and_two_is_subtracted():
    available = box(0, 0, 10, 10)
    denied = box(0, 0, 5, 10)
    absent = box(20, 20, 21, 21)
    result = coverage_from_datasource(
        _coverage(
            (available, 1),
            (denied, 2),
            (absent, None),
        )
    )
    assert result is not None
    assert result.area == pytest.approx(50.0)
    assert shapely.contains_xy(result, 7.5, 5.0)
    assert not shapely.contains_xy(result, 2.5, 5.0)
    assert not shapely.contains_xy(result, 20.5, 20.5)


def test_quilt_clip_without_coverage_is_explicitly_unsupported():
    with pytest.raises(UnsupportedGeometry, match="authoritative M_COVR"):
        build_scene_mesh(
            _scene((0, "DEPARE", box(0, 0, 1, 1))),
            stroke_width=0.1,
            clip=True,
        )


def test_quilt_clip_with_coverage_is_applied_and_reported():
    mesh = build_scene_mesh(
        _scene((0, "DEPARE", box(0, 0, 10, 10))),
        stroke_width=0.1,
        coverage=box(0, 0, 5, 10),
        clip=True,
        area_outlines=False,
    )
    assert mesh.coverage_applied is True
    assert mesh.coverage_state == "applied"
    assert _area(_triangles(mesh)) == pytest.approx(50.0)


def test_explicit_no_clip_keeps_the_full_cell():
    mesh = build_scene_mesh(
        _scene((0, "DEPARE", box(0, 0, 10, 10))),
        stroke_width=0.1,
        coverage=box(0, 0, 5, 10),
        clip=False,
        area_outlines=False,
    )
    assert mesh.coverage_applied is False
    assert mesh.coverage_state == "none"
    assert _area(_triangles(mesh)) == pytest.approx(100.0)


def test_boundary_crossing_stroke_is_clipped_after_expansion():
    # Expanded first, then clipped: the stroke's centreline is inside the
    # region, but its width would otherwise bleed below y = 0.
    mesh = build_scene_mesh(
        _scene((0, "COALNE", LineString([(2, 0.2), (8, 0.2)]))),
        stroke_width=2.0,
        coverage=box(0, 0, 10, 10),
        area_outlines=False,
    )
    positions = mesh.positions
    assert [part.kind for part in mesh.parts] == [LINE]
    assert positions[:, 1].min() >= -1e-9
    assert positions[:, 1].max() <= 1.2 + 1e-9

    # A segment that runs across the region edge is trimmed, not drawn wide.
    crossing = build_scene_mesh(
        _scene((0, "COALNE", LineString([(4, 5), (15, 5)]))),
        stroke_width=1.0,
        coverage=box(0, 0, 10, 10),
        area_outlines=False,
    )
    positions = crossing.positions
    assert positions[:, 0].max() <= 10.0 + 1e-9
    assert positions[:, 0].min() >= 4.0 - 1e-9
    assert positions[:, 1].min() >= 4.5 - 1e-9
    assert positions[:, 1].max() <= 5.5 + 1e-9


def test_area_outline_is_clipped_after_expansion():
    mesh = build_scene_mesh(
        _scene((0, "DEPARE", box(4, 4, 14, 14))),
        stroke_width=2.0,
        coverage=box(0, 0, 10, 10),
        area_outlines=True,
    )
    outline = mesh.part_positions(
        next(part for part in mesh.parts if part.kind == AREA_OUTLINE)
    )
    assert outline[:, 0].max() <= 10.0 + 1e-9
    assert outline[:, 1].max() <= 10.0 + 1e-9


def _qt():
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QGuiApplication

    # Both Qt test modules share one process: use the singleton and never
    # construct a QApplication on top of an existing QGuiApplication.
    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication([])
    return app


def test_apply_mesh_clears_stale_vertices():
    _qt()
    from PySide6.QtQuick import QSGGeometry

    from sailworks.qt.encgpu import _point2d_attributes, apply_mesh

    mesh = build_scene_mesh(
        _scene((0, "DEPARE", box(0, 0, 1, 1))), stroke_width=0.1, area_outlines=False
    )
    # The pinned set, not an inline temporary: the geometry keeps only a raw
    # pointer to it (see ``sailworks.qt.encgpu``).
    geometry = QSGGeometry(_point2d_attributes(), 0)
    assert apply_mesh(geometry, mesh) == mesh.vertex_count
    assert geometry.vertexCount() == mesh.vertex_count
    assert apply_mesh(geometry, EMPTY_MESH) == 0
    assert geometry.vertexCount() == 0


def test_apply_mesh_rejects_a_bad_projection():
    _qt()
    from PySide6.QtQuick import QSGGeometry

    from sailworks.qt.encgpu import _point2d_attributes, apply_mesh

    mesh = build_scene_mesh(
        _scene((0, "DEPARE", box(0, 0, 1, 1))), stroke_width=0.1, area_outlines=False
    )
    # The pinned set, not an inline temporary: the geometry keeps only a raw
    # pointer to it (see ``sailworks.qt.encgpu``).
    geometry = QSGGeometry(_point2d_attributes(), 0)
    with pytest.raises(ValueError):
        apply_mesh(geometry, mesh, projection=lambda positions: positions[:1])


def test_item_mesh_generation_and_clear():
    _qt()
    from sailworks.qt.encgpu import EncMeshItem

    mesh = build_scene_mesh(
        _scene((0, "DEPARE", box(0, 0, 1, 1))), stroke_width=0.1, area_outlines=False
    )
    item = EncMeshItem()
    assert item.generation == 0
    assert item.set_mesh(mesh) is True
    assert item.generation == 1
    assert item.set_mesh(mesh) is False
    assert item.generation == 1
    assert item.set_mesh(None) is True
    assert item.generation == 2
    assert item.mesh.is_empty
    assert item.unsupported_count == 0
