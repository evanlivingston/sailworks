"""Checks for concurrent per-cell mesh building in ``build_view_scene``.

The parallel path has to be indistinguishable from the sequential one: same
bytes, same composite order, same progress prefixes, and one bad cell must not
cost the whole view.  These checks use synthetic cells, so they exercise the
scheduler and the cache rather than ENC data.
"""

from __future__ import annotations

import os
import sys
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


def _build(
    plan,
    *,
    workers,
    mesh_cache=None,
    on_cell=None,
    cancelled=None,
    timings=None,
    palette="cm93",
    stroke_pixels=1.5,
):
    """A build with a fresh scene cache and the synthetic-cell loader."""
    return encview.build_view_scene(
        plan,
        encscene.SceneCache(CACHE_BYTES),
        projection=_projection,
        stroke_pixels=stroke_pixels,
        palette=palette,
        cancelled=cancelled,
        bounding=None,
        loader=_load,
        mesh_cache=mesh_cache,
        on_cell=on_cell,
        timings=timings,
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


# ------------------------------------------------- head-first publication
def test_first_partial_is_published_before_the_other_cells_finish(
    tmp_path, monkeypatch
):
    """The first prefix must not wait for cells that come after it.

    A build that published only when every mesh was in would still return a
    correct scene, but the chart would stay blank until the last cell.  Here
    every non-head cell blocks inside its mesh; the first publication has to
    release them, so a build that waits for them never publishes at all.
    """
    plan = _plan_of(tmp_path, count=3)
    head = plan[0].path
    release = threading.Event()
    real = encview._mesh_cell

    def gated(task, scene, *args, **kwargs):
        if task.cell.path != head:
            # 5 s is far longer than the synthetic mesh; a hang here means
            # the first partial waited for a cell that follows it.
            assert release.wait(5.0), "the first partial waited for later cells"
        return real(task, scene, *args, **kwargs)

    monkeypatch.setattr(encview, "_mesh_cell", gated)

    snapshots = []

    def on_cell(snapshot, done, total):
        snapshots.append((done, len(snapshot.cells)))
        if done == 1:
            release.set()
        return True

    scene = _build(plan, workers=4, on_cell=on_cell)
    assert scene is not None
    # A one-cell prefix is published, in plan order, while the rest are held.
    assert snapshots[0] == (1, 1)
    assert snapshots[-1] == (3, 3)
    assert len(scene.cells) == 3


def test_the_plan_head_is_meshed_alone_before_the_window_opens(tmp_path, monkeypatch):
    """Meshing the head against the window multiplies its latency.

    Publication is a plan prefix, so only the head is on the critical path to
    the first frame.  Measured on the six-cell Strait view, meshing it against
    three other cells took 1.27 s against 0.34 s alone, which pushed every
    partial into one burst at the end of the build.
    """
    plan = _plan_of(tmp_path, count=4)
    started = []
    lock = threading.Lock()
    real = encview._mesh_cell

    def record(task, scene, *args, **kwargs):
        with lock:
            started.append(task.cell.path)
        return real(task, scene, *args, **kwargs)

    monkeypatch.setattr(encview, "_mesh_cell", record)

    at_first = []

    def on_cell(snapshot, done, total):
        with lock:
            if done == 1:
                at_first.extend(started)
        return True

    scene = _build(plan, workers=4, on_cell=on_cell)
    assert scene is not None
    assert at_first == [plan[0].path]
    # The window fills once the head has published, and every cell is drawn.
    assert sorted(started) == sorted(entry.path for entry in plan)
    assert len(scene.cells) == len(plan)


def test_head_first_can_be_switched_off_for_an_ab(tmp_path, monkeypatch):
    """``SAILWORKS_ENC_HEAD_FIRST=0`` restores whole-window submission.

    Opens and submissions happen inline in plan order, so the cells opened by
    the first publication say how much of the window was submitted: only the
    head with the switch on, the whole window off.
    """
    plan = _plan_of(tmp_path, count=4)
    opened = []
    real_load = _load

    def record_load(path, version, cancelled=None):
        opened.append(os.path.abspath(os.fspath(path)))
        return real_load(path, version, cancelled)

    monkeypatch.setattr(sys.modules[__name__], "_load", record_load)

    at_first = []

    def on_cell(snapshot, done, total):
        if done == 1:
            at_first.extend(opened)
        return True

    monkeypatch.setenv("SAILWORKS_ENC_HEAD_FIRST", "0")
    scene = _build(plan, workers=4, on_cell=on_cell)
    assert scene is not None
    assert len(scene.cells) == len(plan)
    assert at_first == [entry.path for entry in plan]

    opened.clear()
    at_first.clear()
    monkeypatch.setenv("SAILWORKS_ENC_HEAD_FIRST", "1")
    scene = _build(plan, workers=4, on_cell=on_cell)
    assert scene is not None
    assert len(scene.cells) == len(plan)
    assert at_first == [plan[0].path]
    # Every cell is opened in plan order either way.
    assert opened == [entry.path for entry in plan]


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


# ------------------------------------------------------- per-cell cache trace
def _trace():
    """A traced build's timings mapping: ``cells`` is the trace flag."""
    return {"cells": {}}


def test_cache_trace_reports_actual_miss_then_hit_serial(tmp_path):
    plan = _plan_of(tmp_path, count=2)
    mesh_cache = encview.MeshCache(CACHE_BYTES)

    cold = _trace()
    assert _build(plan, workers=1, mesh_cache=mesh_cache, timings=cold) is not None
    rows = cold["cells_cache"]["cells"]
    assert [row["lookup"] for row in rows] == ["miss", "miss"]
    assert [row["index"] for row in rows] == [0, 1]
    assert [row["cell"] for row in rows] == [
        os.path.basename(os.fspath(cell.path)) for cell in plan
    ]
    assert cold["cells_cache"]["dropped"] == 0
    assert cold["cells_cache"]["cache"] == {
        "evictions": 0,
        "refused": 0,
        "pinned_evictions_avoided": 0,
    }

    warm = _trace()
    assert _build(plan, workers=1, mesh_cache=mesh_cache, timings=warm) is not None
    warm_rows = warm["cells_cache"]["cells"]
    assert [row["lookup"] for row in warm_rows] == ["hit", "hit"]
    # The hit asked the same key the miss stored: no component drifted.
    assert [row["key"] for row in warm_rows] == [row["key"] for row in rows]


def test_cache_trace_parallel_rows_follow_plan_order(tmp_path):
    plan = _plan_of(tmp_path, count=4)
    mesh_cache = encview.MeshCache(CACHE_BYTES)
    assert _build(plan, workers=4, mesh_cache=mesh_cache, timings=_trace()) is not None

    timings = _trace()
    scene = _build(plan, workers=4, mesh_cache=mesh_cache, timings=timings)
    assert scene is not None and scene.mesh_hits == len(plan)
    rows = timings["cells_cache"]["cells"]
    assert [row["index"] for row in rows] == list(range(len(plan)))
    assert [row["lookup"] for row in rows] == ["hit"] * len(plan)


def test_cache_trace_shows_the_one_key_component_that_changed(tmp_path):
    plan = _plan_of(tmp_path, count=1)
    mesh_cache = encview.MeshCache(CACHE_BYTES)
    first = _trace()
    second = _trace()

    assert _build(plan, workers=1, mesh_cache=mesh_cache, timings=first) is not None
    assert (
        _build(plan, workers=1, mesh_cache=mesh_cache, timings=second, palette=None)
        is not None
    )

    before = first["cells_cache"]["cells"][0]
    after = second["cells_cache"]["cells"][0]
    # A different palette is a different key, so the second lookup misses.
    assert before["lookup"] == "miss" and after["lookup"] == "miss"
    changed = {
        name for name in before["key"] if before["key"][name] != after["key"][name]
    }
    assert changed == {"palette"}
    assert before["key"]["palette"] == "cm93" and after["key"]["palette"] is None


def test_untraced_build_keeps_no_cache_rows(tmp_path, monkeypatch):
    plan = _plan_of(tmp_path, count=2)
    mesh_cache = encview.MeshCache(CACHE_BYTES)
    formatted = []
    monkeypatch.setattr(
        encview, "_cache_trace_row", lambda *args, **kwargs: formatted.append(args)
    )

    timings = {}
    assert _build(plan, workers=1, mesh_cache=mesh_cache, timings=timings) is not None
    assert "cells" not in timings
    assert "cells_cache" not in timings
    assert formatted == []
