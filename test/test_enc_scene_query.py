"""Query and record fidelity checks for the Stage 1 ENC source scene (#215).

Oracle results come from a fresh OGR datasource with ``SetSpatialFilter``, the
path the scene must reproduce.  The shared GPKG fixture lives in
``test.enc_scene_fixtures``.
"""

import dataclasses
import sys

import pytest
from osgeo import ogr, osr

from sailworks.rendering.vectordrawer import encscene
from sailworks.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer
from test.enc_scene_fixtures import (
    _bbox,
    _CancelAfter,
    _feature,
    _polygon,
    cell,
    scene,
)


def _oracle(path, bounding):
    datasource = ogr.Open(str(path), 0)
    identities = []
    try:
        for index in range(datasource.GetLayerCount()):
            layer = datasource.GetLayerByIndex(index)
            name = layer.GetName()
            if (
                name in encscene.SKIP_LAYERS
                or layer.GetLayerDefn().GetGeomFieldCount() == 0
            ):
                continue
            layer.SetSpatialFilter(bounding)
            layer.ResetReading()
            for feature in layer:
                identities.append((index, name, feature.GetFID()))
    finally:
        datasource = None
    return identities


VIEWS = (
    (-1, -1, 201, 201),
    (4.5, 4.5, 5.5, 5.5),
    (49, 49, 51, 51),
    (30.5, 30.5, 30.9, 30.9),
    (24, 0.5, 25, 1),
    (8, 0.5, 9, 1),
    (99, 99, 102, 102),
)


@pytest.mark.parametrize("box", VIEWS)
def test_query_matches_ogr_candidate_identity_and_order(cell, scene, box):
    bounding = _bbox(*box)
    result = scene.query(bounding)
    assert result is not None
    assert [(r.layer_index, r.source_class, r.fid) for r in result] == _oracle(
        cell, bounding
    )


def test_holes_multipoints_and_multipolygons_survive(scene):
    hole_view = scene.query(_bbox(4.5, 4.5, 5.5, 5.5))
    identities = {(r.layer_index, r.source_class, r.fid) for r in hole_view}
    assert (0, "DEPARE", 1) not in identities  # bounding box is inside the hole
    assert (2, "SOUNDG", 1) in identities  # one point of a multipoint

    split_view = {r.fid for r in scene.query(_bbox(30.5, 30.5, 30.9, 30.9))}
    assert split_view == {2}  # second member of a multipolygon

    point_view = {r.fid for r in scene.query(_bbox(49, 49, 51, 51))}
    assert point_view == {1}

    empty = scene.query(_bbox(24, 0.5, 25, 1))
    assert empty == ()  # envelope overlap without geometry intersection


def test_records_keep_layer_fid_order_and_source_identity(scene):
    result = scene.query(_bbox(-1, -1, 201, 201))
    assert [r.layer_index for r in result] == sorted(r.layer_index for r in result)
    by_layer = {}
    for record in result:
        by_layer.setdefault(record.layer_index, []).append(record.fid)
    for fids in by_layer.values():
        assert fids == sorted(fids)
    assert len({record.sequence for record in scene.records}) == len(scene.records)

    again = scene.query(_bbox(-1, -1, 201, 201))
    assert again == result
    assert all(left is right for left, right in zip(result, again))


def test_scene_uses_drawer_layer_rules(scene):
    assert encscene.SKIP_LAYERS == S57ChartDrawer.SKIP_LAYERS
    assert encscene.ATTRIBUTE_FIELDS == S57ChartDrawer.ATTRIBUTE_FIELDS
    names = {record.source_class for record in scene.records}
    assert names == {"DEPARE", "DEPCNT", "SOUNDG", "BOYLAT"}
    assert "ATTRONLY" not in names


def test_records_are_immutable_and_hold_one_wkb_copy(scene):
    names = {field.name for field in dataclasses.fields(encscene.FeatureRecord)}
    assert names == {
        "layer_index",
        "sequence",
        "fid",
        "source_class",
        "attributes",
        "envelope",
        "wkb",
    }
    assert all(isinstance(record.wkb, bytes) and record.wkb for record in scene.records)
    assert scene.nbytes >= sum(len(record.wkb) for record in scene.records)
    with pytest.raises(dataclasses.FrozenInstanceError):
        scene.records[0].fid = 99

    depare = next(
        record
        for record in scene.records
        if record.source_class == "DEPARE" and record.fid == 1
    )
    assert depare.attributes_dict == {"DRVAL1": 5.0, "DRVAL2": 9.0}
    boylat = next(
        record
        for record in scene.records
        if record.source_class == "BOYLAT" and record.fid == 1
    )
    # Preserve OGR's raw GetField representation for the drawer; this GDAL
    # renders an integer list as a string, not a Python tuple.
    assert boylat.attribute("COLOUR") == "(2:3,4)"


def test_cancelled_query_returns_nothing_and_no_partial_tuple(scene):
    bounding = _bbox(-1, -1, 201, 201)
    assert scene.query(bounding, _CancelAfter(1)) is None
    assert scene.query(bounding, _CancelAfter(2)) is None


def test_cancelled_query_is_polled_during_the_scan(scene):
    cancel = _CancelAfter(6)
    assert scene.query(_bbox(-1, -1, 201, 201), cancel) is None
    assert cancel.calls > 2


def test_scene_nbytes_is_conservative(scene):
    independent = sum(
        sys.getsizeof(record)
        + sys.getsizeof(record.attributes)
        + sys.getsizeof(record.envelope)
        + sys.getsizeof(record.wkb)
        for record in scene.records
    )
    independent += sum(
        sys.getsizeof(layer) + sys.getsizeof(layer.grid) for layer in scene.layers
    )
    assert scene.nbytes >= independent


def _write_dense_cell(path, side=16):
    datasource = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = datasource.CreateLayer("DEPARE", srs, ogr.wkbPolygon)
    for row in range(side):
        for column in range(side):
            x = column * 10.0
            y = row * 10.0
            _feature(
                layer,
                _polygon([(x, y), (x + 8, y), (x + 8, y + 8), (x, y + 8)]),
            )
    datasource = None


def test_grid_reduces_candidates_and_preserves_order(tmp_path):
    path = tmp_path / "dense.gpkg"
    _write_dense_cell(path)
    scene = encscene.open_cell_scene(path, encscene.cell_version(path))
    layer = scene.layers[0]
    bounding = _bbox(0, 0, 5, 5)
    assert len(layer.envelopes) == 256
    assert len(layer.candidate_offsets(bounding)) <= 16
    assert [record.fid for record in scene.query(bounding)] == [1]

    for box in ((0, 0, 25, 25), (15, 15, 40, 40), (0, 0, 158, 158)):
        bounding = _bbox(*box)
        result = scene.query(bounding)
        assert [(r.layer_index, r.source_class, r.fid) for r in result] == _oracle(
            path, bounding
        )
