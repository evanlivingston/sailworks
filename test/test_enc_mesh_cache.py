"""Focused checks for the reusable per-cell mesh cache and composite order.

The cache is what stops a view change from re-tessellating resident cells: a
cached mesh is clipped to the cell's own coverage, so its key does not depend
on the view, and the view's overlap suppression is done by painting cells
worst-ranked first instead of by rebuilding them against a per-view
``Difference(covered)`` region.
"""

from __future__ import annotations

import numpy as np
import shapely
from osgeo import ogr
from shapely.geometry import LineString, Polygon

from sailworks.rendering import encplan, encview
from sailworks.rendering.vectordrawer import encscene


def _record(sequence, source_class, geometry, attributes=()):
    min_x, min_y, max_x, max_y = geometry.bounds
    return encscene.FeatureRecord(
        layer_index=0,
        sequence=sequence,
        fid=sequence,
        source_class=source_class,
        attributes=tuple(attributes),
        envelope=(min_x, max_x, min_y, max_y),
        wkb=bytes(shapely.to_wkb(geometry)),
    )


def _scene(path, version, records, nbytes=256):
    envelopes = tuple(record.envelope for record in records)
    layers = ()
    if records:
        layers = (
            encscene.LayerCandidates(
                layer_index=0,
                source_class=records[0].source_class,
                start=0,
                envelopes=envelopes,
                grid=(0.0, 0.0, 1.0, 1.0, 1),
                cells=(tuple(range(len(records))),),
            ),
        )
    return encscene.CellScene(
        path=str(path),
        version=version,
        records=tuple(records),
        layers=layers,
        nbytes=nbytes,
    )


def _cell(tmp_path, name, records, nbytes=256):
    path = tmp_path / name
    path.write_bytes(b"cell")
    version = encscene.cell_version(path)
    return _scene(path, version, records, nbytes=nbytes)


def _plan(path, version, clip, known, coverage=None):
    return encplan.CellPlan(
        rank_key=(0, path),
        path=str(path),
        version=version,
        clip=clip,
        coverage_known=known,
        coverage=coverage,
    )


def _area(min_x, min_y, max_x, max_y, depth=10.0):
    return [
        _record(
            0,
            "DEPARE",
            Polygon([(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)]),
            (("DRVAL1", 0.0), ("DRVAL2", depth)),
        )
    ]


def _projection(points):
    return np.asarray(points, dtype=np.float64) * 2.0


def _box(min_x, min_y, max_x, max_y):
    return ogr.CreateGeometryFromWkt(
        f"POLYGON (({min_x} {min_y}, {max_x} {min_y}, {max_x} {max_y}, "
        f"{min_x} {max_y}, {min_x} {min_y}))"
    )


def _build(plan, cache, mesh_cache, *, bounding=None, cancelled=None):
    return encview.build_view_scene(
        plan,
        cache,
        projection=_projection,
        stroke_pixels=1.5,
        palette="cm93",
        cancelled=cancelled,
        bounding=bounding,
        mesh_cache=mesh_cache,
    )


def test_second_view_reuses_meshes_and_tessellates_nothing(tmp_path):
    cell = _cell(
        tmp_path,
        "reuse.000",
        _area(0, 0, 2, 2)
        + [_record(1, "COALNE", LineString([(0.2, 0.2), (1.8, 1.8)]))],
    )
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)

    # A cell wholly inside the view meshes to its own coverage, so the mesh is
    # view-independent and a second view reuses it.  A clip narrower than the
    # coverage is view-dependent and `_prepare_cell` deliberately does not
    # cache it.
    first = _build(
        (_plan(cell.path, cell.version, coverage, True, coverage),),
        cache,
        mesh_cache,
        bounding=_box(0, 0, 1.0, 2),
    )
    builds = mesh_cache.triangulations
    assert first is not None and builds == 1

    second = _build(
        (_plan(cell.path, cell.version, coverage, True, coverage),),
        cache,
        mesh_cache,
        bounding=_box(0.5, 0.5, 2, 2),
    )
    assert second is not None
    # No second tessellation: the cached mesh was reused as-is.
    assert mesh_cache.triangulations == builds
    assert second.mesh_hits == 1
    assert second.mesh_misses == 0
    np.testing.assert_array_equal(first.positions, second.positions)
    assert first.parts == second.parts
    assert first.cells[0].coverage_state == second.cells[0].coverage_state


