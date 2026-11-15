"""Focused checks for backend-neutral ordered multi-cell scene assembly."""

from __future__ import annotations

import numpy as np
import shapely
from osgeo import ogr
from shapely.geometry import LineString, Point, Polygon

from sailworks.rendering import encplan, encview
from sailworks.rendering.encview import build_view_scene
from sailworks.rendering.vectordrawer import encmesh, encpalette, encscene


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


def _projection(points):
    return np.asarray(points, dtype=np.float64) * 2.0


def _box(min_x, min_y, max_x, max_y):
    return ogr.CreateGeometryFromWkt(
        f"POLYGON (({min_x} {min_y}, {max_x} {min_y}, {max_x} {max_y}, "
        f"{min_x} {max_y}, {min_x} {min_y}))"
    )


def _build(plan, cache, *, bounding=None, cancelled=None, loader=None, mesh_cache=None):
    kwargs = {}
    if loader is not None:
        kwargs["loader"] = loader
    return build_view_scene(
        plan,
        cache,
        projection=_projection,
        stroke_pixels=1.5,
        palette="cm93",
        cancelled=cancelled,
        bounding=bounding,
        mesh_cache=mesh_cache,
        **kwargs,
    )


def _shifted_parts(mesh, offset=0):
    return tuple(
        (
            part.kind,
            part.source_class,
            part.first_vertex + offset,
            part.vertex_count,
            part.colour,
        )
        for part in mesh.parts
    )


def test_assembles_cells_in_plan_order_and_matches_per_cell_meshes(tmp_path):
    first = _cell(
        tmp_path,
        "first.000",
        [
            _record(
                0,
                "DEPARE",
                Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
                (("DRVAL1", 0.0), ("DRVAL2", 10.0)),
            )
        ],
    )
    second = _cell(
        tmp_path,
        "second.000",
        [
            _record(
                1,
                "DEPARE",
                Polygon([(3, 0), (5, 0), (5, 2), (3, 2)]),
                (("DRVAL1", 0.0), ("DRVAL2", 30.0)),
            ),
            _record(2, "COALNE", LineString([(3, 1), (5, 1)])),
        ],
    )
    plan = (
        _plan(
            first.path,
            first.version,
            shapely.box(0, 0, 1.5, 2),
            True,
            coverage=shapely.box(0, 0, 2, 2),
        ),
        _plan(
            second.path,
            second.version,
            shapely.box(2, 0, 5, 2),
            True,
            coverage=shapely.box(3, 0, 5, 2),
        ),
    )
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(first) and cache.put(second)
    mesh_cache = encview.MeshCache(1_000_000)

    assembled = _build(plan, cache, bounding=_box(0, 0, 5, 2), mesh_cache=mesh_cache)

    assert assembled is not None
    # Composite order is the reverse of the plan: worst first, best last.
    assert [cell.path for cell in assembled.cells] == [second.path, first.path]
    assert assembled.skipped == ()
    assert assembled.is_complete

    # Each mesh is clipped to the cell's own coverage, not the view clip.
    expected_positions = []
    expected_parts = []
    offset = 0
    for scene, cell in zip((second, first), reversed(plan)):
        mesh = encmesh.build_scene_mesh(
            scene,
            coverage=cell.coverage,
            clip=True,
            stroke_pixels=1.5,
            projection=_projection,
            palette="cm93",
        )
        expected_positions.append(mesh.positions)
        expected_parts.extend(_shifted_parts(mesh, offset))
        offset += mesh.vertex_count

    expected = np.concatenate(expected_positions)
    np.testing.assert_array_equal(assembled.positions, expected)
    assert assembled.vertex_count == expected.shape[0]
    assert _shifted_parts(assembled) == tuple(expected_parts)
    assert assembled.parts[0].colour is not None
    assert [part.first_vertex for part in assembled.parts] == sorted(
        part.first_vertex for part in assembled.parts
    )
    assert assembled.accounted_bytes == first.nbytes + second.nbytes
    assert assembled.mesh_hits == 0 and assembled.mesh_misses == 2

    first_span = assembled.cells[0]
    assert first_span.first_vertex == 0
    assert (
        first_span.first_vertex + first_span.vertex_count
        == assembled.cells[1].first_vertex
    )


def test_unknown_coverage_cell_is_not_clipped(tmp_path):
    scene = _cell(
        tmp_path,
        "unknown.000",
        [
            _record(
                0,
                "DEPARE",
                Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
                (("DRVAL1", 0.0), ("DRVAL2", 10.0)),
            )
        ],
    )
    plan = (_plan(scene.path, scene.version, shapely.box(0, 0, 1, 1), False),)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(scene)

    assembled = _build(plan, cache, bounding=_box(0, 0, 4, 4))

    unclipped = encmesh.build_scene_mesh(
        scene,
        coverage=None,
        clip=None,
        stroke_pixels=1.5,
        projection=_projection,
        palette="cm93",
    )
    assert assembled is not None
    assert assembled.cells[0].coverage_state == "none"
    assert assembled.vertex_count == unclipped.vertex_count
    np.testing.assert_array_equal(assembled.positions, unclipped.positions)
    assert assembled.parts[0].vertex_count == unclipped.parts[0].vertex_count
    # The whole 2x2 cell is present, not the 1x1 plan clip.
    assert assembled.positions[:, 0].max() >= 2.0


