"""Backend-neutral, opt-in composition plan for stacked ENC cells.

``plan_cells`` reproduces :meth:`ENCOverlayRenderer._render`'s ordering and
coverage-difference policy exactly, but returns ordered data instead of drawing
raster content.  Nothing here builds a mesh, draws, or touches the default
renderer: the caller decides what a plan entry means for its own backend.

Clip geometries are shapely objects, the representation
:func:`encmesh.coverage_from_datasource` already returns and
:func:`encmesh.build_scene_mesh` accepts.  A Cairo caller can convert one with
``ogr.CreateGeometryFromWkb(shapely.to_wkb(clip))``.
"""

from __future__ import annotations

import dataclasses
import os

import shapely
from osgeo import ogr

from sailworks.core.enccharts import chart_scale_rank, chart_scale_visible
from sailworks.rendering.chartlayer import ChartLayer
from sailworks.rendering.vectordrawer import encmesh, encscene


@dataclasses.dataclass(frozen=True)
class CellPlan:
    """One cell's place in an ordered composition plan."""

    rank_key: tuple
    path: str
    version: tuple | None
    #: The cell's coverage intersected with the view and minus the ground
    #: already owned by better-ranked cells: the exact visible region.
    clip: object | None
    coverage_known: bool
    #: The cell's own authoritative coverage, untrimmed by the view.  It is
    #: the view-independent clip a reusable per-cell mesh can be built from.
    coverage: object | None = None


def _cancelled(cancelled) -> bool:
    return cancelled is not None and bool(cancelled())


def _bounding(viewport):
    """The view polygon as shapely plus OGR's (min_x, max_x, min_y, max_y)."""
    p1, p2 = viewport.get_bbox()
    wkt = ChartLayer.get_bounding_wkt_of_coords(*p1.get_degrees(), *p2.get_degrees())
    geometry = ogr.CreateGeometryFromWkt(wkt)
    return shapely.from_wkb(bytes(geometry.ExportToWkb())), geometry.GetEnvelope()


def _open_coverage(path):
    datasource = ogr.Open(os.fspath(path), 0)
    if datasource is None:
        raise OSError(f"cannot open ENC cell: {path}")
    try:
        return encmesh.coverage_from_datasource(datasource)
    finally:
        datasource = None


def plan_cells(
    viewport,
    charts,
    cancelled=None,
    coverage_loader=_open_coverage,
    version_of=encscene.cell_version,
):
    """Order ``charts`` the way the raster renderer would, without drawing.

    ``charts`` is the renderer's own sequence of ``(path, native_scale,
    envelopes)`` entries.  The result is a tuple of :class:`CellPlan` in draw
    order; each clip is the cell's coverage intersected with the view and
    minus the ground already owned by better-ranked cells.  ``coverage`` keeps
    the cell's own untrimmed coverage so a per-cell mesh can be built once,
    independent of the view, and reused across view changes.  A cell with
    unknown coverage (``coverage_known`` false) is drawn over the full view and
    never masks a later cell; it has no ``coverage`` either.

    ``cancelled`` is polled once per cell, as in ``_render``.  A cancelled
    plan returns ``None``, never a partial tuple.  ``coverage_loader`` and
    ``version_of`` are injection points for tests; the defaults read M_COVR
    from the cell file and bound the read with ``encscene.cell_version``.
    """
    scale = viewport.get_scale()
    bounding, (min_lon, max_lon, min_lat, max_lat) = _bounding(viewport)
    # Same key as _render: best scale rank first, chart path breaking ties.
    charts = sorted(
        charts,
        key=lambda c: (chart_scale_rank(c[1], scale), c[0]),
    )
    covered = None
    plan: list[CellPlan] = []
    for path, native_scale, envelopes in charts:
        if _cancelled(cancelled):
            return None
        if not chart_scale_visible(native_scale, scale):
            continue
        if envelopes and not any(
            a <= max_lon and b >= min_lon and c <= max_lat and d >= min_lat
            for a, b, c, d in envelopes
        ):
            continue
        version = version_of(path)
        region = coverage_loader(path)
        if version_of(path) != version:
            raise ValueError(f"cell changed while it was being planned: {path}")
        known_coverage = region is not None
        own_coverage = region
        region = (
            shapely.intersection(region, bounding) if region is not None else bounding
        )
        if covered is not None:
            region = shapely.difference(region, covered)
        if region.is_empty:
            continue
        if known_coverage:
            covered = region if covered is None else shapely.union(covered, region)
        plan.append(
            CellPlan(
                rank_key=chart_scale_rank(native_scale, scale),
                path=path,
                version=version,
                clip=region,
                coverage_known=known_coverage,
                coverage=own_coverage,
            )
        )
    if _cancelled(cancelled):
        return None
    return tuple(plan)