def test_cell_version_change_invalidates_the_mesh(tmp_path):
    cell = _cell(tmp_path, "bump.000", _area(0, 0, 2, 2, depth=10.0))
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    first = _build(plan, cache, mesh_cache, bounding=_box(0, 0, 2, 2))
    assert first is not None and mesh_cache.triangulations == 1

    # A new version at the same path: different file content, new version.
    (tmp_path / "bump.000").write_bytes(b"cell-v2")
    newer = _scene(
        cell.path,
        encscene.cell_version(tmp_path / "bump.000"),
        _area(0, 0, 1, 1, depth=30.0),
    )
    assert newer.version != cell.version
    assert cache.put(newer)
    newer_plan = (_plan(newer.path, newer.version, coverage, True, coverage),)

    second = _build(newer_plan, cache, mesh_cache, bounding=_box(0, 0, 2, 2))
    assert second is not None
    # A changed version is a different key: it must be rebuilt, never stale.
    assert mesh_cache.triangulations == 2
    assert second.mesh_hits == 0 and second.mesh_misses == 1
    assert not np.array_equal(first.positions, second.positions)


def test_byte_bound_is_honoured(tmp_path):
    one = _cell(tmp_path, "one.000", _area(0, 0, 2, 2))
    two = _cell(tmp_path, "two.000", _area(3, 0, 5, 2))
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(one) and cache.put(two)
    plan = (
        _plan(
            one.path,
            one.version,
            shapely.box(0, 0, 2, 2),
            True,
            shapely.box(0, 0, 2, 2),
        ),
        _plan(
            two.path,
            two.version,
            shapely.box(3, 0, 5, 2),
            True,
            shapely.box(3, 0, 5, 2),
        ),
    )

    # A budget too small for one mesh refuses every entry and stays empty.
    tiny = encview.MeshCache(16)
    assembled = _build(plan, cache, tiny, bounding=_box(0, 0, 5, 2))
    assert assembled is not None
    assert tiny.resident_bytes == 0
    assert tiny.refused == 2
    assert assembled.mesh_bytes == 0
    assert assembled.mesh_hits == 0 and assembled.mesh_misses == 2

    # Wide enough for both: both stay resident and are accounted exactly once.
    roomy = encview.MeshCache(1_000_000)
    assembled = _build(plan, cache, roomy, bounding=_box(0, 0, 5, 2))
    assert assembled is not None
    assert roomy.resident_bytes <= roomy.max_bytes
    assert len(roomy._entries) == 2
    assert assembled.mesh_bytes == roomy.resident_bytes

    # A budget that holds exactly one of these two meshes keeps only one.
    sizes = sorted(entry.nbytes for entry in roomy._entries.values())
    single = encview.MeshCache(sizes[-1])
    _build(plan, cache, single, bounding=_box(0, 0, 5, 2))
    assert single.resident_bytes <= single.max_bytes
    assert len(single._entries) == 1
    assert single.evictions == 1

    # Wide enough for both: both stay resident and are accounted exactly once.
    roomy = encview.MeshCache(1_000_000)
    assembled = _build(plan, cache, roomy, bounding=_box(0, 0, 5, 2))
    assert assembled is not None
    assert roomy.resident_bytes <= roomy.max_bytes
    assert len(roomy._entries) == 2
    assert assembled.mesh_bytes == roomy.resident_bytes


