"""GPU line payload for COALNE/DEPCNT in :mod:`encmesh`.

The slice has to do two separable things, and each is checked on its own:

* extraction - clip the source line to the cell region, drop degenerate
  segments and pack ``x0, y0, x1, y1``, a screen-pixel width, the resolved
  colour and the ``(on, off)`` dash pair per segment;
* the CPU-stroke skip - for those two classes the quad expansion must not run
  at all, while every other class keeps it.

No rendering is wired, so the default flag stays off and the CPU stroke stays
in place; the tests opt in explicitly with ``gpu_lines=True``.
"""

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, box

from sailworks.rendering import encview
from sailworks.rendering.vectordrawer import encmesh


@pytest.fixture
def gpu_lines_available():
    """Report a successfully created native item for one test only."""
    encmesh.set_gpu_lines_available(True)
    try:
        yield
    finally:
        encmesh.set_gpu_lines_available(False)


class _Record:
    """The attributes ``build_scene_mesh`` reads from a source record."""

    def __init__(self, index, source_class, geometry, fid=None):
        self.layer_index = 0
        self.source_class = source_class
        self.sequence = index
        self.fid = index if fid is None else fid
        self.wkb = shapely.to_wkb(geometry)
        self.attributes = ()

    def attribute(self, name):
        return None


class _Scene:
    def __init__(self, *records):
        self.records = records


def _scene(*specs):
    return _Scene(
        *[
            _Record(index, source_class, geometry)
            for index, (source_class, geometry) in enumerate(specs)
        ]
    )


def _projection(positions):
    """One screen pixel is one source unit: the identity camera."""
    return np.asarray(positions)


def _build(geometry, source_class="COALNE", region=None, **kwargs):
    kwargs.setdefault("stroke_pixels", 1.0)
    kwargs.setdefault("projection", _projection)
    kwargs.setdefault("area_outlines", False)
    return encmesh.build_scene_mesh(
        _scene((source_class, geometry)),
        coverage=region,
        clip=None if region is None else True,
        gpu_lines=True,
        **kwargs,
    )


# --- extraction -----------------------------------------------------------


def test_inside_line_emits_one_segment_per_edge():
    line = LineString([(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)])
    segments = _build(line).line_segments

    assert segments.segment_count == 2
    assert segments.endpoints.dtype == np.float32
    assert segments.endpoints.shape == (2, 4)
    np.testing.assert_allclose(
        segments.endpoints,
        np.array([[0.0, 0.0, 1.0, 1.0], [1.0, 1.0, 2.0, 0.0]], dtype=np.float32),
    )
    # Source order is preserved: each record continues where the previous ended.
    np.testing.assert_array_equal(
        segments.endpoints[1:, 0:2], segments.endpoints[:-1, 2:4]
    )


def test_multilinestring_keeps_every_part():
    geometry = MultiLineString([[(0, 0), (1, 0)], [(5, 5), (6, 6), (7, 5)]])
    segments = _build(geometry).line_segments
    assert segments.segment_count == 3


def test_crossing_line_is_clipped_to_the_region():
    region = box(0.0, 0.0, 1.0, 1.0)
    line = LineString([(-1.0, 0.5), (2.0, 0.5)])
    segments = _build(line, region=region).line_segments

    assert segments.segment_count == 1
    x0, y0, x1, y1 = segments.endpoints[0]
    assert float(x0) == pytest.approx(0.0, abs=1e-6)
    assert float(x1) == pytest.approx(1.0, abs=1e-6)
    assert float(y0) == pytest.approx(0.5, abs=1e-6)
    assert float(y1) == pytest.approx(0.5, abs=1e-6)
    # Nothing may bleed past the clip region.
    assert region.buffer(1e-9).covers(LineString([(x0, y0), (x1, y1)]))


def test_partial_line_keeps_only_the_inside_piece():
    region = box(0.0, 0.0, 1.0, 1.0)
    line = LineString([(0.25, 0.25), (0.75, 0.75), (3.0, 3.0)])
    segments = _build(line, region=region).line_segments

    assert segments.segment_count == 2
    for x0, y0, x1, y1 in segments.endpoints:
        assert region.buffer(1e-9).covers(LineString([(x0, y0), (x1, y1)]))


