"""An unreadable ENC cell must not blank the chart.

``UnavailableCell`` claims that a cell the plan cannot read "contributes no
clip and no coverage claim - the remaining cells keep the ground they would
have had, and the raster stays visible over any area nothing readable covers."
These tests follow that claim through the real pieces it touches: the plan's
own clips, the coverage gate the raster is held behind, and the status text
that names the missing cell.

The cells are real GPKG files written by the same writer the plan tests use;
the unreadable one is a real cell truncated to zero bytes, so the failure goes
through ``encscene.cell_version``/``_open_coverage`` rather than an injected
stub.  Nothing here draws, opens a window or touches the network.
"""

from __future__ import annotations

import os
from types import MethodType, SimpleNamespace

import pytest
import shapely
from osgeo import ogr

from sailworks.qt import encgpu, encscenelayer, gribviewport
from sailworks.rendering import encplan


class _Point:
    def __init__(self, lat, lon):
        self._degrees = (lat, lon)

    def get_degrees(self):
        return self._degrees


class _Viewport:
    """The plan's viewport protocol over a one-degree cell of sea."""

    def __init__(self, scale=1.0, first=(50.0, -5.0), second=(51.0, -4.0)):
        self._scale = scale
        self._first = _Point(*first)
        self._second = _Point(*second)

    def get_bbox(self):
        return self._first, self._second

    def get_scale(self):
        return self._scale


def _polygon(bounds):
    min_x, min_y, max_x, max_y = bounds
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in (
        (min_x, min_y),
        (max_x, min_y),
        (max_x, max_y),
        (min_x, max_y),
        (min_x, min_y),
    ):
        ring.AddPoint_2D(x, y)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def _cell(path, features):
    """Write a GPKG cell with an M_COVR layer of ``(bounds, catcov)`` entries."""
    datasource = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    layer = datasource.CreateLayer("M_COVR", geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("CATCOV", ogr.OFTInteger))
    for bounds, catcov in features:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(_polygon(bounds))
        if catcov is not None:
            feature.SetField("CATCOV", catcov)
        layer.CreateFeature(feature)
    datasource = None
    return str(path)


def _view(viewport):
    """The view polygon, from the plan's own unknown-coverage path."""
    (entry,) = encplan.plan_cells(
        viewport,
        [("view", 2500, None)],
        coverage_loader=lambda path: None,
        version_of=lambda path: None,
    )
    return entry.clip


def _halves(viewport):
    """A's ground on the west, B's on the east, together the whole view."""
    view = _view(viewport)
    min_x, min_y, max_x, max_y = view.bounds
    middle = (min_x + max_x) / 2.0
    return (
        (min_x, min_y, middle, max_y),
        (middle, min_y, max_x, max_y),
    )


def _composed_cells(tmp_path):
    """Plan A (readable) and B (a real cell truncated to nothing)."""
    viewport = _Viewport()
    a_ground, b_ground = _halves(viewport)
    good = _cell(tmp_path / "US4WA1IB.gpkg", [(a_ground, 1)])
    broken_path = tmp_path / "US5WA2IB.gpkg"
    broken = _cell(broken_path, [(b_ground, 1)])
    broken_path.write_bytes(b"")
    unavailable = []
    charts = [(broken, 2000, None), (good, 2500, None)]
    plan = encplan.plan_cells(viewport, charts, unavailable=unavailable)
    return viewport, plan, unavailable, good, broken, a_ground, b_ground


# ------------------------------------------------------------- the planner
def test_unreadable_cell_is_named_and_left_out_of_the_plan(tmp_path):
    _, plan, unavailable, good, broken, _, _ = _composed_cells(tmp_path)

    # The readable cell still composes at its rank, in plan order first
    # because it is the more detailed of the two.
    assert [entry.path for entry in plan] == [good]
    assert plan[0].coverage_known is True
    # Exactly one record, naming B's path with the production reason rather
    # than a silence.
    (record,) = unavailable
    assert isinstance(record, encplan.UnavailableCell)
    assert record.path == broken
    assert record.reason.startswith("cannot open ENC cell")
    assert broken in record.reason
    assert record.rank_key == encplan.chart_scale_rank(2000, 1.0)


# --------------------------------------------------- no coverage claim for B
def test_planned_clips_claim_a_ground_and_leave_b_ground_unclaimed(tmp_path):
    _, plan, _, _, _, a_ground, b_ground = _composed_cells(tmp_path)
    a_box = shapely.box(*a_ground)
    b_box = shapely.box(*b_ground)

    # A's ground is still claimed, in full, by A's own clip and coverage.
    assert plan[0].clip.intersection(a_box).area == pytest.approx(a_box.area)
    assert plan[0].coverage.intersection(a_box).area == pytest.approx(a_box.area)

    # B's ground is claimed by nothing in the plan: no clip, no coverage.
    # (Their shared edge is a zero-area line, not claimed ground.)
    assert plan[0].clip.intersection(b_box).area == 0.0
    assert plan[0].coverage.intersection(b_box).area == 0.0
    claimed = shapely.union_all([entry.clip for entry in plan])
    assert claimed.intersection(b_box).area == 0.0
    # So the composed set leaves exactly B's half to the raster underneath.
    assert claimed.area < a_box.area + b_box.area