def test_unknown_coverage_cell_is_unclipped_and_never_cached(tmp_path):
    cell = _cell(tmp_path, "unknown.000", _area(0, 0, 2, 2))
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, shapely.box(0, 0, 1, 1), False),)

    first = _build(plan, cache, mesh_cache, bounding=_box(0, 0, 4, 4))
    second = _build(plan, cache, mesh_cache, bounding=_box(0, 0, 4, 4))
    assert first is not None and second is not None
    assert first.cells[0].coverage_state == "none"
    assert first.positions[:, 0].max() >= 2.0
    # Unknown coverage is deliberately not given an invented clip, so its
    # view-dependent mesh is never reused across views.
    assert mesh_cache.triangulations == 2
    assert second.mesh_hits == 0
    # Neither assembly even asked the cache: an unknown cell has no stable key.
    assert second.mesh_misses == 0


def test_composite_order_is_the_reverse_of_the_plan(tmp_path):
    cells = [
        _cell(tmp_path, f"rank{index}.000", _area(index, 0, index + 1, 1))
        for index in range(3)
    ]
    cache = encscene.SceneCache(1_000_000)
    for cell in cells:
        assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = tuple(
        _plan(
            cell.path,
            cell.version,
            shapely.box(index, 0, index + 1, 1),
            True,
            shapely.box(index, 0, index + 1, 1),
        )
        for index, cell in enumerate(cells)
    )
    # Best-ranked first, exactly as encplan.plan_cells returns it.
    assert [entry.path for entry in plan] == [cell.path for cell in cells]

    assembled = _build(plan, cache, mesh_cache, bounding=_box(0, 0, 3, 1))
    assert assembled is not None
    # Painter's algorithm: worst-ranked drawn first, best-ranked last.
    assert [cell.path for cell in assembled.cells] == [
        cell.path for cell in reversed(cells)
    ]
    assert [cell.first_vertex for cell in assembled.cells] == sorted(
        cell.first_vertex for cell in assembled.cells
    )
    # The approximation is documented on the entry point, not just here.
    doc = encview.build_view_scene.__doc__ or ""
    assert "worst-ranked first" in doc
    assert "Difference(covered)" in doc


def test_cancellation_returns_none_even_with_a_warm_mesh(tmp_path):
    cell = _cell(tmp_path, "cancel.000", _area(0, 0, 2, 2))
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    assert _build(plan, cache, mesh_cache, bounding=_box(0, 0, 2, 2)) is not None
    assert mesh_cache.triangulations == 1
    assert _build(plan, cache, mesh_cache, cancelled=lambda: True) is None

    polls = {"count": 0}

    def cancelled():
        polls["count"] += 1
        return polls["count"] > 1

    # A cancel that trips after the first poll still returns no partial scene.
    assert _build(plan, cache, mesh_cache, cancelled=cancelled) is None
    # Cancelling never added a second tessellation to the warm cache.
    assert mesh_cache.triangulations == 1


def test_tolerance_matches_the_drawer_rule_and_zoom_invalidates(tmp_path):
    import math

    from sailworks.rendering.vectordrawer import encmesh

    # The drawer: min(lon_span/width, lat_span/height) * 0.5, floored to a
    # power of two.  Same inputs, same bucket.
    expected = min(0.5 / 1000, 0.25 / 500) * 0.5
    tolerance = encmesh.view_tolerance(0.5, 0.25, 1000, 500)
    assert tolerance == 2 ** math.floor(math.log2(expected))
    assert encmesh.quantize_tolerance(3 * 2**-10) == 2**-9
    assert encmesh.quantize_tolerance(0.0) == 0.0

    # The key varies with the bucket, so zooming across one invalidates.
    first = encview.MeshCache.key("cell.000", (1, 2, 3, 4), 2.0, tolerance, "cm93", 1.5)
    same = encview.MeshCache.key("cell.000", (1, 2, 3, 4), 2.0, tolerance, "cm93", 1.5)
    tighter = encview.MeshCache.key(
        "cell.000", (1, 2, 3, 4), 2.0, tolerance / 2, "cm93", 1.5
    )
    assert first == same
    assert first != tighter

    cell = _cell(tmp_path, "zoom.000", _area(0, 0, 2, 2))
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    def build(factor):
        return encview.build_view_scene(
            plan,
            cache,
            projection=lambda points: np.asarray(points) * factor,
            stroke_pixels=1.5,
            palette="cm93",
            mesh_cache=mesh_cache,
        )

    assert build(2.0) is not None
    assert mesh_cache.triangulations == 1
    zoomed = build(8.0)
    assert zoomed is not None
    # A different degrees-per-pixel bucket is a different key: no stale mesh.
    assert mesh_cache.triangulations == 2
    assert zoomed.mesh_hits == 0


