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


def _build(
    plan,
    cache,
    mesh_cache,
    *,
    bounding=None,
    cancelled=None,
    display_scale=None,
):
    return encview.build_view_scene(
        plan,
        cache,
        projection=_projection,
        stroke_pixels=1.5,
        palette="cm93",
        cancelled=cancelled,
        bounding=bounding,
        mesh_cache=mesh_cache,
        display_scale=display_scale,
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


def test_repeated_identical_frame_tessellates_nothing_after_the_first(tmp_path):
    cell = _cell(tmp_path, "same.000", _area(0, 0, 2, 2))
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    first = _build(plan, cache, mesh_cache, bounding=_box(0, 0, 2, 2))
    assert first is not None
    # The counters are the instrumentation: one build, then pure reuse.
    assert first.mesh_built == 1 and first.mesh_hits == 0 and first.mesh_misses == 1
    built = mesh_cache.triangulations

    second = _build(plan, cache, mesh_cache, bounding=_box(0, 0, 2, 2))
    assert second is not None
    assert mesh_cache.triangulations == built
    assert second.mesh_built == 0
    assert second.mesh_hits == 1 and second.mesh_misses == 0
    assert second.mesh_bytes > 0


def test_pan_inside_the_granule_reuses_the_mesh(tmp_path):
    # A cell far larger than the view: the whole-cell rule cannot apply, but
    # the cell owns the inflated view, so the granule rule does.
    cell = _cell(tmp_path, "granule.000", _area(0, 0, 10, 10))
    coverage = shapely.box(0, 0, 10, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    planned = shapely.box(2.1, 2.1, 3.1, 3.1)
    plan = (_plan(cell.path, cell.version, planned, True, coverage),)

    first = _build(plan, cache, mesh_cache, bounding=_box(2.1, 2.1, 3.1, 3.1))
    assert first is not None
    assert first.mesh_built == 1 and mesh_cache.triangulations == 1

    # A 2% pan keeps the quantised granule, so nothing is re-tessellated.
    pan = shapely.box(2.12, 2.1, 3.12, 3.1)
    pan_plan = (_plan(cell.path, cell.version, pan, True, coverage),)
    second = _build(pan_plan, cache, mesh_cache, bounding=_box(2.12, 2.1, 3.12, 3.1))
    assert second is not None
    assert mesh_cache.triangulations == 1
    assert second.mesh_hits == 1 and second.mesh_built == 0
    np.testing.assert_array_equal(first.positions, second.positions)


def test_granule_is_not_used_when_the_plan_lacks_the_trimming_cell(tmp_path):
    lower = _cell(tmp_path, "lower.000", _area(0, 0, 10, 10))
    coverage = shapely.box(0, 0, 10, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(lower)
    mesh_cache = encview.MeshCache(1_000_000)
    # The planned clip is trimmed, but the plan does not carry the better cell
    # that trimmed it, so the ground this cell keeps is unknown: meshing the
    # granule could add hidden geometry.  Refusing the cache is the safe side.
    trimmed = shapely.box(2, 2, 3.0, 3.5)
    plan = (_plan(lower.path, lower.version, trimmed, True, coverage),)

    scene = _build(plan, cache, mesh_cache, bounding=_box(2.1, 2.1, 3.1, 3.1))
    assert scene is not None
    assert scene.mesh_built == 1
    # Not cached: a repeated frame meshes it again.
    again = _build(plan, cache, mesh_cache, bounding=_box(2.1, 2.1, 3.1, 3.1))
    assert again is not None
    assert again.mesh_hits == 0 and again.mesh_built == 1


def _trimmed_plan(lower, coverage, better, better_coverage, view):
    """A two-cell plan: the better cell claims a slab of the lower one."""
    return (
        _plan(
            better.path,
            better.version,
            shapely.intersection(better_coverage, view),
            True,
            better_coverage,
        ),
        _plan(
            lower.path,
            lower.version,
            shapely.intersection(shapely.difference(coverage, better_coverage), view),
            True,
            coverage,
        ),
    )


def test_trimmed_cell_reuses_its_mesh_across_a_pan(tmp_path):
    lower = _cell(tmp_path, "trimlower.000", _area(0, 0, 10, 10))
    better = _cell(tmp_path, "trimbetter.000", _area(0, 0, 2.5, 10))
    coverage = shapely.box(0, 0, 10, 10)
    better_coverage = shapely.box(0, 0, 2.5, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(lower) and cache.put(better)
    mesh_cache = encview.MeshCache(1_000_000)

    view = shapely.box(2.1, 2.1, 3.1, 3.1)
    first = _build(
        _trimmed_plan(lower, coverage, better, better_coverage, view),
        cache,
        mesh_cache,
        bounding=_box(2.1, 2.1, 3.1, 3.1),
    )
    assert first is not None
    # Two cells, one of them trimmed: both meshes built on the first frame.
    assert first.mesh_built == 2 and mesh_cache.triangulations == 2

    # A 2% pan stays inside the granule, and the better-cell set is unchanged,
    # so the trimmed cell's mesh is as reusable as the untrimmed one's.
    pan = shapely.box(2.12, 2.1, 3.12, 3.1)
    second = _build(
        _trimmed_plan(lower, coverage, better, better_coverage, pan),
        cache,
        mesh_cache,
        bounding=_box(2.12, 2.1, 3.12, 3.1),
    )
    assert second is not None
    assert second.mesh_built == 0 and second.mesh_hits == 2
    assert mesh_cache.triangulations == 2
    np.testing.assert_array_equal(first.positions, second.positions)


def test_changed_better_cell_set_invalidates_a_trimmed_mesh(tmp_path):
    lower = _cell(tmp_path, "trimrekey.000", _area(0, 0, 10, 10))
    wide = _cell(tmp_path, "trimwide.000", _area(0, 0, 2.5, 10))
    narrow = _cell(tmp_path, "trimnarrow.000", _area(0, 0, 2.0, 10))
    coverage = shapely.box(0, 0, 10, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(lower) and cache.put(wide) and cache.put(narrow)
    mesh_cache = encview.MeshCache(1_000_000)
    view = shapely.box(2.1, 2.1, 3.1, 3.1)

    first = _fixed_frame_build(
        _trimmed_plan(lower, coverage, wide, shapely.box(0, 0, 2.5, 10), view),
        cache,
        mesh_cache,
        14.236,
        bounding=_box(2.1, 2.1, 3.1, 3.1),
    )
    assert first is not None and mesh_cache.triangulations == 2

    # Same camera, same lower cell, same granule - only the neighbour's claim
    # shrank, so the ground the lower cell keeps is different geometry.  The
    # key must change or a stale strip would be drawn where the wider cell used
    # to cover.  In the fixed reference frame nothing else in the key can
    # account for it: scale and tolerance are None by design.
    second = _fixed_frame_build(
        _trimmed_plan(lower, coverage, narrow, shapely.box(0, 0, 2.0, 10), view),
        cache,
        mesh_cache,
        14.236,
        bounding=_box(2.1, 2.1, 3.1, 3.1),
    )
    assert second is not None
    assert second.mesh_hits == 0 and mesh_cache.triangulations == 4
    lower_keys = [key for key in mesh_cache._entries if key[0] == lower.path]
    assert len(lower_keys) == 2
    # Same cell, same granule, no scale or tolerance: only the trim identity
    # can keep those two meshes apart.
    assert lower_keys[0][2] is None and lower_keys[0][3] is None
    assert lower_keys[0][:2] == lower_keys[1][:2]
    assert lower_keys[0][6] == lower_keys[1][6]
    assert lower_keys[0][7] != lower_keys[1][7]


# ------------------------------------------------------------ pinning
class _Sized:
    """A stand-in mesh: the cache only reads ``nbytes`` from an entry."""

    def __init__(self, nbytes):
        self.nbytes = nbytes


def test_a_pinned_mesh_survives_an_eviction():
    cache = encview.MeshCache(200)
    first, second, third = (f"pin{index}.000" for index in range(3))
    assert cache.put(first, _Sized(100))
    assert cache.put(second, _Sized(100))

    # The third mesh does not fit: the unpinned victim goes, not the pinned
    # entry another planned cell is about to reuse.
    assert cache.put(third, _Sized(100), pin={second})
    assert cache.evictions == 1
    assert cache.pinned_evictions_avoided == 1
    assert cache.get(first) is None
    assert cache.get(second) is not None
    assert cache.resident_bytes == 200


def test_the_byte_cap_evicts_the_oldest_pinned_mesh():
    cache = encview.MeshCache(200)
    first, second, third = (f"all{index}.000" for index in range(3))
    assert cache.put(first, _Sized(100))
    assert cache.put(second, _Sized(100))

    # Every resident mesh is pinned: the cap still wins, so the oldest pinned
    # entry is dropped rather than the cache growing past its budget.
    assert cache.put(third, _Sized(100), pin={first, second})
    assert cache.evictions == 1
    assert cache.pinned_evictions_avoided == 0
    assert cache.get(first) is None
    assert cache.get(second) is not None
    assert cache.resident_bytes == 200
    assert len(cache._entries) == 2


# --------------------------------------------------- absolute granule grid
def _granule_of(mesh_cache, path):
    """The newest cached granule for ``path`` (a miss leaves the old entry)."""
    return [key[6] for key in mesh_cache._entries if key[0] == path][-1]


def _fixed(plan, cache, mesh_cache, view):
    """One assembly in the fixed reference frame over ``view``."""
    return _fixed_frame_build(
        plan,
        cache,
        mesh_cache,
        12.0,
        bounding=_box(*view.bounds),
    )


def test_the_granule_is_a_global_grid_not_the_view(tmp_path):
    # A cell far larger than the view: only the granule rule applies.  In the
    # fixed frame the grid is the global one, so two slightly different
    # rectangles at the same zoom share their keys.
    cell = _cell(tmp_path, "absolute.000", _area(0, 0, 10, 10))
    coverage = shapely.box(0, 0, 10, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)

    view = shapely.box(4.0, 2.0, 4.1, 2.1)
    first = _fixed(
        (_plan(cell.path, cell.version, view, True, coverage),),
        cache,
        mesh_cache,
        view,
    )
    assert first is not None and mesh_cache.triangulations == 1

    # Every bound sits on the 1/GRANULE_BASE_DEGREES lattice, which is what
    # makes the grid global: a view-relative step would put bounds anywhere.
    for bound in _granule_of(mesh_cache, cell.path):
        scaled = bound * (1.0 / encview.GRANULE_BASE_DEGREES)
        assert abs(scaled - round(scaled)) < 1e-6

    # A rectangle a hair wider is still the same granule: the step comes from
    # the zoom level, not from the exact rectangle.
    wider = shapely.box(4.0, 2.0, 4.101, 2.1)
    second = _fixed(
        (_plan(cell.path, cell.version, wider, True, coverage),),
        cache,
        mesh_cache,
        wider,
    )
    assert second is not None
    assert second.mesh_hits == 1 and second.mesh_built == 0
    assert mesh_cache.triangulations == 1
    np.testing.assert_array_equal(first.positions, second.positions)


def test_panning_inside_one_granule_hits_the_cache(tmp_path):
    cell = _cell(tmp_path, "granulehit.000", _area(0, 0, 10, 10))
    coverage = shapely.box(0, 0, 10, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)

    view = shapely.box(4.0, 2.0, 4.1, 2.1)
    assert (
        _fixed(
            (_plan(cell.path, cell.version, view, True, coverage),),
            cache,
            mesh_cache,
            view,
        )
        is not None
    )
    granule = _granule_of(mesh_cache, cell.path)

    # A 1% pan stays on the same grid lines.
    pan = shapely.box(4.003, 2.0, 4.103, 2.1)
    second = _fixed(
        (_plan(cell.path, cell.version, pan, True, coverage),),
        cache,
        mesh_cache,
        pan,
    )
    assert second is not None
    assert second.mesh_hits == 1 and second.mesh_built == 0
    assert mesh_cache.triangulations == 1
    assert _granule_of(mesh_cache, cell.path) == granule
    # The reused mesh covers the new clip, not only the clip it was built for.
    assert granule[0] <= pan.bounds[0] and granule[2] >= pan.bounds[2]
    le = np.asarray(second.positions, dtype=np.float64)[:, :2]
    assert le[:, 0].max() >= pan.bounds[2]


def test_crossing_a_granule_line_misses(tmp_path):
    cell = _cell(tmp_path, "crossing.000", _area(0, 0, 10, 10))
    coverage = shapely.box(0, 0, 10, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)

    view = shapely.box(4.0, 2.0, 4.1, 2.1)
    assert (
        _fixed(
            (_plan(cell.path, cell.version, view, True, coverage),),
            cache,
            mesh_cache,
            view,
        )
        is not None
    )
    first_granule = _granule_of(mesh_cache, cell.path)
    assert mesh_cache.triangulations == 1

    # A whole granule to the right: the cached clip does not reach it.
    across = shapely.box(4.2, 2.0, 4.3, 2.1)
    second = _fixed(
        (_plan(cell.path, cell.version, across, True, coverage),),
        cache,
        mesh_cache,
        across,
    )
    assert second is not None
    assert mesh_cache.triangulations == 2
    assert second.mesh_misses == 1 and second.mesh_built == 1
    assert _granule_of(mesh_cache, cell.path) != first_granule


def test_a_granule_hit_keeps_the_better_cell_ground_out(tmp_path):
    lower = _cell(tmp_path, "excludelower.000", _area(0, 0, 10, 10))
    better = _cell(tmp_path, "excludebetter.000", _area(0, 0, 4.98, 10))
    coverage = shapely.box(0, 0, 10, 10)
    better_coverage = shapely.box(0, 0, 4.98, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(lower) and cache.put(better)
    mesh_cache = encview.MeshCache(1_000_000)

    view = shapely.box(4.95, 2.0, 5.05, 2.1)
    first = _fixed(
        _trimmed_plan(lower, coverage, better, better_coverage, view),
        cache,
        mesh_cache,
        view,
    )
    assert first is not None and first.mesh_built == 2

    pan = shapely.box(4.953, 2.0, 5.053, 2.1)
    second = _fixed(
        _trimmed_plan(lower, coverage, better, better_coverage, pan),
        cache,
        mesh_cache,
        pan,
    )
    assert second is not None
    assert second.mesh_hits == 2 and second.mesh_built == 0
    # A granule larger than the view must still exclude the better cell's
    # ground (it reaches x=5): a mesh that ignored the trim would carry
    # vertices from x=0.
    le = np.asarray(second.positions, dtype=np.float64)[:, :2]
    assert le[:, 0].min() > 4.5


def test_display_bucket_ladder_is_absolute_and_never_coarser():
    """The ladder is fixed in scale space, and its value is a floor.

    ``bucket <= scale`` is what keeps the SCAMIN gate conservative: the gate
    keeps a feature when the value it compares is at or below the feature's
    SCAMIN, so a smaller value can only admit features.
    """
    for scale in (1.0, 1_000.0, 1_024.0, 22_000.0, 90_000.0):
        bucket = encview.bucket_display_scale(scale)
        assert bucket <= scale
        # A ladder value is its own bucket, and 10% above it is still inside.
        assert encview.bucket_display_scale(bucket) == bucket
        assert encview.bucket_display_scale(bucket * 1.1) == bucket
    assert encview.bucket_display_scale(None) is None
    assert encview.bucket_display_scale(0.0) is None


def test_a_small_zoom_step_reuses_the_mesh(tmp_path):
    cell = _cell(tmp_path, "bucket.000", _area(0, 0, 2, 2))
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    # ~11% apart, so both land on the same quarter-octave step.
    assert encview.bucket_display_scale(900.0) == encview.bucket_display_scale(1_000.0)
    first = _build(
        plan, cache, mesh_cache, bounding=_box(0, 0, 2, 2), display_scale=900.0
    )
    assert first is not None and mesh_cache.triangulations == 1

    second = _build(
        plan, cache, mesh_cache, bounding=_box(0, 0, 2, 2), display_scale=1_000.0
    )
    assert second is not None
    assert second.mesh_hits == 1 and second.mesh_built == 0
    assert mesh_cache.triangulations == 1


def test_a_scale_in_another_bucket_misses(tmp_path):
    cell = _cell(tmp_path, "bucket.000", _area(0, 0, 2, 2))
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    assert encview.bucket_display_scale(1_000.0) != encview.bucket_display_scale(
        2_000.0
    )
    first = _build(
        plan, cache, mesh_cache, bounding=_box(0, 0, 2, 2), display_scale=1_000.0
    )
    assert first is not None and mesh_cache.triangulations == 1

    second = _build(
        plan, cache, mesh_cache, bounding=_box(0, 0, 2, 2), display_scale=2_000.0
    )
    assert second is not None
    assert second.mesh_misses == 1 and second.mesh_built == 1
    assert mesh_cache.triangulations == 2


def _granule_for(region):
    """The absolute granule ``region`` is snapped to, and the grid step."""
    step = encview._absolute_granule_step(region)
    assert step is not None
    granule = encview._granule_region(region, step=step)
    assert granule is not None
    return step, granule


def _prefetch(plan, cache, mesh_cache, granule, **kwargs):
    return encview.prefetch_granule(
        plan,
        cache,
        granule=granule,
        projection=_projection,
        stroke_pixels=1.5,
        palette="cm93",
        mesh_cache=mesh_cache,
        **kwargs,
    )


def test_prefetch_warms_a_neighbour_granule_for_a_later_hit(tmp_path):
    cell = _cell(tmp_path, "prefetch.000", _area(0, 0, 10, 10))
    coverage = shapely.box(0, 0, 10, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)

    # A power-of-two grid step, so the neighbour's bounds are exactly the
    # grid lines a camera arriving there will snap to.
    view = shapely.box(4.0, 2.0, 4.5, 2.5)
    plan = (_plan(cell.path, cell.version, view, True, coverage),)
    assert _build(plan, cache, mesh_cache) is not None
    assert mesh_cache.triangulations == 1

    step, granule = _granule_for(view)
    right = encview.neighbour_granules(granule, step=step, limit=1)[0]
    report = _prefetch(plan, cache, mesh_cache, right)
    assert report == {
        "built": 1,
        "resident": 0,
        "skipped": 0,
        "cancelled": False,
        "reason": None,
    }
    assert mesh_cache.triangulations == 2

    # The camera reaches that granule: the mesh a real request needs is there,
    # because the prefetch keyed it exactly as the request would have.
    moved = shapely.box(*right.bounds)
    moved_plan = (_plan(cell.path, cell.version, moved, True, coverage),)
    built = _build(moved_plan, cache, mesh_cache)
    assert built is not None
    assert built.mesh_hits == 1 and built.mesh_built == 0
    assert mesh_cache.triangulations == 2


def test_prefetch_skips_resident_granules_and_bounds_its_neighbours(tmp_path):
    cell = _cell(tmp_path, "prefetchbound.000", _area(0, 0, 10, 10))
    coverage = shapely.box(0, 0, 10, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)

    view = shapely.box(4.0, 2.0, 4.5, 2.5)
    plan = (_plan(cell.path, cell.version, view, True, coverage),)
    step, granule = _granule_for(view)
    neighbours = encview.neighbour_granules(granule, step=step)
    assert len(neighbours) == encview.PREFETCH_GRANULES

    first = _prefetch(plan, cache, mesh_cache, neighbours[0])
    assert first["built"] == 1 and first["resident"] == 0
    again = _prefetch(plan, cache, mesh_cache, neighbours[0])
    assert again["built"] == 0 and again["resident"] == 1
    assert mesh_cache.triangulations == 1


def test_a_prefetched_granule_is_never_an_uncacheable_cell(tmp_path):
    cell = _cell(tmp_path, "prefetchskip.000", _area(0, 0, 10, 10))
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)

    # Unknown coverage cannot be keyed the way a real request would: the whole
    # granule is refused instead of warming something a request will not find.
    view = shapely.box(4.0, 2.0, 4.5, 2.5)
    plan = (_plan(cell.path, cell.version, view, False),)
    step, granule = _granule_for(view)
    report = _prefetch(plan, cache, mesh_cache, granule)
    assert report["built"] == 0 and report["reason"] == "uncacheable"
    assert mesh_cache.triangulations == 0


def test_neighbour_granules_follow_the_direction_of_travel():
    granule = shapely.box(0.0, 0.0, 1.0, 1.0)
    east = encview.neighbour_granules(granule, step=1.0, travel=(1.0, 0.0))
    assert shapely.equals(east[0], shapely.box(1.0, 0.0, 2.0, 1.0))
    south = encview.neighbour_granules(granule, step=1.0, travel=(0.0, -1.0))
    assert shapely.equals(south[0], shapely.box(0.0, -1.0, 1.0, 0.0))
    assert encview.neighbour_granules(granule, step=1.0, limit=0) == []


#: One scale and another that buckets differently, for the prefetch rule.
_SAME_SCALE = 1_000.0
_OTHER_BUCKET = 2_000.0


def _candidate(granule, travel, display_scale=_SAME_SCALE, current_scale=_SAME_SCALE):
    return encview.prefetch_candidate(
        granule,
        step=1.0,
        travel=travel,
        display_scale=display_scale,
        current_scale=current_scale,
    )


def test_prefetch_picks_only_the_neighbour_in_the_travel_direction():
    granule = shapely.box(0.0, 0.0, 1.0, 1.0)
    assert len(encview.neighbour_granules(granule, step=1.0)) == 1
    assert encview.PREFETCH_GRANULES == 1

    ahead, reason = _candidate(granule, (0.0, -1.0))
    assert reason is None
    assert shapely.equals(ahead, shapely.box(0.0, -1.0, 1.0, 0.0))
    assert shapely.equals(
        ahead,
        encview.neighbour_granules(granule, step=1.0, limit=1, travel=(0.0, -1.0))[0],
    )

    ahead, reason = _candidate(granule, (0.3, 0.0))
    assert reason is None
    # A diagonally-weighted pan still names exactly one granule, never two.
    assert shapely.equals(ahead, shapely.box(1.0, 0.0, 2.0, 1.0))


def test_prefetch_without_travel_prefetches_nothing():
    granule = shapely.box(0.0, 0.0, 1.0, 1.0)
    for travel in (None, (0.0, 0.0), (float("nan"), 1.0), ("x", "y")):
        assert _candidate(granule, travel) == (None, "no_travel")


def test_prefetch_skips_a_granule_at_another_scale_bucket():
    granule = shapely.box(0.0, 0.0, 1.0, 1.0)
    assert encview.bucket_display_scale(_SAME_SCALE) != encview.bucket_display_scale(
        _OTHER_BUCKET
    )
    assert _candidate(
        granule, (1.0, 0.0), display_scale=_SAME_SCALE, current_scale=_OTHER_BUCKET
    ) == (None, "wrong_bucket")
    ahead, reason = _candidate(granule, (1.0, 0.0))
    assert reason is None and ahead is not None


def test_a_resident_prefetch_granule_is_skipped_without_building(tmp_path):
    cell = _cell(tmp_path, "prefetchresident.000", _area(0, 0, 10, 10))
    coverage = shapely.box(0, 0, 10, 10)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)

    view = shapely.box(4.0, 2.0, 4.5, 2.5)
    plan = (_plan(cell.path, cell.version, view, True, coverage),)
    step, granule = _granule_for(view)
    ahead, reason = encview.prefetch_candidate(
        granule,
        step=step,
        travel=(1.0, 0.0),
        display_scale=_SAME_SCALE,
        current_scale=_SAME_SCALE,
    )
    assert reason is None and ahead is not None

    first = _prefetch(plan, cache, mesh_cache, ahead, display_scale=_SAME_SCALE)
    assert first["built"] == 1 and first["reason"] is None
    again = _prefetch(plan, cache, mesh_cache, ahead, display_scale=_SAME_SCALE)
    assert again["built"] == 0
    assert again["resident"] == 1
    assert again["reason"] == "resident"
    assert mesh_cache.triangulations == 1