def test_outside_line_emits_nothing():
    region = box(0.0, 0.0, 1.0, 1.0)
    line = LineString([(10.0, 10.0), (11.0, 11.0)])
    segments = _build(line, region=region).line_segments

    assert segments.is_empty
    assert segments.segment_count == 0
    assert segments.nbytes == 0


def test_zero_length_segment_is_dropped():
    line = LineString([(0.0, 0.0), (0.0, 0.0), (1.0, 0.0)])
    segments = _build(line).line_segments

    assert segments.segment_count == 1
    np.testing.assert_allclose(segments.endpoints[0], [0.0, 0.0, 1.0, 0.0])


def test_non_line_geometry_is_empty_not_an_error():
    assert _build(Point(0.0, 0.0), source_class="SLCONS").line_segments.is_empty
    assert encmesh.gpu_line_segments(None, None, 1.0).is_empty
    assert encmesh.gpu_line_segments(LineString(), None, 1.0).is_empty


def test_empty_payload_constant_is_typed():
    empty = encmesh.EMPTY_LINE_SEGMENTS
    assert empty.endpoints.shape == (0, 4)
    assert empty.endpoints.dtype == np.float32
    assert empty.widths.shape == (0,)
    assert empty.colours.shape == (0, 4)
    assert empty.colours.dtype == np.uint8
    assert empty.dash_on.shape == (0,)
    assert empty.dash_on.dtype == np.float32
    assert empty.dash_off.shape == (0,)
    assert empty.dash_off.dtype == np.float32
    assert empty.phase.shape == (0,)
    assert empty.phase.dtype == np.float32


# --- width and colour -----------------------------------------------------


@pytest.mark.parametrize(
    ("source_class", "expected_width"),
    [("COALNE", 1.5), ("DEPCNT", 0.5)],
)
def test_width_comes_from_line_styles_in_screen_pixels(source_class, expected_width):
    segments = _build(
        LineString([(0.0, 0.0), (3.0, 0.0), (3.0, 3.0)]),
        source_class=source_class,
        stroke_pixels=4.0,
    ).line_segments

    assert segments.widths.dtype == np.float32
    assert segments.segment_count == 2
    np.testing.assert_allclose(
        segments.widths, np.full(2, expected_width, dtype=np.float32)
    )


def test_colour_is_the_resolved_palette_colour(monkeypatch):
    seen = (10, 20, 30, 200)
    calls = []

    def fake_part_colour(source_class, role, drval1, drval2, palette):
        calls.append((source_class, role, palette))
        return seen

    monkeypatch.setattr(encmesh, "part_colour", fake_part_colour)
    palette = object()
    segments = _build(
        LineString([(0.0, 0.0), (1.0, 0.0)]), palette=palette
    ).line_segments

    assert calls and calls[0][0] == "COALNE"
    assert calls[0][2] is palette
    assert segments.colours.dtype == np.uint8
    # One colour per *segment*: a two-point line packs one segment, not two.
    np.testing.assert_array_equal(
        segments.colours, np.array([seen], dtype=np.uint8).reshape(1, 4)
    )
    # The one palette lookup is shared with the part, never resolved twice.
    assert len(calls) == 1


def test_colour_is_white_without_a_palette():
    segments = _build(LineString([(0.0, 0.0), (1.0, 0.0)])).line_segments
    np.testing.assert_array_equal(
        segments.colours,
        np.array([[255, 255, 255, 255]], dtype=np.uint8),
    )