def test_cancelled_assembly_returns_none(tmp_path):
    scene = _cell(
        tmp_path,
        "cancel.000",
        [_record(0, "DEPARE", Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]))],
    )
    plan = (_plan(scene.path, scene.version, shapely.box(0, 0, 1, 1), True),)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(scene)

    assert _build(plan, cache, cancelled=lambda: True) is None

    polls = {"count": 0}

    def cancelled():
        polls["count"] += 1
        return polls["count"] > 2

    assert _build(plan, cache, cancelled=cancelled) is None


def test_oversized_and_unsupported_are_reported_not_dropped(tmp_path):
    oversized = _cell(
        tmp_path,
        "oversized.000",
        [_record(0, "DEPARE", Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]))],
        nbytes=1000,
    )
    usable = _cell(
        tmp_path,
        "usable.000",
        [
            _record(1, "DEPARE", Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])),
            # Soundings need text, so increment 1 still reports them unsupported.
            _record(2, "SOUNDG", Point(0.5, 0.5)),
        ],
        nbytes=10,
    )
    plan = (
        _plan(oversized.path, oversized.version, shapely.box(0, 0, 1, 1), True),
        _plan(usable.path, usable.version, shapely.box(0, 0, 1, 1), True),
    )
    cache = encscene.SceneCache(100)
    assert cache.put(usable)

    def loader(path, version, cancelled):
        assert path == oversized.path
        return oversized

    assembled = _build(plan, cache, bounding=_box(0, 0, 1, 1), loader=loader)

    assert assembled is not None
    assert [cell.path for cell in assembled.cells] == [usable.path]
    assert len(assembled.skipped) == 1
    assert assembled.skipped[0].path == oversized.path
    assert assembled.skipped[0].reason == "oversized"
    assert assembled.accounted_bytes == usable.nbytes
    assert not assembled.is_complete

    assert len(assembled.unsupported) == 1
    reported = assembled.unsupported[0]
    assert reported.path == usable.path
    assert reported.part.source_class == "SOUNDG"
    assert "not rendered" in reported.reason

    expected = encmesh.build_scene_mesh(
        usable,
        coverage=plan[1].clip,
        clip=True,
        stroke_pixels=1.5,
        projection=_projection,
        palette="cm93",
    )
    np.testing.assert_array_equal(assembled.positions, expected.positions)


def test_repeated_assembly_is_stable(tmp_path):
    scene = _cell(
        tmp_path,
        "stable.000",
        [
            _record(
                0,
                "DEPARE",
                Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
                (("DRVAL1", 0.0), ("DRVAL2", 10.0)),
            ),
            _record(1, "COALNE", LineString([(0.5, 0.5), (1.5, 1.5)])),
        ],
    )
    plan = (_plan(scene.path, scene.version, shapely.box(0, 0, 1.5, 1.5), True),)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(scene)

    calls = {"loader": 0}

    def loader(path, version, cancelled):
        calls["loader"] += 1
        return scene

    first = _build(plan, cache, bounding=_box(0, 0, 2, 2), loader=loader)
    second = _build(plan, cache, bounding=_box(0, 0, 2, 2), loader=loader)

    assert first is not None and second is not None
    assert calls["loader"] == 0
    np.testing.assert_array_equal(first.positions, second.positions)
    assert first.parts == second.parts
    assert first.cells == second.cells
    assert first.accounted_bytes == second.accounted_bytes
    assert first.skipped == () and first.unsupported == ()


def test_cached_colours_are_reused_and_accounted(tmp_path, monkeypatch):
    cell = _cell(
        tmp_path,
        "colour.000",
        [
            _record(
                0,
                "DEPARE",
                Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
                (("DRVAL1", 0.0), ("DRVAL2", 10.0)),
            )
        ],
    )
    coverage = shapely.box(0, 0, 2, 2)
    cache = encscene.SceneCache(1_000_000)
    assert cache.put(cell)
    mesh_cache = encview.MeshCache(1_000_000)
    plan = (_plan(cell.path, cell.version, coverage, True, coverage),)

    first = _build(plan, cache, bounding=_box(0, 0, 2, 2), mesh_cache=mesh_cache)
    assert first is not None and mesh_cache.triangulations == 1
    second = _build(plan, cache, bounding=_box(0, 0, 2, 2), mesh_cache=mesh_cache)
    assert second is not None and second.mesh_hits == 1

    # The assembled view reuses the cached buffer instead of expanding again.
    assert second.vertex_colours() is second.colours
    np.testing.assert_array_equal(second.vertex_colours(), first.colours)

    entry = next(iter(mesh_cache._entries.values()))
    assert entry.colours.shape == (entry.positions.shape[0], 4)
    assert entry.nbytes == (
        entry.positions.nbytes
        + entry.soundings.nbytes
        + entry.colours.nbytes
        + (len(entry.parts) + len(entry.unsupported)) * encview.PART_OVERHEAD_BYTES
    )

    def boom(*args, **kwargs):
        raise AssertionError("warm view re-expanded per-vertex colours")

    monkeypatch.setattr(encpalette, "vertex_colours", boom)
    np.testing.assert_array_equal(second.vertex_colours(), first.colours)
