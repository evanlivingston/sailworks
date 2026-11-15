"""Checks for concurrent per-cell mesh building in ``build_view_scene``.

The parallel path has to be indistinguishable from the sequential one: same
bytes, same composite order, same progress prefixes, and one bad cell must not
cost the whole view.  These checks use synthetic cells, so they exercise the
scheduler and the cache rather than ENC data.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon

from sailworks.rendering import encplan, encview
from sailworks.rendering.vectordrawer import encmesh, encscene, encsounding

CACHE_BYTES = 64 * 1024 * 1024

#: Synthetic cells, keyed by path, so each build can use a fresh scene cache
#: (and still miss it) without opening a real ENC file.
_SCENES = {}


def _load(path, version, cancelled=None):
    """Stand-in for ``encscene.open_cell_scene`` over the synthetic cells."""
    scene = _SCENES[os.path.abspath(os.fspath(path))]
    assert scene.version == tuple(version)
    return scene


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


def _scene(path, version, records, nbytes=512):
    envelopes = tuple(record.envelope for record in records)
    return encscene.CellScene(
        path=str(path),
        version=version,
        records=tuple(records),
        layers=(
            encscene.LayerCandidates(
                layer_index=0,
                source_class=records[0].source_class,
                start=0,
                envelopes=envelopes,
                grid=(0.0, 0.0, 1.0, 1.0, 1),
                cells=(tuple(range(len(records))),),
            ),
        ),
        nbytes=nbytes,
    )


def _cell(tmp_path, name, min_x, min_y, attributes=()):
    """One quotable area plus a coastline inside its own 2x2 degree box."""
    max_x, max_y = min_x + 2.0, min_y + 2.0
    records = [
        _record(
            0,
            "DEPARE",
            Polygon(
                [
                    (min_x, min_y),
                    (max_x, min_y),
                    (max_x, max_y),
                    (min_x, max_y),
                ]
            ),
            (("DRVAL1", 0.0), ("DRVAL2", 20.0)) + tuple(attributes),
        ),
        _record(
            1,
            "COALNE",
            LineString([(min_x + 0.2, min_y + 0.2), (max_x - 0.2, max_y - 0.2)]),
            tuple(attributes),
        ),
    ]
    path = tmp_path / name
    path.write_bytes(name.encode())
    version = encscene.cell_version(path)
    scene = _scene(path, version, records)
    _SCENES[os.path.abspath(str(path))] = scene
    return scene


def _plan(scene):
    coverage = shapely.box(*_envelope(scene))
    return encplan.CellPlan(
        rank_key=(0, scene.path),
        path=str(scene.path),
        version=scene.version,
        clip=coverage,
        coverage_known=True,
        coverage=coverage,
    )


def _envelope(scene):
    xs = [record.envelope[0] for record in scene.records]
    ys = [record.envelope[2] for record in scene.records]
    return (
        min(xs),
        min(ys),
        max(record.envelope[1] for record in scene.records),
        max(record.envelope[3] for record in scene.records),
    )


def _projection(points):
    return np.asarray(points, dtype=np.float64) * 3.0


def _build(plan, *, workers, mesh_cache=None, on_cell=None, cancelled=None):
    """A build with a fresh scene cache and the synthetic-cell loader."""
    return encview.build_view_scene(
        plan,
        encscene.SceneCache(CACHE_BYTES),
        projection=_projection,
        stroke_pixels=1.5,
        palette="cm93",
        cancelled=cancelled,
        bounding=None,
        loader=_load,
        mesh_cache=mesh_cache,
        on_cell=on_cell,
        workers=workers,
    )


def _plan_of(tmp_path, count=4):
    cells = [
        _cell(tmp_path, f"cell{index}.000", 3.0 * index, 0.0) for index in range(count)
    ]
    return tuple(_plan(cell) for cell in cells)


def _bytes(scene):
    return (
        scene.positions.tobytes(),
        scene.colours.tobytes(),
        scene.soundings.tobytes(),
        tuple(scene.parts),
        tuple(scene.cells),
        tuple(scene.unsupported),
        scene.vertex_count,
        scene.triangle_count,
    )


# ----------------------------------------------------------- identical output
def test_parallel_output_is_byte_identical_to_sequential(tmp_path):
    plan = _plan_of(tmp_path)

    sequential = _build(plan, workers=1)
    parallel = _build(plan, workers=4)

    assert sequential is not None and parallel is not None
    assert _bytes(parallel) == _bytes(sequential)
    assert [cell.path for cell in parallel.cells] == [
        cell.path for cell in sequential.cells
    ]
    assert parallel.vertex_count > 0 and parallel.triangle_count > 0


def test_composite_order_is_stable_across_repeated_runs(tmp_path):
    plan = _plan_of(tmp_path, count=4)

    runs = [_build(plan, workers=4) for _ in range(5)]
    assert all(scene is not None for scene in runs)
    first = _bytes(runs[0])
    for scene in runs[1:]:
        assert _bytes(scene) == first
    # The plan is best-first, the composite is the reverse of it.
    assert [cell.path.rsplit("/", 1)[-1] for cell in runs[0].cells] == [
        cell.path.rsplit("/", 1)[-1] for cell in reversed(plan)
    ]


def test_warm_build_reuses_meshes_under_parallelism(tmp_path):
    plan = _plan_of(tmp_path)
    mesh_cache = encview.MeshCache(64 * 1024 * 1024)
    cache = encscene.SceneCache(CACHE_BYTES)

    cold = _build(plan, workers=4, mesh_cache=mesh_cache)
    builds = mesh_cache.triangulations
    warm = _build(plan, workers=4, mesh_cache=mesh_cache)

    assert cold is not None and warm is not None
    assert builds == len(plan)
    assert mesh_cache.triangulations == builds
    assert warm.mesh_hits == len(plan)
    assert warm.mesh_misses == 0
    assert warm.positions.tobytes() == cold.positions.tobytes()


# ------------------------------------------------------------------- progress
def test_progress_publishes_plan_prefixes_despite_completion_order(
    tmp_path, monkeypatch
):
    plan = _plan_of(tmp_path, count=4)
    real = encmesh.build_scene_mesh

    def slow_first_cell(scene, *args, **kwargs):
        # The best-ranked cell is the slow one: its completion is last, and a
        # naive "publish on completion" would leak that order into the scene.
        if any(("SLOW", 1.0) in record.attributes for record in scene.records):
            time.sleep(0.2)
        return real(scene, *args, **kwargs)

    # Mark the first cell's records as slow without touching their geometry.
    slow_scene = _cell(tmp_path, "slow.000", -6.0, 0.0, attributes=(("SLOW", 1.0),))
    plan = (_plan(slow_scene),) + plan[1:]
    monkeypatch.setattr(encview.encmesh, "build_scene_mesh", slow_first_cell)

    snapshots = []
    progress = []

    def on_cell(snapshot, done, total):
        snapshots.append(snapshot)
        progress.append((done, total))
        return True

    result = _build(plan, workers=4, on_cell=on_cell)
    assert result is not None
    assert progress == [(1, 4), (2, 4), (3, 4), (4, 4)]

    # Each snapshot is exactly the sequential scene for that prefix.
    for (done, _total), snapshot in zip(progress, snapshots):
        reference = _build(plan[:done], workers=1)
        assert reference is not None
        assert snapshot.positions.tobytes() == reference.positions.tobytes()
        assert tuple(snapshot.cells) == tuple(reference.cells)

    assert [cell.path for cell in snapshots[-1].cells] == [
        cell.path for cell in result.cells
    ]


def test_refusing_progress_stops_the_build(tmp_path):
    plan = _plan_of(tmp_path, count=4)
    seen = []

    def on_cell(snapshot, done, total):
        seen.append(done)
        return False

    assert _build(plan, workers=4, on_cell=on_cell) is None
    # The first prefix is refused, and nothing later is published.
    assert seen == [1]


# -------------------------------------------------------------- cancellation
def test_cancelled_build_returns_none_and_stops_publishing(tmp_path):
    plan = _plan_of(tmp_path, count=4)
    cancelled = threading.Event()
    snapshots = []

    def on_cell(snapshot, done, total):
        snapshots.append(done)
        cancelled.set()
        return True

    result = _build(plan, workers=4, on_cell=on_cell, cancelled=cancelled.is_set)
    assert result is None
    assert snapshots == [1]


def test_pre_cancelled_build_touches_nothing(tmp_path):
    plan = _plan_of(tmp_path, count=2)
    calls = []
    result = _build(
        plan,
        workers=4,
        on_cell=lambda *args: calls.append(args) or True,
        cancelled=lambda: True,
    )
    assert result is None
    assert calls == []


# ------------------------------------------------------------------- failures
def test_a_failing_cell_is_reported_and_the_rest_assembles(tmp_path, monkeypatch):
    plan = _plan_of(tmp_path, count=3)
    broken = _cell(tmp_path, "broken.000", 30.0, 0.0, attributes=(("BOOM", 1.0),))
    plan = plan + (_plan(broken),)

    real = encmesh.build_scene_mesh

    def flaky(scene, *args, **kwargs):
        if any(("BOOM", 1.0) in record.attributes for record in scene.records):
            raise RuntimeError("boom")
        return real(scene, *args, **kwargs)

    monkeypatch.setattr(encview.encmesh, "build_scene_mesh", flaky)

    scene = _build(plan, workers=4)
    assert scene is not None
    assert [cell.path for cell in scene.skipped] == [str(broken.path)]
    assert scene.skipped[0].reason.startswith("error: RuntimeError: boom")
    # The surviving cells are exactly the scene built without the bad one.
    reference = _build(plan[:-1], workers=1)
    assert reference is not None
    assert scene.positions.tobytes() == reference.positions.tobytes()
    assert tuple(scene.cells) == tuple(reference.cells)
    assert scene.vertex_count > 0


def test_invalid_worker_count_is_rejected(tmp_path):
    plan = _plan_of(tmp_path, count=1)
    try:
        _build(plan, workers=0)
    except ValueError as error:
        assert "workers" in str(error)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("workers=0 was accepted")


# --------------------------------------------------------------- cache bytes
def _empty_entry(nbytes):
    return encview.CellMesh(
        positions=np.empty((0, 2), dtype=np.float64),
        parts=(),
        unsupported=(),
        coverage_state="none",
        soundings=encsounding.EMPTY_SOUNDINGS,
        colours=np.empty((0, 4), dtype=np.uint8),
        nbytes=nbytes,
    )


def test_concurrent_puts_keep_byte_accounting_exact():
    entry_bytes = 1000
    keys = [(f"cell{index}", (1, 2, 3, 4)) for index in range(40)]
    cache = encview.MeshCache(40 * entry_bytes)

    def put_chunk(chunk):
        for key in chunk:
            assert cache.put(key, _empty_entry(entry_bytes))

    chunks = [keys[index::8] for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(put_chunk, chunks))

    assert cache.resident_bytes == 40 * entry_bytes
    assert cache.evictions == 0
    assert all(cache.get(key) is not None for key in keys)


def test_concurrent_puts_never_overshoot_the_budget():
    entry_bytes = 1000
    keys = [(f"cell{index}", (1, 2, 3, 4)) for index in range(64)]
    cache = encview.MeshCache(5 * entry_bytes)

    def put_chunk(chunk):
        for key in chunk:
            cache.put(key, _empty_entry(entry_bytes))
            assert 0 <= cache.resident_bytes <= cache.max_bytes

    chunks = [keys[index::8] for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(put_chunk, chunks))

    resident = [cache.get(key) for key in keys]
    surviving = sum(entry.nbytes for entry in resident if entry is not None)
    assert 0 <= cache.resident_bytes <= cache.max_bytes
    assert cache.resident_bytes == surviving