def test_packed_layout_is_one_float32_record_per_segment():
    segments = encmesh.gpu_line_segments(
        LineString([(1.0, 2.0), (3.0, 4.0)]), (255, 0, 0, 255), 1.5
    )
    packed = segments.packed()

    assert packed.dtype == np.float32
    # endpoints, width, RGBA, the dash pair and the dash phase: 12 float32.
    assert packed.shape == (1, 12)
    np.testing.assert_allclose(packed[0, :4], [1.0, 2.0, 3.0, 4.0])
    assert packed[0, 4] == pytest.approx(1.5)
    np.testing.assert_allclose(packed[0, 5:9], [1.0, 0.0, 0.0, 1.0])
    # A solid single segment still emits both dash fields, as zeros, and its
    # phase is the start of its own chain.
    np.testing.assert_allclose(packed[0, 9:], [0.0, 0.0, 0.0])

    blob = segments.to_bytes()
    assert len(blob) == segments.segment_count * 12 * 4
    assert len(blob) == 48
    assert segments.nbytes == (
        segments.endpoints.nbytes
        + segments.widths.nbytes
        + segments.colours.nbytes
        + segments.dash_on.nbytes
        + segments.dash_off.nbytes
        + segments.phase.nbytes
    )


def test_packed_dash_pair_survives_the_float32_upload():
    segments = encmesh.gpu_line_segments(
        LineString([(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]),
        (0, 0, 255, 255),
        0.5,
        dash=(3.0, 3.0),
    )
    packed = segments.packed()

    assert packed.shape == (2, 12)
    np.testing.assert_allclose(packed[:, 9:11], np.full((2, 2), 3.0))
    # The dash sits after the unchanged 9-float prefix, so a reader that
    # ignores dashes still finds width and colour at their old offsets.
    np.testing.assert_allclose(packed[0, 4], 0.5)
    np.testing.assert_allclose(packed[0, 5:9], [0.0, 0.0, 1.0, 1.0])
    # The phase is appended after the dash pair: the second segment continues
    # the first, so its phase is that segment's world length.
    assert packed[0, 11] == pytest.approx(0.0, abs=1e-7)
    assert packed[1, 11] > packed[0, 11]


def test_phase_accumulates_along_a_polyline():
    segments = encmesh.gpu_line_segments(
        LineString([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]), (0, 0, 0, 255), 1.0
    )

    phase = segments.phase
    assert phase.dtype == np.float32
    # The first segment starts its chain; the second inherits the first's
    # world length, which for one degree of longitude is exactly 1/360.
    assert phase[0] == pytest.approx(0.0, abs=1e-6)
    assert phase[1] == pytest.approx(1.0 / 360.0, rel=1e-6)


def test_phase_resets_per_part():
    segments = encmesh.gpu_line_segments(
        MultiLineString(
            [[(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)], [(5.0, 5.0), (6.0, 5.0)]]
        ),
        None,
        1.0,
    )

    # Two segments in the first part, one in the second: each part starts its
    # own chain, so the third record's phase is zero again.
    assert segments.segment_count == 3
    np.testing.assert_allclose(segments.phase[0], 0.0, atol=1e-6)
    assert segments.phase[1] > 0.0
    np.testing.assert_allclose(segments.phase[2], 0.0, atol=1e-6)


def test_clipped_pieces_start_their_own_phase():
    line = LineString([(-2.0, 0.0), (2.0, 0.0)])
    segments = encmesh.gpu_line_segments(
        line, None, 1.0, region=box(-1.0, -1.0, 1.0, 1.0)
    )

    # One clipped piece, so its only segment restarts the chain at zero even
    # though the source segment began two degrees to the west.
    assert segments.segment_count == 1
    np.testing.assert_allclose(segments.phase, [0.0], atol=1e-6)


# --- CPU stroke skip ------------------------------------------------------


def _record_stroke_lines(monkeypatch):
    calls = []

    def fake_stroke_lines(geometry, width, **kwargs):
        calls.append(geometry)
        # A real vertex run, not an empty one: ``emit`` drops a zero-length
        # part, so an empty fake would hide the very part these tests check.
        return np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float64)

    monkeypatch.setattr(encmesh, "stroke_lines", fake_stroke_lines)
    return calls


