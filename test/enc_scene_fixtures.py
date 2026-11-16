"""Shared GPKG fixture and geometry helpers for the ENC source scene tests.

The fixture is a GPKG cell with holes, multipolygons, multipoints, skipped
layers and attribute-only tables.  Oracle results come from a fresh OGR
datasource with ``SetSpatialFilter``, the path the scene must reproduce.
"""

import os

import pytest
from osgeo import ogr, osr

from sailworks.rendering.vectordrawer import encscene


class _CancelAfter:
    """Cancel when the callback has been polled ``after`` times."""

    def __init__(self, after):
        self.after = after
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.calls >= self.after


def _ring(coordinates):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in coordinates:
        ring.AddPoint_2D(x, y)
    ring.CloseRings()
    return ring


def _polygon(*rings):
    polygon = ogr.Geometry(ogr.wkbPolygon)
    for coordinates in rings:
        polygon.AddGeometry(_ring(coordinates))
    return polygon


def _multipolygon(*polygons):
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for polygon in polygons:
        multi.AddGeometry(polygon)
    return multi


def _line(coordinates):
    line = ogr.Geometry(ogr.wkbLineString)
    for x, y in coordinates:
        line.AddPoint_2D(x, y)
    return line


def _multipoint(coordinates):
    multi = ogr.Geometry(ogr.wkbMultiPoint)
    for x, y in coordinates:
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(x, y)
        multi.AddGeometry(point)
    return multi


def _point(x, y):
    point = ogr.Geometry(ogr.wkbPoint)
    point.AddPoint_2D(x, y)
    return point


def _feature(layer, geometry, values=None):
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(geometry)
    for key, value in (values or {}).items():
        index = layer.GetLayerDefn().GetFieldIndex(key)
        if isinstance(value, list):
            feature.SetFieldIntegerList(index, value)
        else:
            feature.SetField(index, value)
    assert layer.CreateFeature(feature) == ogr.OGRERR_NONE


def _write_cell(path):
    datasource = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)

    depare = datasource.CreateLayer("DEPARE", srs, ogr.wkbMultiPolygon)
    for name, kind in (
        ("DRVAL1", ogr.OFTReal),
        ("DRVAL2", ogr.OFTReal),
        ("EXTR", ogr.OFTString),
    ):
        depare.CreateField(ogr.FieldDefn(name, kind))
    shell = [(0, 0), (10, 0), (10, 10), (0, 10)]
    hole = [(4, 4), (6, 4), (6, 6), (4, 6)]
    _feature(
        depare,
        _multipolygon(_polygon(shell, hole)),
        {"DRVAL1": 5.0, "DRVAL2": 9.0, "EXTR": "ignored"},
    )
    _feature(
        depare,
        _multipolygon(
            _polygon([(20, 20), (21, 20), (21, 21), (20, 21)]),
            _polygon([(30, 30), (31, 30), (31, 31), (30, 31)]),
        ),
        {"DRVAL1": 2.0, "DRVAL2": 4.0},
    )
    _feature(
        depare,
        _multipolygon(_polygon([(100, 100), (101, 100), (101, 101), (100, 101)])),
        {"DRVAL1": 1.0, "DRVAL2": 2.0},
    )

    depcnt = datasource.CreateLayer("DEPCNT", srs, ogr.wkbLineString)
    _feature(depcnt, _line([(0, 0), (10, 10)]))
    _feature(depcnt, _line([(0, 10), (10, 0)]))
    _feature(depcnt, _line([(100, 100), (101, 101)]))
    # Envelope covers (24, 0.5)-(25, 1); the line itself does not.
    _feature(depcnt, _line([(20, 0), (30, 10)]))

    soundg = datasource.CreateLayer("SOUNDG", srs, ogr.wkbMultiPoint)
    _feature(soundg, _multipoint([(5, 5), (50, 50)]))
    _feature(soundg, _multipoint([(200, 200)]))

    boylat = datasource.CreateLayer("BOYLAT", srs, ogr.wkbPoint)
    boylat.CreateField(ogr.FieldDefn("COLOUR", ogr.OFTIntegerList))
    _feature(boylat, _point(7, 7), {"COLOUR": [3, 4]})
    _feature(boylat, _point(60, 60), {"COLOUR": [1]})

    mcovr = datasource.CreateLayer("M_COVR", srs, ogr.wkbMultiPolygon)
    _feature(mcovr, _multipolygon(_polygon([(0, 0), (1, 0), (1, 1), (0, 1)])))

    attronly = datasource.CreateLayer("ATTRONLY", geom_type=ogr.wkbNone)
    attronly.CreateField(ogr.FieldDefn("NAME", ogr.OFTString))
    feature = ogr.Feature(attronly.GetLayerDefn())
    feature.SetField("NAME", "only")
    assert attronly.CreateFeature(feature) == ogr.OGRERR_NONE
    datasource = None


def _bbox(min_x, min_y, max_x, max_y):
    return _polygon([(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)])


@pytest.fixture(scope="module")
def cell(tmp_path_factory):
    path = tmp_path_factory.mktemp("encscene") / "synthetic.gpkg"
    _write_cell(path)
    return path


@pytest.fixture(scope="module")
def scene(cell):
    return encscene.open_cell_scene(cell, encscene.cell_version(cell))
