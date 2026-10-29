"""Focused contracts for deterministic per-seed streamline paths."""

import numpy as np

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.rendering.streamlines import density_parameters
from gweatherrouting.rendering.streamlinepaths import (
    PathSegment,
    StreamlinePath,
    _seeds,
    _screen_geo,
    paths_to_flat,
    paths_to_render_buffers,
    trace_streamline_paths,
)
from test.map_helpers import MapCanvas


def _view():
    canvas = MapCanvas()
    canvas._center_lat = canvas._center_lon = 0
    canvas._zoom = 8
    return MapViewport(canvas)


def _field(value=7.0):
    return [
        [(270.0, value, (lat, lon)) for lat in np.linspace(-2, 2, 17)]
        for lon in np.linspace(-2, 2, 17)
    ]


def test_seed_identity_and_seed_coordinate_repeat_exactly():
    view = _view()
    paths = trace_streamline_paths(view, _field(), lambda *_: False)
    again = trace_streamline_paths(view, _field(), lambda *_: False)
    seeds = _seeds(view, "standard")
    assert seeds == _seeds(view, "standard")
    assert all(0 <= x < view.width and 0 <= y < view.height for _, x, y in seeds)
    assert [(p.seed_id, p.vertices[len(p.vertices) // 2]) for p in paths] == [
        (p.seed_id, p.vertices[len(p.vertices) // 2]) for p in again
    ]
    assert paths and all(
        p.vertices and len(p.segments) == len(p.vertices) - 1 for p in paths
    )


def test_capped_seed_grid_keeps_coverage_across_the_viewport():
    canvas = MapCanvas()
    canvas._center_lat = canvas._center_lon = 0
    canvas._zoom = 8
    canvas.width = 1600
    canvas.height = 1200
    view = MapViewport(canvas)

    seeds = _seeds(view, "all")
    assert len(seeds) == 500
    xs = [seed[1] for seed in seeds]
    ys = [seed[2] for seed in seeds]
    assert min(xs) < view.width * 0.1
    assert max(xs) > view.width * 0.9
    assert min(ys) < view.height * 0.1
    assert max(ys) > view.height * 0.9


def test_seed_selection_thins_low_and_high_candidate_counts_for_each_density():
    for width, height in ((240, 180), (1600, 1200)):
        canvas = MapCanvas()
        canvas._center_lat = canvas._center_lon = 0
        canvas._zoom = 8
        canvas.width, canvas.height = width, height
        view = MapViewport(canvas)
        for density in ("sparse", "standard", "dense", "very_dense", "all"):
            spacing, cap, _ = density_parameters(density)
            cx, cy = view._lon_to_x(view._center_lon), view._lat_to_y(view._center_lat)
            left, top = cx - width / 2, cy - height / 2
            cells = [
                (ix, iy)
                for iy in range(
                    int(np.floor(top / spacing)),
                    int(np.floor((top + height) / spacing)) + 1,
                )
                for ix in range(
                    int(np.floor(left / spacing)),
                    int(np.floor((left + width) / spacing)) + 1,
                )
                if 0 <= (ix + 0.5) * spacing - left < width
                and 0 <= (iy + 0.5) * spacing - top < height
            ]
            expected = (
                (cap + 3) // 4
                if len(cells) > cap
                else sum(ix % 2 == 0 and iy % 2 == 0 for ix, iy in cells) or 1
            )
            assert len(_seeds(view, density)) == expected


def test_flat_conversion_preserves_path_segment_order_and_metadata():
    path = StreamlinePath(
        (8, 0, 0),
        ((1.0, 2.0), (3.0, 4.0), (5.0, 6.0)),
        (PathSegment(7.5, False, True), PathSegment(8.5, True, False)),
    )
    converted = paths_to_flat((path,))
    np.testing.assert_allclose(converted[0], [1.0, 3.0])
    np.testing.assert_allclose(converted[1], [2.0, 4.0])
    np.testing.assert_allclose(converted[2], [3.0, 5.0])
    np.testing.assert_allclose(converted[3], [4.0, 6.0])
    np.testing.assert_allclose(converted[4], [7.5, 8.5])
    np.testing.assert_array_equal(converted[5], [False, True])
    np.testing.assert_array_equal(converted[6], [True, False])


def test_render_buffers_are_geographic_bucketed_and_immutable():
    path = StreamlinePath(
        (0, 0, 0),
        ((0.0, 0.0), (45.0, 90.0), (-90.0, 180.0), (0.0, 181.0)),
        (
            PathSegment(3.9, False, True),
            PathSegment(8.0, True, True),
            PathSegment(100.0, False, False),
        ),
    )
    buffers = paths_to_render_buffers((path,), over_land=False)
    assert set(buffers) == {1, 14}
    lines, arrows = buffers[1]
    np.testing.assert_allclose(lines[0], [0.5, 0.5, 0.75, 0.3597250369])
    np.testing.assert_allclose(arrows, lines)
    assert buffers[14][0].shape == (1, 4)
    assert buffers[14][1].shape == (0, 4)
    assert buffers[14][0][0, 0] == 0.0  # longitude 181 wraps to -179
    assert all(not array.flags.writeable for pair in buffers.values() for array in pair)

    with_land = paths_to_render_buffers((path,), over_land=True)
    assert with_land[4][0].shape == (1, 4)
    assert with_land[4][1].shape == (1, 4)


def test_traced_paths_skip_land_sampling_and_mark_segments_unfiltered():
    def coastline_must_not_be_called(*_):
        raise AssertionError("streamline trace must not query coastlines")

    paths = trace_streamline_paths(_view(), _field(), coastline_must_not_be_called)
    flat = paths_to_flat(paths)
    assert sum(len(path.segments) for path in paths) == len(flat[0])
    assert not flat[5].any()


def test_nan_termination_budgets_and_cancellation():
    field = _field()
    field[8][8] = (270.0, float("nan"), (0, 0))
    paths = trace_streamline_paths(_view(), field, lambda *_: False)
    assert paths is not None
    assert (
        trace_streamline_paths(
            _view(), _field(), lambda *_: False, cancelled=lambda: True
        )
        is None
    )
    assert (
        trace_streamline_paths(_view(), [], lambda *_: False, cancelled=lambda: True)
        is None
    )


def test_paths_are_ordered_and_join_at_stable_seed():
    view = _view()
    paths = trace_streamline_paths(view, _field(), lambda *_: False)
    seeds = {seed_id: (x, y) for seed_id, x, y in _seeds(view, "standard")}
    for path in paths:
        sx, sy = seeds[path.seed_id]
        seed = _screen_geo(view, sx, sy)
        assert seed in path.vertices
        assert len(path.segments) == len(path.vertices) - 1


def test_paths_have_no_cross_seed_occupancy_coupling():
    view = _view()
    all_paths = trace_streamline_paths(view, _field(), lambda *_: False)
    first = trace_streamline_paths(
        view, _field(), lambda *_: False, cancelled=lambda: False
    )
    assert [(p.seed_id, p.vertices) for p in all_paths] == [
        (p.seed_id, p.vertices) for p in first
    ]