@pytest.mark.parametrize("source_class", ["COALNE", "DEPCNT"])
def test_gpu_classes_never_call_the_cpu_stroke(monkeypatch, source_class):
    calls = _record_stroke_lines(monkeypatch)
    timings = {}
    mesh = _build(
        LineString([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]),
        source_class=source_class,
        timings=timings,
    )

    assert calls == []
    assert mesh.line_segments.segment_count == 2
    assert "stroke_line" not in timings
    assert "gpu_lines" in timings
    # No LINE part and no vertices: the payload is the only line output.
    assert mesh.vertex_count == 0
    assert [part for part in mesh.parts if part.kind == encmesh.LINE] == []


def test_other_line_classes_still_stroke(monkeypatch):
    calls = _record_stroke_lines(monkeypatch)
    mesh = _build(LineString([(0.0, 0.0), (1.0, 0.0)]), source_class="SLCONS")

    assert len(calls) == 1
    assert mesh.line_segments.is_empty


def test_flag_off_keeps_the_area_outline_part_and_stroke(monkeypatch):
    calls = _record_stroke_lines(monkeypatch)
    mesh = encmesh.build_scene_mesh(
        _scene(("DEPARE", box(0.0, 0.0, 2.0, 2.0))),
        stroke_pixels=1.0,
        projection=_projection,
        gpu_lines=False,
    )

    assert len(calls) == 1  # the DEPARE outline, clipped as before
    assert mesh.line_segments.is_empty
    assert [part.kind for part in mesh.parts] == [
        encmesh.AREA_FILL,
        encmesh.AREA_OUTLINE,
    ]


# --- area outlines on the same payload ------------------------------------
#
# The boundary stroke is the dominant per-cell cost, so with the slice on it
# has to leave the CPU tessellator exactly the way COALNE/DEPCNT do: segments
# in the payload, no ``stroke_outline``, no AREA_OUTLINE part, fill untouched.


def _build_area(source_class="DEPARE", geometry=None, **kwargs):
    kwargs.setdefault("stroke_pixels", 1.0)
    kwargs.setdefault("projection", _projection)
    kwargs.setdefault("gpu_lines", True)
    if geometry is None:
        geometry = box(0.0, 0.0, 2.0, 2.0)
    return encmesh.build_scene_mesh(_scene((source_class, geometry)), **kwargs)


def _part_positions(mesh, part):
    """The vertex run one part owns, so a fill can be compared on its own."""
    start = part.first_vertex
    return mesh.positions[start : start + part.vertex_count]


def test_area_outline_inside_emits_one_segment_per_ring_edge(monkeypatch):
    calls = _record_stroke_lines(monkeypatch)
    timings = {}
    mesh = _build_area(timings=timings)

    assert calls == []
    assert mesh.line_segments.segment_count == 4
    assert "stroke_outline" not in timings
    assert "gpu_lines" in timings
    # No AREA_OUTLINE part and no LINE part: the payload is the only stroke
    # output, so nothing is drawn twice.
    assert [part.kind for part in mesh.parts] == [encmesh.AREA_FILL]
    assert mesh.vertex_count == 6  # the fill's two triangles


def test_area_outline_crossing_is_clipped_to_the_region():
    region = box(0.0, 0.0, 1.0, 1.0)
    segments = _build_area(
        geometry=box(-1.0, -1.0, 2.0, 2.0), coverage=region, clip=True
    ).line_segments

    assert segments.segment_count == 4
    for x0, y0, x1, y1 in segments.endpoints:
        assert region.buffer(1e-9).covers(LineString([(x0, y0), (x1, y1)]))


