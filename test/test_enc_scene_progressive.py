"""Qt-free checks for progressive publication of an ENC view scene.

``build_view_scene`` may hand each resolved subset to ``on_cell``.  A snapshot
has to be a correct scene in its own right: same composite order as the
finished scene, same vertex offsets, and the same bytes for the cells it
holds.  The final scene must be byte-identical to a build that never published
progress, and refusing progress or cancelling must never yield a final scene.
"""

from __future__ import annotations

import threading

import numpy as np
import shapely

from sailworks.rendering import encplan, encview
from sailworks.rendering.vectordrawer import encscene
from test.enc_scene_fixtures import cell  # noqa: F401 - a pytest fixture

CACHE_BYTES = 64 * 1024 * 1024

#: Every fixture cell falls inside this, and the plan's own view is not used.
BBOX = shapely.box(-180.0, -90.0, 180.0, 90.0)


def _projection(points):
    """A linear degrees -> logical pixels projection, like the viewport's."""
    points = np.asarray(points, dtype=np.float64)
    if points.shape[0] == 0:
        return np.empty((0, 2), dtype=np.float64)
    return np.column_stack(((points[:, 0] + 1.0) * 10.0, (12.0 - points[:, 1]) * 10.0))


def _plan(path, count=1):
    """``count`` view-dependent plan cells for the same fixture, in plan order."""
    version = encscene.cell_version(path)
    return tuple(
        encplan.CellPlan(
            rank_key=(index, 0),
            path=str(path),
            version=version,
            clip=None,
            coverage_known=False,
        )
        for index in range(count)
    )


def _build(plan, *, on_cell=None, cancelled=None):
    return encview.build_view_scene(
        plan,
        encscene.SceneCache(CACHE_BYTES),
        projection=_projection,
        stroke_pixels=1.0,
        palette=None,
        cancelled=cancelled,
        bounding=BBOX,
        # A fresh cache per build keeps every byte comparison reproducible.
        mesh_cache=None,
        on_cell=on_cell,
    )


def _collect(plan, **kwargs):
    snapshots = []
    progress = []

    def on_cell(snapshot, done, total):
        snapshots.append(snapshot)
        progress.append((done, total))
        return True

    result = _build(plan, on_cell=on_cell, **kwargs)
    return result, snapshots, progress


# ------------------------------------------------------------- progress API
def test_progress_callback_reports_done_and_total(cell):
    plan = _plan(cell, 3)
    scene, snapshots, progress = _collect(plan)

    assert scene is not None
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert len(snapshots) == len(plan)
    assert [len(snapshot.cells) for snapshot in snapshots] == [1, 2, 3]
    # The last snapshot already holds everything the finished scene does.
    assert snapshots[-1].vertex_count == scene.vertex_count


def test_first_cell_snapshot_is_a_correct_subset(cell):
    plan = _plan(cell, 3)
    _, snapshots, _ = _collect(plan)
    first = snapshots[0]
    reference = _build(plan[:1])

    assert reference is not None
    assert len(first.cells) == 1
    assert first.cells[0].first_vertex == 0
    assert first.cells[0].part_count == len(first.parts)
    assert first.vertex_count > 0
    # The snapshot is the first cell's contribution, positioned exactly as it
    # will be in the finished scene.
    np.testing.assert_array_equal(first.positions, reference.positions)
    assert first.positions.tobytes() == reference.positions.tobytes()
    assert first.parts == reference.parts
    assert first.cells == reference.cells


def test_progress_refusal_returns_no_scene_and_stops_assembly(cell):
    plan = _plan(cell, 3)
    calls = []

    def on_cell(snapshot, done, total):
        calls.append(done)
        return False

    assert _build(plan, on_cell=on_cell) is None
    # Refused at the first cell: the remaining cells are never assembled.
    assert calls == [1]


def test_cancelled_after_the_first_cell_returns_no_final_scene(cell):
    plan = _plan(cell, 3)
    cancelled = threading.Event()
    snapshots = []

    def on_cell(snapshot, done, total):
        snapshots.append(snapshot)
        cancelled.set()
        return True

    assert _build(plan, on_cell=on_cell, cancelled=cancelled.is_set) is None
    assert len(snapshots) == 1


def test_no_callback_builds_the_whole_scene(cell):
    plan = _plan(cell, 2)
    scene = _build(plan)

    assert scene is not None
    assert len(scene.cells) == 2
    assert not scene.is_empty


# ----------------------------------------------------------------- ordering
def test_every_snapshot_matches_the_prefix_build_order(cell):
    plan = _plan(cell, 4)
    _, snapshots, progress = _collect(plan)

    for index, snapshot in enumerate(snapshots, start=1):
        assert progress[index - 1] == (index, len(plan))
        reference = _build(plan[:index])
        assert reference is not None
        # Plan order, then reversed for assembly: the worst-ranked completed
        # cell is first and the best-ranked is last.
        assert [span.path for span in snapshot.cells] == [
            entry.path for entry in reversed(plan[:index])
        ]
        assert snapshot.cells == reference.cells
        assert snapshot.parts == reference.parts
        assert snapshot.unsupported == reference.unsupported
        np.testing.assert_array_equal(snapshot.positions, reference.positions)
        np.testing.assert_array_equal(snapshot.colours, reference.colours)

        offset = 0
        for span in snapshot.cells:
            assert span.first_vertex == offset
            offset += span.vertex_count
        assert offset == snapshot.vertex_count


def test_final_scene_is_identical_to_a_non_progressive_build(cell):
    plan = _plan(cell, 3)
    progressed, _, _ = _collect(plan)
    plain = _build(plan)

    assert progressed is not None
    assert plain is not None
    assert progressed.parts == plain.parts
    assert progressed.cells == plain.cells
    assert progressed.skipped == plain.skipped
    assert progressed.unsupported == plain.unsupported
    assert progressed.accounted_bytes == plain.accounted_bytes
    assert progressed.positions.tobytes() == plain.positions.tobytes()
    assert progressed.colours.tobytes() == plain.colours.tobytes()
    assert progressed.soundings.tobytes() == plain.soundings.tobytes()
