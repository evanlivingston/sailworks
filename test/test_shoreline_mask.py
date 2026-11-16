"""The conservative routing mask must never change an exact shoreline decision."""

from pathlib import Path
from tempfile import TemporaryDirectory

from osgeo import ogr

from sailworks.core.geo.shoreline import ShorelineValidity

SEGMENTS = [
    (0.5, 0.5, 0.5, 0.6),
    (0.5, 0.5, 0.5, 0.55),
    (0.0, -0.05, 0.0, 0.05),
    (-0.05, -0.05, 0.05, 0.05),
    (0.0, -0.03, 0.0, 0.03),
]


def _shoreline(root):
    directory = Path(root) / "GSHHS_shp" / "h"
    directory.mkdir(parents=True)
    dataset = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(
        str(directory / "GSHHS_h_L1.shp")
    )
    layer = dataset.CreateLayer("land", geom_type=ogr.wkbPolygon)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(
        ogr.CreateGeometryFromWkt(
            "POLYGON ((-0.02 -0.02, 0.02 -0.02, 0.02 0.02, " "-0.02 0.02, -0.02 -0.02))"
        )
    )
    layer.CreateFeature(feature)
    dataset = None
    return str(root)


def test_mask_only_skips_segments_that_are_definitely_water():
    with TemporaryDirectory() as directory:
        root = _shoreline(directory)
        masked = ShorelineValidity(root, bounds=(-1, -1, 1, 1))
        exact = ShorelineValidity(root)
        assert masked.lines_validity(SEGMENTS) == exact.lines_validity(SEGMENTS)
        # A water segment is skipped without exact geometry; a segment crossing
        # or near the island is not skipped and uses the exact predicate.
        assert masked._segments_are_water([0.5], [0.5], [0.6], [0.6]) is True
        assert masked._segments_are_water([0.0], [-0.05], [0.0], [0.05]) is False
        assert masked.lines_validity([(0.0, -0.05, 0.0, 0.05)]) == [False]
        for segment in SEGMENTS:
            assert masked.segment_allowed(*segment) == exact.segment_allowed(*segment)
        payload = masked.native_mask()
        assert payload is not None
        assert payload["mask"].dtype.name == "uint8"
        assert payload["mask"].any()
        assert payload["resolution"] > 0