@pytest.mark.parametrize(
    ("source_class", "expected_width"),
    [
        # Every solid AREA_OUTLINE_STYLES entry, plus LNDARE, which carries no
        # style of its own and inherits the caller's pixel width.
        ("DEPARE", 0.5),
        ("DRGARE", 0.5),
        ("LAKARE", 0.5),
        ("ACHARE", 0.5),
        ("LNDARE", 2.0),
    ],
)
def test_area_outline_width_and_colour_come_from_the_area_styles(
    monkeypatch, source_class, expected_width
):
    roles = []
    outline_colour = (11, 22, 33, 200)

    def fake_part_colour(source_class, role, drval1, drval2, palette):
        roles.append(role)
        return outline_colour

    monkeypatch.setattr(encmesh, "part_colour", fake_part_colour)
    segments = encmesh.build_scene_mesh(
        _scene((source_class, box(0.0, 0.0, 2.0, 2.0))),
        stroke_pixels=2.0,
        projection=_projection,
        palette=object(),
        gpu_lines=True,
    ).line_segments

    assert segments.segment_count == 4
    np.testing.assert_allclose(
        segments.widths, np.full(4, expected_width, dtype=np.float32)
    )
    np.testing.assert_array_equal(
        segments.colours,
        np.tile(np.array(outline_colour, dtype=np.uint8), (4, 1)),
    )
    # The outline role, resolved once, is what every segment carries.
    assert roles.count(encmesh.ROLE_OUTLINE) == 1


@pytest.mark.parametrize("source_class", ["RESARE", "CTNARE"])
def test_dashed_area_outlines_ride_the_payload(monkeypatch, source_class):
    calls = _record_stroke_lines(monkeypatch)
    timings = {}
    mesh = _build_area(source_class=source_class, timings=timings)

    # The dash pair travels with the segments, so the CPU stroke never runs
    # and no AREA_OUTLINE part is emitted for the boundary.
    assert calls == []
    assert "stroke_outline" not in timings
    assert "gpu_lines" in timings
    assert mesh.line_segments.segment_count == 4
    np.testing.assert_allclose(
        mesh.line_segments.dash_on, np.full(4, 3.0, dtype=np.float32)
    )
    np.testing.assert_allclose(
        mesh.line_segments.dash_off, np.full(4, 3.0, dtype=np.float32)
    )
    np.testing.assert_allclose(
        mesh.line_segments.widths, np.full(4, 0.5, dtype=np.float32)
    )
    assert [part.kind for part in mesh.parts] == [encmesh.AREA_FILL]


def test_solid_area_outline_carries_a_zero_dash():
    mesh = _build_area()

    assert mesh.line_segments.segment_count == 4
    np.testing.assert_allclose(mesh.line_segments.dash_on, np.zeros(4))
    np.testing.assert_allclose(mesh.line_segments.dash_off, np.zeros(4))


@pytest.mark.parametrize("source_class", ["RESARE", "CTNARE"])
def test_flag_off_keeps_the_dashed_cpu_stroke(monkeypatch, source_class):
    calls = []
    dashes = []

    def fake_stroke_lines(geometry, width, **kwargs):
        calls.append(geometry)
        dashes.append(kwargs.get("dash"))
        return np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float64)

    monkeypatch.setattr(encmesh, "stroke_lines", fake_stroke_lines)
    timings = {}
    mesh = _build_area(source_class=source_class, gpu_lines=False, timings=timings)

    assert len(calls) == 1
    assert dashes == [(3.0, 3.0)]
    assert "stroke_outline" in timings
    assert mesh.line_segments.is_empty
    assert [part.kind for part in mesh.parts] == [
        encmesh.AREA_FILL,
        encmesh.AREA_OUTLINE,
    ]


def test_depare_fill_and_depth_bands_are_unaffected():
    geometry = MultiPolygon([box(0.0, 0.0, 1.0, 1.0), box(2.0, 2.0, 3.0, 3.0)])
    off = _build_area(geometry=geometry, gpu_lines=False)
    on = _build_area(geometry=geometry)

    # The fill part is untouched: compare the AREA_FILL slice, not the whole
    # mesh, because the flag-off mesh still carries the AREA_OUTLINE part
    # after it and that part is deliberately absent when the slice is on.
    fill_on = on.parts[0]
    fill_off = off.parts[0]
    assert fill_on.kind == encmesh.AREA_FILL
    assert fill_off.kind == encmesh.AREA_FILL
    assert (fill_on.first_vertex, fill_on.vertex_count) == (
        fill_off.first_vertex,
        fill_off.vertex_count,
    )
    np.testing.assert_allclose(
        _part_positions(on, fill_on), _part_positions(off, fill_off)
    )
    assert fill_on.colour == fill_off.colour

    assert [part.kind for part in on.parts] == [encmesh.AREA_FILL]
    # Flag off keeps the outline as a CPU part appended to the same fill.
    assert [part.kind for part in off.parts] == [
        encmesh.AREA_FILL,
        encmesh.AREA_OUTLINE,
    ]
    outline_off = off.parts[1]
    assert outline_off.first_vertex == fill_off.vertex_count
    # Both rings' edges, and the outline is no longer a part of its own.
    assert on.line_segments.segment_count == 8
    assert off.line_segments.is_empty