def _fixed_frame_build(plan, cache, mesh_cache, zoom, **kwargs):
    """One assembly in the fixed reference frame at ``zoom``.

    The projection is world pixels at ``zoom`` and ``mesh_scale`` puts that in
    ``MESH_REFERENCE_ZOOM``, exactly as ``encscenelayer.mesh_scale_for`` does,
    so two different zooms describe the same frame.
    """
    return encview.build_view_scene(
        plan,
        cache,
        projection=lambda points, z=zoom: np.asarray(points, dtype=np.float64)
        * (2.0**z),
        stroke_pixels=1.5,
        palette="cm93",
        mesh_cache=mesh_cache,
        mesh_scale=2.0 ** (encview.MESH_REFERENCE_ZOOM - zoom),
        **kwargs,
    )


def test_fixed_frame_reference_is_the_chart_max_zoom():
    from sailworks.core import chscharts

    # The reference must stay the finest zoom the chart offers: that is the
    # tolerance the baked mesh is allowed to assume.
    assert encview.MESH_REFERENCE_ZOOM == float(chscharts.MAX_ZOOM)


def test_zoom_in_the_fixed_frame_reuses_the_mesh(tmp_path):
    cell = _cell(
        tmp_path,
        "zoomhit.000",
        _area(0, 0, 2, 2)
        + [_record(1, "COALNE", LineString([(0.2, 0.2), (1.8, 1.8)]))],
    )
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    first = _fixed_frame_build(plan, cache, mesh_cache, 11.0)
    assert first is not None and mesh_cache.triangulations == 1
    key = next(iter(mesh_cache._entries))
    # The defect this guards: neither the view scale nor the tolerance may
    # identify a fixed-frame mesh, or every zoom step re-tessellates.
    assert key[2] is None and key[3] is None

    zoomed = _fixed_frame_build(plan, cache, mesh_cache, 14.25)
    assert zoomed is not None
    # No new triangulation, and the geometry is the same source-unit mesh.
    assert mesh_cache.triangulations == 1
    assert zoomed.mesh_hits == 1 and zoomed.mesh_misses == 0
    np.testing.assert_array_equal(first.positions, zoomed.positions)
    assert first.parts == zoomed.parts
    assert first.vertex_colours().shape == zoomed.vertex_colours().shape


def test_fixed_frame_still_invalidates_on_a_cell_version(tmp_path):
    cell = _cell(tmp_path, "zoomver.000", _area(0, 0, 2, 2, depth=10.0))
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    assert _fixed_frame_build(plan, cache, mesh_cache, 12.0) is not None
    assert mesh_cache.triangulations == 1

    (tmp_path / "zoomver.000").write_bytes(b"cell-v2")
    newer = _scene(
        cell.path,
        encscene.cell_version(tmp_path / "zoomver.000"),
        _area(0, 0, 1, 1, depth=30.0),
    )
    assert cache.put(newer)
    newer_plan = (_plan(newer.path, newer.version, coverage, True, coverage),)

    rebuilt = _fixed_frame_build(newer_plan, cache, mesh_cache, 12.0)
    assert rebuilt is not None
    # Cell identity still wins over reuse: a new version is a new mesh.
    assert mesh_cache.triangulations == 2
    assert rebuilt.mesh_hits == 0 and rebuilt.mesh_misses == 1


def test_fixed_frame_meshes_are_still_byte_bounded(tmp_path):
    cell = _cell(tmp_path, "zoombound.000", _area(0, 0, 2, 2))
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    tiny = encview.MeshCache(16)
    assembled = _fixed_frame_build(plan, cache, tiny, 12.0)
    assert assembled is not None
    # The bound is enforced independently of the key shape.
    assert tiny.refused == 1
    assert tiny.resident_bytes == 0