# ------------------------------------------------ the consequence: the gate
def _covers(boxes, region):
    """``coverage_boxes_cover`` over ``region``, with its corner at the origin.

    The real gate maps drawn cells into item pixels and asks whether their
    boxes cover the whole item rect; shifting a region to the origin lets the
    same function answer "is this ground covered?".
    """
    x0, y0, x1, y1 = region
    shifted = [(b[0] - x0, b[1] - y0, b[2] - x0, b[3] - y0) for b in boxes]
    return encgpu.coverage_boxes_cover(shifted, x1 - x0, y1 - y0)


def _in_pixels(regions, view, size=1000.0):
    """Map ground regions into the item-pixel rect the gate samples.

    The gate's tolerance is half an item pixel, so grounds are measured in
    pixels (the viewport is ``size`` across) rather than in degrees.
    """
    x0, y0, x1, y1 = view
    scale_x = size / (x1 - x0)
    scale_y = size / (y1 - y0)
    return [
        (
            (r[0] - x0) * scale_x,
            (r[1] - y0) * scale_y,
            (r[2] - x0) * scale_x,
            (r[3] - y0) * scale_y,
        )
        for r in regions
    ]


def test_the_gate_does_not_claim_the_view_so_the_raster_stays_up(tmp_path):
    viewport, plan, _, _, _, a_ground, b_ground = _composed_cells(tmp_path)
    view = _view(viewport).bounds
    drawn = _in_pixels([entry.clip.bounds for entry in plan], view)
    a_box, b_box = _in_pixels([a_ground, b_ground], view)
    whole = (0.0, 0.0, 1000.0, 1000.0)

    # Over A's ground the drawn set still claims coverage: the raster is not
    # needed there and stays held back.
    assert _covers(drawn, a_box) is True
    # Over B's ground it claims nothing, so the gate answers false and the
    # Cairo raster composes instead of being suppressed over a blank chart.
    assert _covers(drawn, b_box) is False
    assert _covers(drawn, whole) is False

    # Counterfactual: had B been readable, the two together would have
    # claimed the whole viewport and the raster would have been held back.
    assert _covers([a_box, b_box], whole) is True


# ---------------------------------------------------------------- the status
class _Scene:
    def __init__(self):
        self.props = {}

    def setProperty(self, name, value):
        self.props[name] = value


def _status_widget(states):
    """The viewport's real status method bound to the surface it reads."""
    widget_class = gribviewport._viewport_widget_class()
    widget = SimpleNamespace(
        _weather=_Scene(),
        _enc_scene_layer=True,
        _enc_scene_model=SimpleNamespace(
            state="ready", error="", cell_states=tuple(states)
        ),
        _enc_scene_provisional=False,
    )
    setattr(
        widget,
        "_update_enc_status",
        MethodType(widget_class._update_enc_status, widget),
    )
    return widget


def _published_cell_states(plan, unavailable):
    """The chain ``encscenelayer`` runs: metadata -> skipped -> cell states.

    ``compose`` records the plan's unreadable cells as ``(path, reason)``
    pairs and names them in ``cells_order`` beside the drawable ones
    (``encscenelayer.py:1081-1098``).  From there this is production code:
    :func:`plan_unavailable` turns those records into the scene's skip list,
    and :func:`cell_states` resolves every named cell.
    """
    metadata = {
        # The same collapse ``compose`` performs when it writes its metadata.
        "unavailable": tuple((cell.path, cell.reason) for cell in unavailable),
        "cells_order": tuple(encscenelayer.cell_identity(cell) for cell in plan)
        + tuple(
            {
                "path": os.fspath(cell.path),
                "name": os.path.basename(cell.path),
                "rank_key": tuple(cell.rank_key),
                "version": None,
                "coverage_known": False,
            }
            for cell in unavailable
        ),
    }
    published = dict(metadata)
    published["skipped"] = tuple(
        published.get("skipped", ())
    ) + encscenelayer.plan_unavailable(published)
    published["cell_states"] = encscenelayer.cell_states(
        published["cells_order"], published["skipped"]
    )
    return published


def test_status_names_the_unreadable_cell_it_planned(tmp_path):
    _, plan, unavailable, good, broken, _, _ = _composed_cells(tmp_path)

    published = _published_cell_states(plan, unavailable)
    states = {cell["path"]: cell for cell in published["cell_states"]}
    name = os.path.basename(broken)

    # The plan's own UnavailableCell reaches the reported states as
    # "unavailable", named after B, with its reason attached; A is untouched.
    assert states[good]["state"] == encscenelayer.STATE_READY
    assert states[broken]["state"] == encscenelayer.STATE_UNAVAILABLE
    assert states[broken]["name"] == name
    assert states[broken]["reason"].startswith("cannot open ENC cell")

    widget = _status_widget(published["cell_states"])
    widget._update_enc_status()

    assert widget._weather.props["encStatus"] == (
        f"ENC chart unavailable in part: {name}"
    )