def test_area_outline_is_not_emitted_twice():
    geometry = MultiPolygon([box(0.0, 0.0, 1.0, 1.0), box(5.0, 5.0, 6.0, 6.0)])
    mesh = _build_area(geometry=geometry)

    assert mesh.line_segments.segment_count == 8
    assert encmesh.AREA_OUTLINE not in [part.kind for part in mesh.parts]
    assert encmesh.LINE not in [part.kind for part in mesh.parts]


def test_area_outline_segments_concatenate_in_source_order():
    def payload(source_class, geometry):
        return encmesh.build_scene_mesh(
            _scene((source_class, geometry)),
            stroke_pixels=1.0,
            projection=_projection,
            gpu_lines=True,
        ).line_segments

    first = payload("DEPARE", box(0.0, 0.0, 1.0, 1.0))
    second = payload("DRGARE", box(5.0, 5.0, 6.0, 6.0))
    joined = encmesh.concat_line_segments([first, second])

    assert joined.segment_count == 8
    np.testing.assert_allclose(joined.endpoints[:4].max(), 1.0)
    assert float(joined.endpoints[4:].min()) == pytest.approx(5.0)
    np.testing.assert_allclose(joined.widths, np.full(8, 0.5, dtype=np.float32))


def test_flag_defaults_off_and_keeps_the_cpu_path(monkeypatch):
    monkeypatch.delenv(encmesh.GPU_LINES_ENV, raising=False)
    encmesh.set_gpu_lines_available(False)
    calls = _record_stroke_lines(monkeypatch)
    mesh = encmesh.build_scene_mesh(
        _scene(("COALNE", LineString([(0.0, 0.0), (1.0, 0.0)]))),
        stroke_pixels=1.0,
        projection=_projection,
        area_outlines=False,
    )

    assert encmesh.gpu_lines_enabled() is False
    assert len(calls) == 1
    assert mesh.line_segments.is_empty
    assert [part.kind for part in mesh.parts] == [encmesh.LINE]


def test_flag_on_via_the_environment_when_available(monkeypatch, gpu_lines_available):
    monkeypatch.setenv(encmesh.GPU_LINES_ENV, "1")
    calls = _record_stroke_lines(monkeypatch)
    mesh = encmesh.build_scene_mesh(
        _scene(("COALNE", LineString([(0.0, 0.0), (1.0, 0.0)]))),
        stroke_pixels=1.0,
        projection=_projection,
        area_outlines=False,
    )

    assert calls == []
    assert mesh.line_segments.segment_count == 1


def test_availability_alone_turns_the_slice_on(monkeypatch, gpu_lines_available):
    """The default flips on once the viewport reports a native item."""
    monkeypatch.delenv(encmesh.GPU_LINES_ENV, raising=False)
    calls = _record_stroke_lines(monkeypatch)
    mesh = encmesh.build_scene_mesh(
        _scene(("COALNE", LineString([(0.0, 0.0), (1.0, 0.0)]))),
        stroke_pixels=1.0,
        projection=_projection,
        area_outlines=False,
    )

    assert encmesh.gpu_lines_enabled() is True
    assert calls == []
    assert mesh.line_segments.segment_count == 1


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "Off"])
def test_explicit_opt_out_disables_an_available_item(
    monkeypatch, gpu_lines_available, value
):
    monkeypatch.setenv(encmesh.GPU_LINES_ENV, value)
    calls = _record_stroke_lines(monkeypatch)
    mesh = encmesh.build_scene_mesh(
        _scene(("COALNE", LineString([(0.0, 0.0), (1.0, 0.0)]))),
        stroke_pixels=1.0,
        projection=_projection,
        area_outlines=False,
    )

    assert encmesh.gpu_lines_enabled() is False
    assert len(calls) == 1
    assert mesh.line_segments.is_empty


