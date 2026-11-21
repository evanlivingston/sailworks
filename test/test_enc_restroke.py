"""Focused regression for the ENC fixed-width stroke patch.

The user-approved scope: during a retained-scene zoom the GPU ENC coastline
(``COALNE``, 1.5 logical px) and depth contour (``DEPCNT``, 0.5 px) keep
their screen width while their centreline zooms with the same affine matrix
every other cell content follows, and at the published camera the cell
buffers hold the baked soup bit for bit (nominal parity).  The patch
rewrites the parsed segments' vertices inside the cell buffers they came
from, so per-cell draw order and culling stay the baked behaviour's.  These
checks exercise the parsed plan, the per-cell grouping and the per-frame
quad derivation directly; the render-thread wiring they feed is covered by
the lifecycle tests in :mod:`test_enc_gpu`.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

pytest.importorskip("numpy")
pytest.importorskip("PySide6")

from shapely.geometry import LineString  # noqa: E402

from sailworks.qt import encgpu  # noqa: E402
from sailworks.rendering.vectordrawer.encmesh import (  # noqa: E402
    LINE,
    MeshPart,
    SceneMesh,
)
from sailworks.rendering.vectordrawer.encstroke import stroke_lines  # noqa: E402

#: Isotropic stand-in for the world-pixel projection: 100 px per degree on
#: both axes, so a stroke baked at ``stroke_pixels`` comes out exactly that
#: many reference-frame pixels wide, like the real Mercator projection does.
_PROJECTION_SCALE = 100.0

_ZOOM_IN = (2.0, 0.0, 5.0, 0.0, 2.0, -3.0)
_ZOOM_OUT = (0.5, 0.0, 1.0, 0.0, 0.5, 2.0)
#: Anisotropic case with shear and translation, so the inverse linear part
#: is not the identity and the perpendicular re-lay has to work for real.
_STRETCH = (2.0, 0.25, -1.0, 0.0, 1.0, 0.5)


def _project(points):
    points = np.asarray(points, dtype=np.float64)
    return points * _PROJECTION_SCALE


def _quads(width, coordinates):
    return stroke_lines(
        LineString(coordinates), stroke_pixels=width, projection=_project
    )


def _two_class_mesh():
    """One cell: coastline, depth contour and a class the scope leaves alone."""
    coastline = _quads(1.5, [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)])
    contour = _quads(0.5, [(0.0, 5.0), (0.0, 6.0), (0.0, 7.0)])
    cable = _quads(1.0, [(5.0, 0.0), (6.0, 0.0)])
    positions = np.concatenate((coastline, contour, cable))
    coast_count = int(coastline.shape[0])
    both_count = coast_count + int(contour.shape[0])
    return SceneMesh(
        positions=positions,
        parts=(
            MeshPart(0, "COALNE", 0, 1, LINE, 0, coast_count, (10, 20, 30, 255)),
            MeshPart(
                0,
                "DEPCNT",
                1,
                2,
                LINE,
                coast_count,
                both_count - coast_count,
                (40, 50, 60, 255),
            ),
            MeshPart(
                0,
                "SLCONS",
                2,
                3,
                LINE,
                both_count,
                int(positions.shape[0]) - both_count,
                (70, 80, 90, 255),
            ),
        ),
        unsupported=(),
        bounds=None,
    )


def _frame(mesh):
    frame = encgpu.build_static_frame(mesh, _project)
    assert frame is not None
    return frame


def _item_positions(points, coeffs):
    """Map reference-frame local positions through the full affine."""
    a, b, c, d, e, f = coeffs
    x = np.asarray(points)[..., 0].astype(np.float64)
    y = np.asarray(points)[..., 1].astype(np.float64)
    return np.stack((a * x + b * y + c, d * x + e * y + f), axis=-1)


def test_plan_parses_the_two_classes_and_keeps_the_baked_buffers():
    mesh = _two_class_mesh()
    frame = _frame(mesh)
    plan = frame.restroke
    assert plan is not None
    assert plan.segments == 5  # 3 coastline segments + 2 contour segments
    assert plan.runs == ((0, 3), (18, 2))
    groups = encgpu._restroke_groups(plan, [(0, 30)])
    assert groups == ((0, 0, 0, 0, 3), (0, 0, 18, 3, 2))
    # No overlay and no blanking: the buffers keep every baked colour, the
    # patch rewrites only the runs' positions inside them.
    rgba = frame.cells[0].vertices["rgba"]
    assert np.array_equal(rgba[0], (10, 20, 30, 255))
    assert np.array_equal(rgba[18], (40, 50, 60, 255))
    assert np.array_equal(rgba[30], (70, 80, 90, 255))
    assert np.all(rgba[:, 3] == 255)


def test_identity_camera_holds_the_parsed_corners_bit_for_bit():
    mesh = _two_class_mesh()
    frame = _frame(mesh)
    plan = frame.restroke
    out = encgpu._restroke_quad_rows(plan.corners).reshape(-1, 2)
    # The quads are the baked soup's own vertices: the settled picture is
    # exactly what the previous behaviour drew.
    projected = _project(mesh.positions)
    base = (projected.min(axis=0) + projected.max(axis=0)) / 2.0
    local = (projected - base).astype(np.float32)
    assert np.array_equal(out, local[:30])


@pytest.mark.parametrize("coeffs", [_ZOOM_IN, _ZOOM_OUT, _STRETCH])
def test_zoom_keeps_logical_width_and_moves_the_centreline(coeffs):
    mesh = _two_class_mesh()
    frame = _frame(mesh)
    plan = frame.restroke
    lin = (coeffs[0], coeffs[1], coeffs[3], coeffs[4])
    out = encgpu._restroke_quad_rows(plan.corners, lin).astype(np.float64)
    item = _item_positions(out, coeffs)
    corners = plan.corners.astype(np.float64)
    centre0 = (corners[:, 0] + corners[:, 3]) * 0.5
    centre1 = (corners[:, 1] + corners[:, 2]) * 0.5
    screen0 = _item_positions(centre0[:, None, :], coeffs)[:, 0]
    screen1 = _item_positions(centre1[:, None, :], coeffs)[:, 0]
    screen_direction = screen1 - screen0
    lengths = np.hypot(screen_direction[:, 0], screen_direction[:, 1])
    # The centreline follows the affine exactly like the cells do.
    assert np.allclose((item[:, 0] + item[:, 5]) * 0.5, screen0, atol=1e-4)
    assert np.allclose((item[:, 1] + item[:, 2]) * 0.5, screen1, atol=1e-4)
    # The two classes keep their logical-pixel widths at every camera.
    delta = item[:, 0] - item[:, 5]
    widths = np.hypot(delta[:, 0], delta[:, 1])
    assert np.allclose(widths[:3], 1.5, atol=1e-4)
    assert np.allclose(widths[3:], 0.5, atol=1e-4)
    # The half-width stays perpendicular to the transformed segment.  The
    # quads round to float32 cell-buffer coordinates, so under shear the
    # normalized dot bottoms out around 1e-5 -- an angle error of ~0.001
    # degrees, noise, not a layout change.
    dots = (
        delta[:, 0] * screen_direction[:, 0] + delta[:, 1] * screen_direction[:, 1]
    ) / (np.hypot(delta[:, 0], delta[:, 1]) * lengths)
    assert np.max(np.abs(dots)) < 1e-4
    # The near side stays the near side: no segment flips across its line.
    edges = item[:, 1] - item[:, 0]
    assert np.all(
        (edges[:, 0] * screen_direction[:, 0] + edges[:, 1] * screen_direction[:, 1])
        > 0.0
    )


def test_a_fragment_stops_the_parse_and_keeps_the_previous_behaviour():
    intact = _quads(1.5, [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])
    # Six vertices that do not repeat their corners: a clipped fragment's
    # re-triangulated soup breaks the six-vertex layout.
    fragment = np.array(
        [
            [10.0, 10.0],
            [11.0, 10.0],
            [11.0, 11.0],
            [12.0, 10.0],
            [13.0, 11.0],
            [14.0, 12.0],
        ]
    )
    positions = np.concatenate((intact, fragment))
    mesh = SceneMesh(
        positions=positions,
        parts=(
            MeshPart(
                0, "COALNE", 0, 1, LINE, 0, int(positions.shape[0]), (10, 20, 30, 255)
            ),
        ),
        unsupported=(),
        bounds=None,
    )
    frame = _frame(mesh)
    plan = frame.restroke
    assert plan is not None
    assert plan.segments == 2  # the intact prefix only
    assert plan.runs == ((0, 2),)
    # Nothing is blanked: the fragment keeps today's rendering and the
    # prefix keeps its baked colours until the patch rewrites its runs.
    rgba = frame.cells[0].vertices["rgba"]
    assert np.all(rgba[:, 3] == 255)
    assert np.array_equal(rgba[0], (10, 20, 30, 255))


def test_the_segment_bound_falls_back_to_the_previous_behaviour(monkeypatch):
    monkeypatch.setattr(encgpu, "RESTROKE_MAX_SEGMENTS", 1)
    mesh = _two_class_mesh()
    frame = _frame(mesh)
    assert frame.restroke is None
    rgba = frame.cells[0].vertices["rgba"]
    assert np.all(rgba[:, 3] == 255)  # nothing blanked: previous rendering


def test_classes_outside_the_scope_are_never_parsed():
    cable = _quads(1.0, [(5.0, 0.0), (6.0, 0.0), (7.0, 0.0)])
    mesh = SceneMesh(
        positions=cable,
        parts=(
            MeshPart(
                0, "SLCONS", 0, 1, LINE, 0, int(cable.shape[0]), (70, 80, 90, 255)
            ),
        ),
        unsupported=(),
        bounds=None,
    )
    frame = _frame(mesh)
    assert frame.restroke is None
    rgba = frame.cells[0].vertices["rgba"]
    assert np.all(rgba[:, 3] == 255)


def test_a_run_split_across_cells_falls_back_to_the_previous_behaviour():
    corners = np.zeros((3, 4, 2), dtype=np.float32)
    plan = encgpu._RestrokePlan(3, corners, ((0, 3),))
    # The run's 18 vertices straddle the first cell's 12: patching per cell
    # would draw the quad's halves at different times, so the plan is
    # rejected and the frame keeps the baked rendering whole.
    assert encgpu._restroke_groups(plan, [(0, 12), (12, 6)]) is None


def test_runs_group_into_their_own_cells():
    corners = np.zeros((3, 4, 2), dtype=np.float32)
    plan = encgpu._RestrokePlan(3, corners, ((0, 2), (12, 1)))
    groups = encgpu._restroke_groups(plan, [(0, 12), (12, 6)])
    assert groups == ((0, 0, 0, 0, 2), (1, 12, 12, 2, 1))