def test_env_force_on_cannot_bypass_a_missing_item(monkeypatch):
    monkeypatch.setenv(encmesh.GPU_LINES_ENV, "1")
    encmesh.set_gpu_lines_available(False)
    calls = _record_stroke_lines(monkeypatch)
    mesh = encmesh.build_scene_mesh(
        _scene(("COALNE", LineString([(0.0, 0.0), (1.0, 0.0)]))),
        stroke_pixels=1.0,
        projection=_projection,
        area_outlines=False,
    )

    assert encmesh.gpu_lines_enabled() is False
    assert len(calls) == 1
    assert mesh.line_segments.is_empty
    assert [part.kind for part in mesh.parts] == [encmesh.LINE]


def test_source_unit_stroke_keeps_the_cpu_path(monkeypatch):
    # A caller with no pixel contract cannot use LINE_STYLES widths, so the
    # payload stays empty and nothing is silently dropped from the mesh.
    calls = _record_stroke_lines(monkeypatch)
    mesh = encmesh.build_scene_mesh(
        _scene(("COALNE", LineString([(0.0, 0.0), (1.0, 0.0)]))),
        stroke_width=0.1,
        area_outlines=False,
        gpu_lines=True,
    )

    assert len(calls) == 1
    assert mesh.line_segments.is_empty


def test_two_gpu_features_concatenate_in_feature_order():
    mesh = encmesh.build_scene_mesh(
        _scene(
            ("COALNE", LineString([(0.0, 0.0), (1.0, 0.0)])),
            ("DEPCNT", LineString([(2.0, 2.0), (3.0, 3.0), (4.0, 4.0)])),
        ),
        stroke_pixels=1.0,
        projection=_projection,
        area_outlines=False,
        gpu_lines=True,
    )

    assert mesh.line_segments.segment_count == 3
    np.testing.assert_allclose(mesh.line_segments.widths, [1.5, 0.5, 0.5])


# --- it travels with the cell and the view --------------------------------


def test_view_scene_carries_the_payload_by_default():
    scene = encview.ViewScene(
        positions=np.empty((0, 2)), parts=(), cells=(), unsupported=(), skipped=()
    )

    assert scene.line_segments is encmesh.EMPTY_LINE_SEGMENTS
    assert scene.line_segments.is_empty


def test_concatenation_preserves_endpoints_widths_colours_and_dashes():
    first = encmesh.gpu_line_segments(
        LineString([(0, 0), (1, 0)]), (1, 2, 3, 4), 1.5, dash=(3.0, 1.0)
    )
    second = encmesh.gpu_line_segments(LineString([(2, 2), (3, 3)]), (5, 6, 7, 8), 0.5)
    joined = encmesh.concat_line_segments([first, encmesh.EMPTY_LINE_SEGMENTS, second])

    assert joined.segment_count == 2
    np.testing.assert_allclose(joined.endpoints[0], [0.0, 0.0, 1.0, 0.0])
    np.testing.assert_allclose(joined.endpoints[1], [2.0, 2.0, 3.0, 3.0])
    np.testing.assert_allclose(joined.widths, [1.5, 0.5])
    np.testing.assert_array_equal(joined.colours[0], [1, 2, 3, 4])
    np.testing.assert_array_equal(joined.colours[1], [5, 6, 7, 8])
    # The dashed feature keeps its pair; the solid one keeps its zeros.
    np.testing.assert_allclose(joined.dash_on, [3.0, 0.0])
    np.testing.assert_allclose(joined.dash_off, [1.0, 0.0])
    # Each chunk's chain restarts at zero, and concatenation keeps it.
    np.testing.assert_allclose(joined.phase, [0.0, 0.0])
