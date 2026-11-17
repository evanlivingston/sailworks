"""Pure-Python tests for tools/measure_enc_phase1.py (issue #213, Phase 1).

The harness is loaded by path so the tests exercise the same module the CLI
runs.  Nothing here imports Qt or cairo; the two coverage helpers take numpy
arrays and are covered with small ones.
"""

import importlib.util
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HARNESS = _load("measure_enc_phase1", ROOT / "tools" / "measure_enc_phase1.py")


class _ReadbackFrame:
    """Minimal stand-in for a readback frame: only shape and bytes."""

    def __init__(self, shape, data):
        self.shape = shape
        self.data = data

    def tobytes(self):
        return self.data


def test_frame_digest_is_shape_tagged_and_content_sensitive():
    assert HARNESS.frame_digest(_ReadbackFrame((2, 3), b"abc")) == HARNESS.frame_digest(
        _ReadbackFrame((2, 3), b"abc")
    )
    assert HARNESS.frame_digest(_ReadbackFrame((2, 3), b"abc")) != HARNESS.frame_digest(
        _ReadbackFrame((2, 3), b"abd")
    )
    assert HARNESS.frame_digest(_ReadbackFrame((2, 3), b"abc")).startswith("2x3:")
    assert HARNESS.frame_digest(_ReadbackFrame((3, 2), b"abc")).startswith("3x2:")


def test_settled_frames_accepts_only_identical_consecutive_frames():
    assert HARNESS.settled_frames([_ReadbackFrame((2, 3), b"abc")] * 2)
    assert HARNESS.settled_frames(
        [
            _ReadbackFrame((2, 3), b"abc"),
            _ReadbackFrame((2, 3), b"abc"),
            _ReadbackFrame((2, 3), b"abc"),
        ]
    )


def test_settled_frames_rejects_changed_content_or_shape():
    assert not HARNESS.settled_frames(
        [_ReadbackFrame((2, 3), b"abc"), _ReadbackFrame((2, 3), b"abd")]
    )
    assert not HARNESS.settled_frames(
        [_ReadbackFrame((2, 3), b"abc"), _ReadbackFrame((3, 2), b"abc")]
    )
    assert not HARNESS.settled_frames([_ReadbackFrame((2, 3), b"abc")])
    assert not HARNESS.settled_frames([])


BASE = {"lat": 48.1125, "lon": -122.7375, "zoom": 14.236}
OTHER = {"lat": 37.8375, "lon": -122.4375, "zoom": 14.236}


def test_sequence_has_the_fixed_step_order():
    sequence = HARNESS.build_sequence(BASE, OTHER)
    assert [entry["step"] for entry in sequence] == [
        "cold",
        "warm",
        "pan",
        "pan_back",
        "zoom_out",
        "zoom_back",
        "switch_cell",
        "return",
    ]
    assert [entry["step"] for entry in sequence] == list(HARNESS.STEPS)


def test_sequence_cameras_are_the_documented_offsets():
    sequence = {
        entry["step"]: entry["camera"] for entry in HARNESS.build_sequence(BASE, OTHER)
    }
    assert sequence["cold"] == BASE
    assert sequence["warm"] == BASE
    assert sequence["pan"]["lat"] == BASE["lat"] + HARNESS.PAN_DEGREES
    assert sequence["pan"]["lon"] == BASE["lon"]
    assert sequence["pan"]["zoom"] == BASE["zoom"]
    assert sequence["pan_back"] == BASE
    assert sequence["zoom_out"]["zoom"] == BASE["zoom"] - HARNESS.ZOOM_OUT_STEPS
    assert sequence["zoom_back"] == BASE
    assert sequence["switch_cell"] == OTHER
    assert sequence["return"] == BASE


def test_sequence_is_deterministic_and_does_not_mutate_inputs():
    first = HARNESS.build_sequence(BASE, OTHER)
    second = HARNESS.build_sequence(BASE, OTHER)
    assert first == second
    assert BASE == {"lat": 48.1125, "lon": -122.7375, "zoom": 14.236}
    assert OTHER == {"lat": 37.8375, "lon": -122.4375, "zoom": 14.236}


def test_session_keeps_the_viewport_camera_until_the_layer_requests_it():
    """The step camera must expose ``zoom``; ChartCamera only has private fields.

    The gate builds each scenario on a ViewportCamera and hands it straight to
    ``set_camera`` and ``layer.request``, which wrap it in a ChartCamera
    internally.  Constructing the canvas here regressed to
    ``'ChartCamera' object has no attribute 'zoom'``.
    """
    source = (ROOT / "tools" / "measure_enc_phase1.py").read_text()
    assert "ChartCamera(" not in source
    assert "widget.set_camera(camera)" in source
    assert "layer.request(camera, replace=True)" in source


def test_summary_reports_p50_p95_and_max():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    result = HARNESS.summary(values)
    assert result["count"] == 5
    assert result["p50"] == 30.0
    assert result["p95"] == 48.0
    assert result["max"] == 50.0


def test_summary_skips_missing_samples_and_handles_empty_input():
    assert HARNESS.summary([]) == {"count": 0}
    assert HARNESS.summary([None, None]) == {"count": 0}
    assert HARNESS.summary([5.0, None, 15.0])["count"] == 2


def test_reproducible_accepts_a_narrow_band_and_identical_selection():
    assert HARNESS.reproducible([0.99, 1.01, 1.0], [("A", "B"), ("B", "A")])


def test_reproducible_rejects_a_wide_band():
    assert not HARNESS.reproducible([0.90, 1.05], [("A",), ("A",)])


def test_reproducible_rejects_a_changed_selection():
    assert not HARNESS.reproducible([1.0, 1.0], [("A",), ("B",)])


def test_reproducible_without_coverage_still_requires_one_selection():
    assert HARNESS.reproducible([], [("A",), ("A",)])
    assert not HARNESS.reproducible([], [("A",), ("B",)])


def test_summarise_repeats_groups_by_density_and_step():
    samples = [
        {
            "density": "sparse",
            "step": "cold",
            "ready": True,
            "coverage": 0.99,
            "selected": ["A"],
            "interaction_ms": 10.0,
            "ready_ms": 100.0,
            "composition_ms": 50.0,
            "queue_ms": 1.0,
            "request_to_publish_ms": 60.0,
            "publish_to_adopt_ms": 5.0,
        },
        {
            "density": "sparse",
            "step": "cold",
            "ready": True,
            "coverage": 1.01,
            "selected": ["A"],
            "interaction_ms": 30.0,
            "ready_ms": 120.0,
            "composition_ms": 70.0,
            "queue_ms": 3.0,
            "request_to_publish_ms": 80.0,
            "publish_to_adopt_ms": 9.0,
        },
        {
            "density": "dense",
            "step": "pan",
            "ready": False,
            "coverage": None,
            "selected": ["B"],
            "interaction_ms": None,
        },
    ]
    summary = HARNESS.summarise_repeats(samples)
    assert set(summary) == {"sparse/cold", "dense/pan"}
    sparse = summary["sparse/cold"]
    assert sparse["count"] == 2
    assert sparse["ready"] is True
    assert sparse["interaction_ms"]["p50"] == 20.0
    assert sparse["composition_ms"]["p95"] == 69.0
    assert sparse["coverage"] == {"min": 0.99, "max": 1.01, "span": 0.02}
    assert sparse["selections"] == [["A"], ["A"]]
    assert sparse["reproducible"] is True
    dense = summary["dense/pan"]
    assert dense["ready"] is False
    assert dense["coverage"]["span"] is None
    assert dense["interaction_ms"] == {"count": 0}


def test_empty_report_matches_the_output_schema():
    report = HARNESS.empty_report()
    assert HARNESS.validate_report(report) == []
    assert report["tool"] == "tools/measure_enc_phase1.py"
    assert report["issue"] == 213
    assert report["phase"] == 1
    assert set(report["checks"]) == set(HARNESS.CHECK_NAMES)
    assert report["complete"] is False
    assert report["dpr_environment"]["QT_SCALE_FACTOR"] == "1"
    assert any("not presentation FPS" in note for note in report["notes"])


def test_validate_report_flags_a_missing_key_and_an_incomplete_sample():
    report = HARNESS.empty_report()
    del report["fixture_sha256"]
    report["sequence"] = [{"step": "cold"}]
    problems = HARNESS.validate_report(report)
    assert "missing key: fixture_sha256" in problems
    assert "sequence[0] missing density" in problems
    assert "sequence[0] missing coverage" in problems


class _Frame:
    """Shape-only stand-in for a numpy frame; slicing crops, as numpy does."""

    def __init__(self, height, width, channels=4):
        self.shape = (height, width, channels)

    def __getitem__(self, key):
        rows, cols = key
        height = len(range(*rows.indices(self.shape[0])))
        width = len(range(*cols.indices(self.shape[1])))
        return _Frame(height, width, self.shape[2])


def test_align_shapes_crops_a_taller_readback_to_the_reference():
    native = _Frame(744, 710)
    ref_on = _Frame(743, 710)
    ref_off = _Frame(743, 710)
    cropped_native, cropped_on, cropped_off, note = HARNESS._align_shapes(
        native, ref_on, ref_off
    )
    assert (
        cropped_native.shape == cropped_on.shape == cropped_off.shape == (743, 710, 4)
    )
    assert note["native"] == [744, 710, 4]
    assert note["reference"] == [743, 710, 4]
    assert note["cropped"] is True


def test_align_shapes_leaves_matching_frames_untouched():
    native = _Frame(743, 710)
    ref_on = _Frame(743, 710)
    ref_off = _Frame(743, 710)
    result = HARNESS._align_shapes(native, ref_on, ref_off)
    assert all(frame.shape == (743, 710, 4) for frame in result[:3])
    assert result[3]["cropped"] is False


def test_align_shapes_aligns_a_narrower_reference_off():
    native = _Frame(744, 712)
    ref_on = _Frame(743, 710)
    ref_off = _Frame(743, 708)
    result = HARNESS._align_shapes(native, ref_on, ref_off)
    assert all(frame.shape == (743, 708, 4) for frame in result[:3])
    assert result[3]["reference_off"] == [743, 708, 4]
    assert result[3]["cropped"] is True


def test_validate_report_accepts_a_fully_populated_sample():
    report = HARNESS.empty_report()
    report["sequence"] = [{key: None for key in HARNESS.SAMPLE_KEYS}]
    assert HARNESS.validate_report(report) == []


# --------------------------------------------------------------------------
# A settled pair is not proof the demand was painted: the frame it settled to
# may be the one already on screen before the camera changed.
# --------------------------------------------------------------------------

STALE = _ReadbackFrame((2, 3), b"stale")
NEW = _ReadbackFrame((2, 3), b"new")
STALE_DIGEST = HARNESS.frame_digest(STALE)


def test_settled_after_demand_accepts_a_pair_that_post_dates_the_demand():
    assert HARNESS.settled_after_demand((NEW, NEW), STALE_DIGEST)
    assert HARNESS.settled_after_demand((STALE, NEW, NEW), STALE_DIGEST)


def test_settled_after_demand_rejects_a_pair_identical_to_the_pre_demand_frame():
    # Both grabs are byte-identical *and* identical to the frame painted
    # before the demand: the stale frame that scored 0.2525 coverage at DPR 2.
    assert not HARNESS.settled_after_demand((STALE, STALE), STALE_DIGEST)


def test_settled_after_demand_still_requires_an_actual_pair():
    assert not HARNESS.settled_after_demand((NEW,), STALE_DIGEST)
    assert not HARNESS.settled_after_demand((NEW, _ReadbackFrame((2, 3), b"x")), None)


def test_settled_after_demand_without_a_baseline_uses_the_pair_alone():
    assert HARNESS.settled_after_demand((STALE, STALE), None)


def test_settled_after_demand_allows_identical_only_when_asked():
    # ``warm`` re-issues the ``cold`` camera: identical pixels are correct
    # there, and the caller proves freshness through the generation instead.
    assert HARNESS.settled_after_demand(
        (STALE, STALE), STALE_DIGEST, allow_identical=True
    )
    assert not HARNESS.settled_after_demand((STALE, STALE), STALE_DIGEST)


def test_same_view_detects_a_re_affirmed_camera():
    camera = (48.0, -122.0, 14.0)
    assert HARNESS.same_view(camera, tuple(camera))
    assert not HARNESS.same_view(camera, (48.0, -122.0, 12.5))
    assert not HARNESS.same_view(None, camera)
    assert not HARNESS.same_view(camera, None)


class _FakeWidget:
    """Only the surfaces ``_capture`` reads: generation and camera."""

    def __init__(self, generation=7, camera=None):
        self._chart_visible_generation = generation
        self.camera = camera


class _Camera:
    """A camera stand-in with the three fields ``_camera_token`` reads."""

    def __init__(self, token):
        self.center_lat, self.center_lon, self.zoom = token


class _FakeCheckEnc:
    """Replays a fixed frame sequence and counts the pumps."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.pumps = 0
        self.grabs = 0

    def _grab(self, _widget):
        self.grabs += 1
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]

    def _pump(self, _milliseconds):
        self.pumps += 1


def test_capture_refuses_to_settle_on_the_pre_demand_frame():
    tool = _FakeCheckEnc([STALE] * 8)
    frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(),
        pre_demand_digest=STALE_DIGEST,
        max_attempts=5,
        pre_pump_ms=HARNESS.CAPTURE_PUMP_MS,
    )
    assert stable is False
    assert settle["stable"] is False
    assert settle["pre_demand_digest"] == STALE_DIGEST
    assert settle["changed_after_demand"] is False
    assert settle["attempts"] == 5
    assert settle["timeouts"] == 1
    assert set(settle["digests"]) == {STALE_DIGEST}
    assert "never differed" in settle["timeout_reason"]
    assert frame is STALE
    assert tool.pumps == 1 + 4  # one pre-demand pump plus one per retry


def test_capture_accepts_a_pair_that_differs_from_the_pre_demand_frame():
    tool = _FakeCheckEnc([STALE, STALE, NEW, NEW])
    frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(),
        pre_demand_digest=STALE_DIGEST,
        max_attempts=6,
        pre_pump_ms=HARNESS.CAPTURE_PUMP_MS,
    )
    assert stable is True
    assert settle["changed_after_demand"] is True
    assert (
        settle["digests"]
        == [STALE_DIGEST, STALE_DIGEST] + [HARNESS.frame_digest(NEW)] * 2
    )
    assert "timeout_reason" not in settle
    assert frame is NEW


def test_capture_never_adopts_a_generation_it_was_not_asked_for():
    tool = _FakeCheckEnc([NEW] * 8)
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=9),
        pre_demand_digest=STALE_DIGEST,
        expect_generation=7,
        max_attempts=4,
    )
    assert stable is False
    assert settle["restarts"] >= 1
    assert settle["changed_after_demand"] is True


def test_capture_accepts_a_re_affirmed_camera_once_the_generation_advances():
    # ``warm`` re-issues ``cold``'s camera, so its pixels never differ from the
    # pre-demand frame; the advanced generation is what proves freshness.
    camera = (48.1125, -122.7375, 14.236)
    tool = _FakeCheckEnc([STALE, STALE])
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=8, camera=_Camera(camera)),
        pre_demand_digest=STALE_DIGEST,
        pre_demand_camera=camera,
        pre_demand_generation=7,
        expect_generation=8,
        max_attempts=4,
    )
    assert stable is True
    assert settle["reasserts_view"] is True
    assert settle["changed_after_demand"] is True
    assert settle["digests"] == [STALE_DIGEST, STALE_DIGEST]


def test_capture_rejects_a_re_affirmed_camera_that_was_never_republished():
    camera = (48.1125, -122.7375, 14.236)
    tool = _FakeCheckEnc([STALE] * 6)
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=7, camera=_Camera(camera)),
        pre_demand_digest=STALE_DIGEST,
        pre_demand_camera=camera,
        pre_demand_generation=7,
        expect_generation=7,
        max_attempts=3,
    )
    assert stable is False
    assert settle["reasserts_view"] is True
    assert settle["changed_after_demand"] is False
    assert settle["generation_advanced"] is False
    assert "never advanced" in settle["timeout_reason"]


def test_capture_rejects_a_new_frame_when_the_chart_never_republished():
    # The DPR 2 stale frame: the underlay repainted after the camera moved, so
    # the digest changed, but the chart generation never advanced.  That frame
    # scored 0.2525 coverage, so different pixels are not proof on their own.
    tool = _FakeCheckEnc([NEW, NEW])
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=16),
        pre_demand_digest=STALE_DIGEST,
        pre_demand_generation=16,
        expect_generation=16,
        max_attempts=3,
    )
    assert stable is False
    assert settle["changed_after_demand"] is True
    assert settle["generation_advanced"] is False
    assert "never advanced" in settle["timeout_reason"]


def test_capture_compares_the_demand_camera_not_the_readback_token():
    # ``warm`` re-issues ``cold``'s camera.  The widget normalises the token it
    # reads back after readiness, so comparing that token with the pre-demand
    # camera missed the re-affirmation and rejected identical pixels forever.
    camera = (48.1125, -122.7375, 14.236)
    normalised = (48.112500000000004, -122.73749999999999, 14.235999999999999)
    tool = _FakeCheckEnc([STALE, STALE])
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=8, camera=_Camera(normalised)),
        pre_demand_digest=STALE_DIGEST,
        pre_demand_camera=camera,
        pre_demand_generation=7,
        expect_generation=8,
        expect_camera=normalised,
        demand_camera=camera,
        max_attempts=4,
    )
    assert stable is True
    assert settle["reasserts_view"] is True


def test_capture_falls_back_after_a_bounded_generation_wait_for_a_settled_camera():
    # ``pan_back`` returns to a camera already on screen: the generation stays
    # at 21, so waiting the full capture bound proves nothing.  Fresh pixels on
    # a camera this run already settled settle instead.
    camera = (48.1125, -122.7375, 14.236)
    tool = _FakeCheckEnc([NEW] * 6)
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=21, camera=_Camera(camera)),
        pre_demand_digest=STALE_DIGEST,
        pre_demand_camera=camera,
        pre_demand_generation=21,
        expect_generation=21,
        expect_camera=camera,
        demand_camera=camera,
        revisits_settled_camera=True,
        max_attempts=6,
        generation_wait_attempts=2,
    )
    assert stable is True
    assert settle["generation_advanced"] is False
    assert settle["generation_fallback"] is True
    assert settle["attempts"] <= 4


def test_capture_keeps_waiting_for_the_generation_on_a_never_settled_camera():
    # A camera this run has not painted yet: its different-looking frame can
    # still be the pre-demand composition (the DPR 2 frame that scored 0.2525
    # coverage), so the bounded wait must not relax into the pixel rule.
    camera = (48.1125, -122.7375, 14.236)
    tool = _FakeCheckEnc([NEW] * 6)
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=21, camera=_Camera(camera)),
        pre_demand_digest=STALE_DIGEST,
        pre_demand_camera=camera,
        pre_demand_generation=21,
        expect_generation=21,
        expect_camera=camera,
        demand_camera=camera,
        max_attempts=4,
        generation_wait_attempts=2,
    )
    assert stable is False
    assert settle["generation_advanced"] is False
    assert settle["generation_fallback"] is False
    assert "never advanced" in settle["timeout_reason"]


def test_capture_accepts_identical_pixels_when_the_demand_revisits_a_settled_camera():
    # The real ``warm``: the widget's readback token drifted from both the
    # pre-demand camera and the demand token, so the drift-free spec comparison
    # is what recognises the re-affirmed view.  Its last frames are byte-equal
    # to the pre-demand frame, and the generation is the only proof.
    camera = (48.1125, -122.7375, 14.236)
    drifted = (48.1125000001, -122.7374999999, 14.2365000001)
    tool = _FakeCheckEnc([NEW, STALE, STALE, STALE])
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=19, camera=_Camera(drifted)),
        pre_demand_digest=STALE_DIGEST,
        pre_demand_camera=camera,
        pre_demand_generation=18,
        expect_generation=19,
        expect_camera=drifted,
        demand_camera=camera,
        revisits_settled_camera=True,
        max_attempts=5,
    )
    assert stable is True
    assert settle["reasserts_view"] is True
    assert settle["generation_advanced"] is True
    assert settle["digests"][-2:] == [STALE_DIGEST, STALE_DIGEST]


def test_capture_rejects_identical_pixels_for_a_changed_camera():
    # The demand moved the camera and the generation advanced, but the pixels
    # still equal the frame grabbed before the demand: that is the stale
    # pre-demand frame (DPR 2, 0.2525 coverage), not a re-affirmed view.
    tool = _FakeCheckEnc([STALE] * 5)
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=16, camera=_Camera((48.5, -122.5, 13.0))),
        pre_demand_digest=STALE_DIGEST,
        pre_demand_camera=(48.0, -122.0, 14.0),
        pre_demand_generation=15,
        expect_generation=16,
        expect_camera=(48.5, -122.5, 13.0),
        demand_camera=(48.5, -122.5, 13.0),
        max_attempts=4,
    )
    assert stable is False
    assert settle["reasserts_view"] is False
    assert settle["changed_after_demand"] is False
    assert "never differed" in settle["timeout_reason"]


def test_capture_never_falls_back_to_an_identical_frame():
    camera = (48.1125, -122.7375, 14.236)
    tool = _FakeCheckEnc([STALE] * 6)
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=21, camera=_Camera(camera)),
        pre_demand_digest=STALE_DIGEST,
        pre_demand_camera=camera,
        pre_demand_generation=21,
        expect_generation=21,
        expect_camera=camera,
        demand_camera=camera,
        revisits_settled_camera=True,
        max_attempts=4,
        generation_wait_attempts=2,
    )
    assert stable is False
    assert settle["changed_after_demand"] is False
    assert settle["generation_fallback"] is False


def test_capture_accepts_an_already_settled_composition_without_a_republish():
    # The opening ``cold`` of a later repeat: the demanded camera is already
    # the composition on screen, this run settled it before, and the scene
    # layer republishes no new generation.  Tolerance-identical pixels are that
    # finished frame, so the row settles without a generation advance and
    # without needing the differing-pixel fallback.
    import numpy as np

    camera = (48.1125, -122.7375, 14.236)
    frame = np.zeros((6, 6, 3), dtype=np.uint8)
    tool = _FakeCheckEnc([frame] * 6)
    _frame, stable, settle = HARNESS._capture(
        tool,
        _FakeWidget(generation=21, camera=_Camera(camera)),
        pre_demand_digest=HARNESS.frame_digest(frame),
        pre_demand_camera=camera,
        pre_demand_generation=21,
        expect_generation=21,
        expect_camera=camera,
        demand_camera=camera,
        revisits_settled_camera=True,
        max_attempts=4,
        generation_wait_attempts=2,
        settle_tolerance=2,
        pre_demand_frame=frame,
    )
    assert stable is True
    assert settle["reasserts_view"] is True
    assert settle["generation_advanced"] is False
    assert settle["generation_fallback"] is False
    assert settle["stable"] is True


def test_dump_directory_is_unique_per_step_density_and_repeat():
    first = HARNESS.dump_directory("/tmp/frames", "dense", "zoom_out", 0)
    assert first == Path("/tmp/frames/zoom_out_dense_r0")
    # ``zoom_out`` occurs under both densities: the dump path must not collide.
    assert first != HARNESS.dump_directory("/tmp/frames", "sparse", "zoom_out", 0)
    assert first != HARNESS.dump_directory("/tmp/frames", "dense", "zoom_out", 1)


def test_frame_dumping_is_opt_in_by_default():
    parameter = inspect.signature(HARNESS.run).parameters["dump_frames"]
    assert parameter.default is None
    assert inspect.signature(HARNESS._session).parameters["dump_frames"].default is None
    assert inspect.signature(HARNESS._measure).parameters["dump_frames"].default is None


def test_main_parses_the_dump_frames_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_run(
        snapshot,
        scenarios,
        repeats,
        requested_dpr,
        experience_only,
        dump_frames,
        enc_scene=False,
        counters_only=False,
        capture_backend="qt",
    ):
        captured["dump_frames"] = dump_frames
        captured["experience_only"] = experience_only
        captured["enc_scene"] = enc_scene
        return HARNESS.empty_report()

    monkeypatch.setattr(HARNESS, "run", fake_run)
    HARNESS.main(
        [
            "--snapshot",
            str(tmp_path),
            "--output",
            str(tmp_path / "report.json"),
            "--dump-frames",
            str(tmp_path / "frames"),
        ]
    )
    assert captured["dump_frames"] == tmp_path / "frames"
    assert captured["experience_only"] is False
    assert captured["enc_scene"] is False


def test_main_parses_the_enc_scene_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_run(
        snapshot,
        scenarios,
        repeats,
        requested_dpr,
        experience_only,
        dump_frames,
        enc_scene=False,
        counters_only=False,
        capture_backend="qt",
    ):
        captured["enc_scene"] = enc_scene
        return HARNESS.empty_report()

    monkeypatch.setattr(HARNESS, "run", fake_run)
    argv = ["--snapshot", str(tmp_path), "--output", str(tmp_path / "r.json")]
    HARNESS.main(argv)
    assert captured["enc_scene"] is False

    HARNESS.main(argv + ["--enc-scene"])
    assert captured["enc_scene"] is True


def test_enc_scene_probe_is_none_without_a_scene_layer():
    assert HARNESS._enc_scene_counters(object()) is None


def test_enc_scene_probe_reads_layer_and_item_counters():
    class _Layer:
        def observations(self):
            return {
                "state": "ready",
                "generation": 4,
                "counters": {
                    "requests": 2,
                    "reuses": 1,
                    "assemblies": 2,
                    "publications": 2,
                },
                "mesh_triangulations": 9,
                "mesh_hits": 3,
                "mesh_misses": 1,
                "scene_hits": 5,
                "scene_misses": 1,
                "store_hits": 6,
                "store_misses": 1,
            }

    class _Item:
        upload_count = 1
        matrix_update_count = 8
        static_mode = True
        visible_cells = (True, False)
        generation = 4

    class _Widget:
        _enc_scene_model = _Layer()
        _enc_scene_item = _Item()

    counters = HARNESS._enc_scene_counters(_Widget())
    assert counters["mesh_triangulations"] == 9
    assert counters["mesh_hits"] == 3
    assert counters["scene_misses"] == 1
    assert counters["store_misses"] == 1
    assert counters["layer_state"] == "ready"
    assert counters["layer_requests"] == 2
    assert counters["upload_count"] == 1
    assert counters["matrix_update_count"] == 8
    assert counters["static_mode"] is True
    assert counters["visible_cells"] == [True, False]
    assert counters["item_generation"] == 4


def test_enc_scene_probe_tolerates_a_layer_without_an_item():
    class _Layer:
        def observations(self):
            return {"state": "loading", "counters": {}, "generation": 1}

    class _Widget:
        _enc_scene_model = _Layer()

    counters = HARNESS._enc_scene_counters(_Widget())
    assert counters["item_present"] is False
    assert counters["upload_count"] is None
    assert counters["mesh_triangulations"] is None


def test_enc_scene_mode_is_off_by_default():
    for name in ("run", "_measure", "_session"):
        parameter = inspect.signature(getattr(HARNESS, name)).parameters["enc_scene"]
        assert parameter.default is False
    assert HARNESS.empty_report()["enc_scene_mode"] is False


# --------------------------------------------------------------------------
# The fidelity oracle's ENC extent: the application fills the viewport, the
# reference leaves everything outside the cells it drew transparent, so the
# coverage the band judges is taken over the reference's own ink only.
# --------------------------------------------------------------------------


def _reference_pair(rows, cols, ink_rows, ink_cols):
    np = pytest.importorskip("numpy")
    ref_on = np.zeros((rows, cols, 4), dtype="uint8")
    ref_off = np.zeros((rows, cols, 4), dtype="uint8")
    ref_on[ink_rows, ink_cols, 3] = 255
    return ref_on, ref_off


def test_enc_extent_mask_marks_the_reference_ink_and_grows_it():
    ref_on, ref_off = _reference_pair(8, 8, slice(2, 5), slice(3, 6))
    mask, extent = HARNESS.enc_extent_mask(ref_on, ref_off, epsilon=0.5, dilate=1)
    assert mask.shape == (8, 8)
    assert mask.dtype == bool
    assert int(mask.sum()) == 25
    assert extent["ink_pixels"] == 9
    assert extent["mask_pixels"] == 25
    assert extent["frame_pixels"] == 64
    assert extent["covered_fraction"] == 0.3906
    assert extent["bbox"] == [1, 5, 2, 6]
    assert extent["dilate_px"] == 1
    assert extent["epsilon"] == 0.5
    assert extent["used_full_frame"] is False


def test_enc_extent_mask_defaults_to_the_configured_dilation():
    ref_on, ref_off = _reference_pair(8, 8, 4, 4)
    mask, extent = HARNESS.enc_extent_mask(ref_on, ref_off, epsilon=0.5)
    assert extent["dilate_px"] == HARNESS.ENC_MASK_DILATE_PX
    assert extent["bbox"] == [1, 7, 1, 7]
    assert int(mask.sum()) == 49
    assert extent["covered_fraction"] == 0.7656


def test_enc_extent_mask_is_empty_when_the_references_agree():
    ref_on, ref_off = _reference_pair(6, 5, slice(1, 3), slice(1, 2))
    ref_off[:, :, :] = ref_on
    mask, extent = HARNESS.enc_extent_mask(ref_on, ref_off, epsilon=0.5)
    assert not mask.any()
    assert extent["ink_pixels"] == 0
    assert extent["mask_pixels"] == 0
    assert extent["covered_fraction"] == 0.0
    assert extent["bbox"] is None


def test_enc_extent_mask_uses_the_gate_epsilon():
    np = pytest.importorskip("numpy")
    ref_on = np.zeros((4, 4, 4), dtype="uint8")
    ref_off = np.zeros((4, 4, 4), dtype="uint8")
    ref_on[1, 1, 3] = 2
    assert not HARNESS.enc_extent_mask(ref_on, ref_off, epsilon=2.0, dilate=0)[0].any()
    assert HARNESS.enc_extent_mask(ref_on, ref_off, epsilon=1.0, dilate=0)[0].any()


def test_enc_extent_mask_does_not_wrap_around_the_frame_edge():
    ref_on, ref_off = _reference_pair(8, 8, 0, 0)
    mask, extent = HARNESS.enc_extent_mask(ref_on, ref_off, epsilon=0.5, dilate=2)
    assert int(mask.sum()) == 9
    assert extent["bbox"] == [0, 2, 0, 2]
    assert not mask[6:, :].any()
    assert not mask[:, 6:].any()


class _SummingFidelity:
    """The gate's ink sums, so the masking can be checked without cairo."""

    def __init__(self):
        self.shapes = []

    def _fidelity(self, native, ref_on, ref_off):
        np = pytest.importorskip("numpy")
        self.shapes.append(tuple(native.shape))
        off = ref_off.astype("int16")
        ref_ink = np.abs(ref_on.astype("int16") - off).sum()
        native_ink = np.abs(native.astype("int16") - off).sum()
        return {
            "coverage": float(native_ink / ref_ink) if ref_ink else 0.0,
            "mae_255": float(np.abs(native.astype("int16") - ref_on).mean()),
            "recall": 1.0,
        }


def test_fidelity_on_mask_excludes_ink_outside_the_enc_extent():
    np = pytest.importorskip("numpy")
    ref_on = np.zeros((4, 4, 4), dtype="uint8")
    ref_off = np.zeros((4, 4, 4), dtype="uint8")
    ref_on[1, 1, 3] = 255
    mask = np.zeros((4, 4), dtype=bool)
    mask[0:3, 0:3] = True
    # Inside the extent the native chart is identical to the reference; outside
    # it the application correctly fills the viewport with flat background.
    native = np.where(mask[:, :, None], ref_on, 180).astype("uint8")
    full = _SummingFidelity()._fidelity(native, ref_on, ref_off)
    assert full["coverage"] > 1.5
    stub = _SummingFidelity()
    masked = HARNESS._fidelity_on_mask(stub, native, ref_on, ref_off, mask)
    assert stub.shapes == [(1, 9, 4)]
    assert masked["coverage"] == 1.0
    assert masked["mae_255"] == 0.0


def test_fidelity_on_mask_rejects_a_mask_that_is_not_the_frame():
    np = pytest.importorskip("numpy")
    frame = np.zeros((4, 4, 4), dtype="uint8")
    with pytest.raises(ValueError):
        HARNESS._fidelity_on_mask(
            _SummingFidelity(), frame, frame, frame, np.zeros(16, dtype=bool)
        )
    with pytest.raises(ValueError):
        HARNESS._fidelity_on_mask(
            _SummingFidelity(), frame, frame, frame, np.zeros((5, 4), dtype=bool)
        )


def test_fidelity_on_mask_returns_none_for_an_empty_extent():
    np = pytest.importorskip("numpy")
    frame = np.zeros((4, 4, 4), dtype="uint8")
    assert (
        HARNESS._fidelity_on_mask(
            _SummingFidelity(), frame, frame, frame, np.zeros((4, 4), dtype=bool)
        )
        is None
    )


def test_coverage_band_uses_the_enc_extent_value():
    """The band judges the masked coverage, not the full-frame one."""
    source = (ROOT / "tools" / "measure_enc_phase1.py").read_text()
    start = source.index('report["checks"]["coverage_band"]')
    block_start = source.rindex("measured_coverage = [", 0, start)
    window = source[block_start : start + 200]
    assert "coverage_enc_extent" in window
    assert "COVERAGE_BAND" in window


class _FlakyCheckEnc(_FakeCheckEnc):
    """Fails its first ``failures`` grabs, then replays the frame sequence.

    Models the DPR 2 run's readback path: the content-item grab stalls at
    ~34 MB per frame, and the stall may clear on the next frame.
    """

    def __init__(self, frames, failures):
        super().__init__(frames)
        self.failures = failures
        self.updates = 0

    def _grab(self, _widget):
        if self.failures > 0:
            self.failures -= 1
            self.grabs += 1
            raise RuntimeError("content item grab timed out after 5000 ms")
        return super()._grab(_widget)


class _UpdatingWidget(_FakeWidget):
    """A widget that counts the forced repaint a readback retry asks for."""

    def __init__(self, generation=7, camera=None):
        super().__init__(generation, camera)
        self.updates = 0

    def update(self):
        self.updates += 1


def test_readback_should_retry_only_a_lone_stall():
    assert HARNESS.readback_should_retry(0, 1) is True
    assert HARNESS.readback_should_retry(0, 0) is False
    assert HARNESS.readback_should_retry(HARNESS.READBACK_DEAD_AFTER, 1) is False
    assert HARNESS.readback_should_retry(HARNESS.READBACK_DEAD_AFTER - 1, 1) is True


def test_readback_retries_a_transient_grab_failure_after_a_forced_frame():
    tool = _FlakyCheckEnc([NEW], failures=1)
    widget = _UpdatingWidget()
    frame, attempts, error = HARNESS._readback(tool, widget)

    assert error is None
    assert frame is NEW
    assert attempts == 2
    assert widget.updates == 1
    assert tool.pumps == 1


def test_readback_gives_up_on_a_wedged_pipeline_without_retrying():
    tool = _FlakyCheckEnc([NEW], failures=99)
    frame, attempts, error = HARNESS._readback(
        tool, _UpdatingWidget(), misses=HARNESS.READBACK_DEAD_AFTER
    )

    assert frame is None
    assert attempts == 1
    assert isinstance(error, RuntimeError)
    assert "timed out" in str(error)


def test_readback_reports_the_last_error_once_retries_are_spent():
    tool = _FlakyCheckEnc([NEW], failures=99)
    widget = _UpdatingWidget()
    frame, attempts, error = HARNESS._readback(tool, widget)

    assert frame is None
    assert attempts == HARNESS.READBACK_RETRY_ATTEMPTS
    assert widget.updates == HARNESS.READBACK_RETRY_ATTEMPTS - 1
    assert isinstance(error, RuntimeError)


def test_capture_records_a_readback_that_needed_a_retry():
    tool = _FlakyCheckEnc([NEW, NEW], failures=1)
    frame, stable, settle = HARNESS._capture(
        tool,
        _UpdatingWidget(),
        pre_demand_digest=STALE_DIGEST,
        max_attempts=6,
        pre_pump_ms=HARNESS.CAPTURE_PUMP_MS,
    )

    assert stable is True
    assert settle["readback_recoveries"] == 1
    assert settle["readback_misses"] == 0
    assert settle["attempts"] == 2
    assert frame is NEW


def test_capture_raises_when_a_wedged_readback_never_recovers():
    tool = _FlakyCheckEnc([NEW], failures=99)
    with pytest.raises(RuntimeError):
        HARNESS._capture(
            tool,
            _UpdatingWidget(),
            pre_demand_digest=STALE_DIGEST,
            max_attempts=6,
            readback_misses=HARNESS.READBACK_DEAD_AFTER,
        )


# --------------------------------------------------------------------------
# Counter-only Gate 2 mode: no framebuffer is read; residency counters decide.
# --------------------------------------------------------------------------

_COUNTER_SEQUENCE = (
    "cold",
    "warm",
    "pan",
    "pan_back",
    "zoom_out",
    "zoom_back",
    "switch_cell",
    "return",
)


_COUNTER_CAMERAS = HARNESS.build_sequence(
    {"lat": 37.7, "lon": -122.5, "zoom": 9.0},
    {"lat": 47.5, "lon": -122.3, "zoom": 8.0},
)


def _counter_rows(counters, assembly=None, repeat=0):
    """One repeat of the fixed sequence with the given per-step overrides.

    The defaults mirror the diagnosed scene layer: one publication per new
    scene (``upload_count == layer_generation + 1``), one matrix update per
    camera change, and no triangle growth on a same-camera or revisit step.
    """
    generation = 1
    matrix = 0
    previous_camera = None
    rows = []
    for entry in _COUNTER_CAMERAS:
        camera = dict(entry["camera"])
        if previous_camera is not None and camera != previous_camera:
            generation += 1
            matrix += 1
        previous_camera = camera
        step_counters = {
            "mesh_triangulations": 12,
            "mesh_hits": 2,
            "scene_misses": 1,
            "store_misses": 2,
            "layer_generation": generation,
            "upload_count": generation + 1,
            "matrix_update_count": matrix,
        }
        step_counters.update(counters.get(entry["step"], counters.get("flat", {})))
        rows.append(
            {
                "step": entry["step"],
                "density": "dense",
                "repeat": repeat,
                "camera": camera,
                "ready": True,
                "enc_scene_counters": step_counters,
                "assembly_ms": (assembly or {}).get(entry["step"], 4.0),
            }
        )
    return rows


#: A single-step override that keeps one repeat consistent with the defaults.
_BASELINE_COUNTERS = {
    "mesh_triangulations": 12,
    "scene_misses": 1,
    "store_misses": 2,
}


def _steady_state_rows(assemblies, first_repeat=1):
    """The flat counter sequence once per measured (non-warm-up) repeat."""
    rows = []
    for offset, assembly in enumerate(assemblies):
        rows.extend(
            _counter_rows(
                {"flat": _BASELINE_COUNTERS},
                assembly,
                repeat=first_repeat + offset,
            )
        )
    return rows


def test_counters_only_flag_requires_enc_scene(monkeypatch, tmp_path):
    monkeypatch.setattr(
        HARNESS, "run", pytest.fail
    )  # validation must happen before any run
    with pytest.raises(SystemExit):
        HARNESS.main(
            [
                "--snapshot",
                str(tmp_path),
                "--output",
                str(tmp_path / "r.json"),
                "--counters-only",
            ]
        )


def test_main_parses_the_counters_only_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_run(
        snapshot,
        scenarios,
        repeats,
        requested_dpr,
        experience_only,
        dump_frames,
        enc_scene=False,
        counters_only=False,
        capture_backend="qt",
    ):
        captured["enc_scene"] = enc_scene
        captured["counters_only"] = counters_only
        return HARNESS.empty_report()

    monkeypatch.setattr(HARNESS, "run", fake_run)
    argv = ["--snapshot", str(tmp_path), "--output", str(tmp_path / "r.json")]
    HARNESS.main(argv + ["--enc-scene"])
    assert captured["counters_only"] is False
    HARNESS.main(argv + ["--enc-scene", "--counters-only"])
    assert captured["counters_only"] is True
    assert HARNESS.empty_report(counters_only=True)["counters_only"] is True
    assert HARNESS.empty_report()["counters_only"] is False


def test_counter_checks_are_off_by_default_and_flagged():
    for name in ("run", "_measure", "_session"):
        parameter = inspect.signature(getattr(HARNESS, name)).parameters[
            "counters_only"
        ]
        assert parameter.default is False


def test_counter_checks_pass_a_flat_sequence():
    rows = _steady_state_rows(
        [{step: 4.0 for step in HARNESS.COUNTER_FLAT_STEPS} for _ in range(3)]
    )
    checks = HARNESS.counter_only_checks(rows)
    assert set(checks) == set(HARNESS.COUNTER_CHECK_NAMES)
    assert all(checks.values())


def test_revisits_reject_growing_triangulations():
    rows = _counter_rows({"pan_back": {"mesh_triangulations": 13}})
    checks = HARNESS.counter_only_checks(rows)
    assert checks["revisits_add_no_geometry"] is False
    assert checks["no_resident_geometry_reupload"] is True
    assert checks["same_camera_no_rebuild"] is True


def test_revisits_reject_a_new_scene_or_store_miss():
    for key in ("scene_misses", "store_misses"):
        rows = _counter_rows({"zoom_back": {key: 2 if key == "scene_misses" else 3}})
        assert HARNESS.counter_only_checks(rows)["revisits_add_no_geometry"] is False


def test_an_unsettled_step_is_not_blamed_for_its_revisit():
    rows = _counter_rows({"pan": {"mesh_triangulations": 13}})
    for row in rows:
        if row["step"] == "pan":
            row["ready"] = False
            row["enc_scene_counters"]["matrix_update_count"] = 0
    checks = HARNESS.counter_only_checks(rows)
    assert checks["revisits_add_no_geometry"] is True
    assert checks["no_resident_geometry_reupload"] is True


def test_new_cells_on_a_wider_view_are_not_a_revisit_failure():
    grown = {"mesh_triangulations": 40, "scene_misses": 4, "store_misses": 5}
    rows = _counter_rows(
        {
            "zoom_out": grown,
            "zoom_back": grown,
            "switch_cell": grown,
            "return": grown,
        }
    )
    checks = HARNESS.counter_only_checks(rows)
    assert checks["revisits_add_no_geometry"] is True
    assert checks["same_camera_no_rebuild"] is True
    assert checks["no_resident_geometry_reupload"] is True


def test_same_camera_rejects_a_second_upload():
    rows = _counter_rows({"warm": {"upload_count": 3}})
    checks = HARNESS.counter_only_checks(rows)
    assert checks["same_camera_no_rebuild"] is False
    assert checks["revisits_add_no_geometry"] is True


def test_same_camera_rejects_a_new_generation():
    rows = _counter_rows({"warm": {"layer_generation": 2}})
    assert HARNESS.counter_only_checks(rows)["same_camera_no_rebuild"] is False


def test_upload_on_a_camera_move_is_allowed():
    rows = _counter_rows({"pan": {"upload_count": 9}})
    checks = HARNESS.counter_only_checks(rows)
    assert checks["no_resident_geometry_reupload"] is True


def test_a_camera_move_must_update_the_matrix():
    rows = _counter_rows({"pan": {"matrix_update_count": 0}})
    assert HARNESS.counter_only_checks(rows)["no_resident_geometry_reupload"] is False


def test_missing_counters_fail_rather_than_pass_by_default():
    counters = dict(_BASELINE_COUNTERS)
    counters["upload_count"] = None
    rows = _counter_rows({"flat": counters})
    assert HARNESS.counter_only_checks(rows)["no_resident_geometry_reupload"] is False
    assert HARNESS.counter_only_checks([]) == {
        "revisits_add_no_geometry": False,
        "no_resident_geometry_reupload": False,
        "per_move_cost_flat": None,
        "same_camera_no_rebuild": False,
    }


def test_assembly_flatness_ignores_the_cold_and_same_camera_steps():
    assembly = {
        "cold": 900.0,
        "warm": 900.0,
        "pan": 4.1,
        "pan_back": 4.2,
        "zoom_out": 4.3,
        "zoom_back": 4.4,
    }
    rows = _steady_state_rows([assembly, assembly, assembly])
    assert HARNESS.counter_only_checks(rows)["per_move_cost_flat"] is True


def test_assembly_flatness_rejects_cost_that_scales_with_the_view():
    assembly = {
        "pan": 4.0,
        "pan_back": 4.0,
        "zoom_out": 40.0,
        "zoom_back": 4.0,
    }
    rows = _steady_state_rows([assembly, assembly, assembly])
    assert HARNESS.counter_only_checks(rows)["per_move_cost_flat"] is False


def test_assembly_flatness_without_measurements_is_not_evaluated():
    rows = _counter_rows({"flat": {}})
    for row in rows:
        row["assembly_ms"] = None
    assert HARNESS.counter_only_checks(rows)["per_move_cost_flat"] is None


def test_assembly_flatness_tolerates_a_single_contention_outlier():
    """A per-class median absorbs one slow sample; max/median would fail."""
    jitter = {step: 5.0 for step in HARNESS.COUNTER_FLAT_STEPS}
    jitter["pan"] = 30.0
    quiet = {step: 5.0 for step in HARNESS.COUNTER_FLAT_STEPS}
    rows = _steady_state_rows([jitter, quiet, quiet])
    # The sample the old max/median statistic measured here:
    ordered = sorted(
        row["assembly_ms"]
        for row in rows
        if row["repeat"] == 1 and row["step"] in HARNESS.COUNTER_FLAT_STEPS
    )
    assert (
        ordered[-1] / HARNESS._percentile(ordered, 0.5)
        >= HARNESS.ASSEMBLY_FLATNESS_RATIO
    )
    assert HARNESS.counter_only_checks(rows)["per_move_cost_flat"] is True


def test_assembly_flatness_excludes_the_warmup_repeat():
    warmup = {step: 5.0 for step in HARNESS.COUNTER_FLAT_STEPS}
    warmup["pan"] = 50.0
    steady = {step: 5.0 for step in HARNESS.COUNTER_FLAT_STEPS}
    warmup_rows = _counter_rows({"flat": _BASELINE_COUNTERS}, warmup, repeat=0)
    assert HARNESS.counter_only_checks(warmup_rows)["per_move_cost_flat"] is None
    rows = warmup_rows + _steady_state_rows([steady, steady, steady])
    assert HARNESS.counter_only_checks(rows)["per_move_cost_flat"] is True


def test_assembly_flatness_with_too_few_moves_is_not_evaluated():
    rows = [
        row
        for row in _steady_state_rows(
            [{step: 5.0 for step in HARNESS.COUNTER_FLAT_STEPS}]
        )
        if row["step"] in ("pan", "pan_back")
    ]
    assert len(rows) < HARNESS.ASSEMBLY_FLATNESS_MIN_SAMPLES
    assert HARNESS.counter_only_checks(rows)["per_move_cost_flat"] is None


def test_enc_scene_assembly_reads_the_published_timings():
    class _Model:
        published = (
            3,
            object(),
            {"timings": {"assembly_ms": 7.25}},
        )

    class _Widget:
        _enc_scene_model = _Model()

    assert HARNESS._enc_scene_assembly_ms(_Widget()) == 7.25
    assert HARNESS._enc_scene_assembly_ms(object()) is None

    class _Empty:
        published = None

    class _ReusedWidget:
        _enc_scene_model = _Empty()

    assert HARNESS._enc_scene_assembly_ms(_ReusedWidget()) is None


def test_enc_scene_timings_are_milliseconds_per_stage():
    class _Model:
        published = (
            3,
            object(),
            {
                "timings": {
                    "assembly_ms": 2400.0,
                    "cell_open": 1.5,
                    "query": 0.25,
                    "triangulate": 0.5,
                }
            },
        )

    class _Widget:
        _enc_scene_model = _Model()

    stages = HARNESS._enc_scene_timings(_Widget())
    # Every declared key is present, so the report schema is stable even when a
    # stage did not run on this step.
    assert set(stages) == set(HARNESS.ENC_TIMING_KEYS)
    assert stages["assembly_ms"] == 2400.0
    assert stages["cell_open"] == 1500.0
    assert stages["query"] == 250.0
    assert stages["triangulate"] == 500.0
    assert stages["simplify"] is None

    class _Empty:
        published = None

    class _ReusedWidget:
        _enc_scene_model = _Empty()

    assert HARNESS._enc_scene_timings(_ReusedWidget()) is None
    assert HARNESS._enc_scene_timings(object()) is None


class _FakeClock:
    connected = False

    def __init__(self, widget):
        pass

    def mark(self):
        pass

    def since(self, started):
        return None


def test_counters_only_session_never_grabs(monkeypatch):
    from dataclasses import dataclass

    @dataclass
    class _CameraSpec:
        center_lat: float
        center_lon: float
        zoom: float

    class _Layer:
        enc_metadata = {"timings": {"assembly_ms": 3.5}}

        def request(self, camera, replace=False):
            pass

        def observations(self):
            return {"counters": {}}

    class _Widget:
        def __init__(self):
            self.counters = {"chart_publications": 0}
            self.camera = None
            self._chart_visible_generation = 0
            self._barbs = None

        def set_camera(self, camera):
            self.camera = camera
            self._chart_visible_generation += 1
            self.counters["chart_publications"] += 1

    class _CheckEnc:
        FIDELITY = {
            name: {"lat": 47.6, "lon": -122.4, "zoom": 9.5}
            for name in HARNESS.KNOWN_SCENARIOS
        }

        def __init__(self):
            self.timeouts = []

        def _wait(self, predicate, timeout):
            self.timeouts.append(timeout)
            return True

        def _readiness_conjuncts(self, layer, widget, before):
            return {"ready": True}

        def _pump(self, milliseconds):
            pass

        def _grab(self, widget):
            raise AssertionError("counters-only must not grab")

    tool = _CheckEnc()
    monkeypatch.setattr(HARNESS, "check_enc_module", lambda: tool)
    monkeypatch.setattr(HARNESS, "_RenderingClock", _FakeClock)
    monkeypatch.setattr(HARNESS, "_coverage", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        HARNESS,
        "_pre_demand_digest",
        lambda *args, **kwargs: pytest.fail("counters-only read a frame"),
    )
    monkeypatch.setattr(
        HARNESS,
        "_capture",
        lambda *args, **kwargs: pytest.fail("counters-only captured a frame"),
    )
    monkeypatch.setattr(
        HARNESS,
        "_enc_scene_counters",
        lambda widget: {
            "mesh_triangulations": 4,
            "scene_misses": 0,
            "store_misses": 0,
            "upload_count": 1,
        },
    )

    report = {"notes": [], "identity": {}}
    samples = HARNESS._session(
        report,
        _Widget(),
        _Layer(),
        _CameraSpec(47.6, -122.4, 9.5),
        (),
        None,
        None,
        ("san-francisco",),
        1,
        1,
        False,
        None,
        True,
        True,
    )
    assert len(samples) == len(HARNESS.DENSITIES) * len(_COUNTER_SEQUENCE)
    assert all(sample["capture_skipped"] is True for sample in samples)
    assert all(sample["stable_readback"] is None for sample in samples)
    assert all(sample["nonblank"] is None for sample in samples)
    assert all(sample["assembly_ms"] == 3.5 for sample in samples)
    assert all(sample["enc_scene_counters"] is not None for sample in samples)
    # Counter-only steps must never block on the chart layer's raster ENC
    # conjuncts, which can never hold in scene mode: every wait is bounded.
    assert tool.timeouts == [HARNESS.COUNTER_READY_TIMEOUT_S] * len(samples)
    assert HARNESS.COUNTER_READY_TIMEOUT_S <= 5.0
    assert all(sample["ready"] is True for sample in samples)
    assert all(isinstance(sample["readiness"], dict) for sample in samples)


# --------------------------------------------------------------------------
# CoreGraphics capture backend: OS-level capture instead of Qt's grab.
# --------------------------------------------------------------------------


class _FakeCapturedImage:
    """The ``QImage`` surface ``cg_frame_to_array`` reads, without Qt."""

    def __init__(self, width, height, stride, data, is_null=False):
        self._width = width
        self._height = height
        self._stride = stride
        self._data = data
        self._null = is_null
        self.formats = []

    def isNull(self):
        return self._null

    def convertToFormat(self, fmt):
        self.formats.append(fmt)
        return self

    def height(self):
        return self._height

    def width(self):
        return self._width

    def bytesPerLine(self):
        return self._stride

    def constBits(self):
        return self._data


def test_cg_frame_to_array_matches_the_premultiplied_bgra_contract():
    np = pytest.importorskip("numpy")
    # Two BGRA pixels plus four bytes of row padding: the padding must not
    # leak into the frame the fidelity math compares.
    data = bytes([1, 2, 3, 4, 5, 6, 7, 8]) + b"\xff\xff\xff\xff"
    image = _FakeCapturedImage(2, 1, 12, data)
    frame = HARNESS.cg_frame_to_array(image, format_premultiplied=99)
    assert frame.shape == (1, 2, 4)
    assert frame.dtype == np.uint8
    assert frame.tolist() == [[[1, 2, 3, 4], [5, 6, 7, 8]]]
    # The array is detached from the (fake) QImage buffer.
    assert frame.base is None
    assert image.formats == [99]


def test_cg_frame_to_array_rejects_a_null_image():
    with pytest.raises(RuntimeError, match="no image"):
        HARNESS.cg_frame_to_array(
            _FakeCapturedImage(2, 1, 8, b"", is_null=True), format_premultiplied=0
        )


def test_nonblank_fraction_is_zero_for_a_transparent_capture():
    np = pytest.importorskip("numpy")
    assert HARNESS.nonblank_fraction(np.zeros((4, 4, 4), np.uint8)) == 0.0
    painted = np.zeros((2, 2, 4), np.uint8)
    painted[0, 0] = [10, 20, 30, 255]
    assert HARNESS.nonblank_fraction(painted) == pytest.approx(0.25)


class _CgWindow:
    """Only the geometry ``cg_grab`` reads from a top-level window."""

    def __init__(self, width, height):
        self._width = width
        self._height = height

    def width(self):
        return self._width

    def height(self):
        return self._height


class _CgPoint:
    def __init__(self, x, y):
        self._x = x
        self._y = y

    def x(self):
        return self._x

    def y(self):
        return self._y


class _CgWidget:
    """A widget with just the geometry ``cg_grab`` crops by."""

    def __init__(self, window_size, offset=(0, 0), size=None):
        self._window = _CgWindow(*window_size)
        self._offset = offset
        self._size = size or window_size

    def window(self):
        return self._window

    def mapTo(self, window, point):
        return _CgPoint(*self._offset)

    def width(self):
        return self._size[0]

    def height(self):
        return self._size[1]


def test_cg_grab_refuses_a_blank_capture(monkeypatch):
    np = pytest.importorskip("numpy")
    diagnostic = {"width": 2, "height": 2, "stride": 8}
    monkeypatch.setattr(
        HARNESS, "capture_window_cg", lambda widget: (object(), dict(diagnostic))
    )
    monkeypatch.setattr(
        HARNESS,
        "cg_frame_to_array",
        lambda image: np.zeros((2, 2, 4), np.uint8),
    )
    _patch_cg_frame(monkeypatch, (0.0, 0.0, 2.0, 2.0))
    diagnostics = {}
    with pytest.raises(RuntimeError, match="blank"):
        HARNESS.cg_grab(diagnostics)(_CgWidget((2, 2)))
    # The refused frame still says what was captured, so the report can name it.
    assert diagnostics["nonblank_fraction"] == 0.0
    assert diagnostics["width"] == 2


def test_cg_grab_returns_a_painted_frame_and_records_the_ink(monkeypatch):
    np = pytest.importorskip("numpy")
    painted = np.zeros((2, 2, 4), np.uint8)
    painted[:, :, 3] = 255
    monkeypatch.setattr(
        HARNESS, "capture_window_cg", lambda widget: (object(), {"width": 2})
    )
    monkeypatch.setattr(HARNESS, "cg_frame_to_array", lambda image: painted)
    _patch_cg_frame(monkeypatch, (0.0, 0.0, 2.0, 2.0))
    diagnostics = {}
    frame = HARNESS.cg_grab(diagnostics)(_CgWidget((2, 2)))
    assert frame.shape == (2, 2, 4)
    assert diagnostics["nonblank_fraction"] == 1.0
    assert diagnostics["width"] == 2
    assert diagnostics["rect"] == [0, 0, 2, 2]


def test_cg_grab_crops_a_maximised_window_to_the_widget(monkeypatch):
    np = pytest.importorskip("numpy")
    # A maximised window at 2x: the chart is a small rectangle inside it.
    oversized = np.zeros((900, 1600, 4), np.uint8)
    oversized[:] = [0, 0, 0, 255]
    oversized[100:500, 200:800] = [255, 255, 255, 255]
    monkeypatch.setattr(
        HARNESS, "capture_window_cg", lambda widget: (object(), {"width": 1600})
    )
    monkeypatch.setattr(HARNESS, "cg_frame_to_array", lambda image: oversized)
    _patch_cg_frame(monkeypatch, (0.0, 0.0, 800.0, 450.0))
    widget = _CgWidget((800, 450), offset=(100, 50), size=(300, 200))
    diagnostics = {}
    chart = HARNESS.cg_grab(diagnostics)(widget)
    assert chart.shape[:2] == (400, 600)
    assert diagnostics["rect"] == [100, 200, 400, 600]
    assert diagnostics["nonblank_fraction"] == 1.0


def test_cg_grab_reports_geometry_it_cannot_map(monkeypatch):
    np = pytest.importorskip("numpy")
    # Width and height disagree on the pixels per point: not the window.
    skewed = np.zeros((900, 1600, 4), np.uint8)
    skewed[:] = [10, 10, 10, 255]
    monkeypatch.setattr(
        HARNESS, "capture_window_cg", lambda widget: (object(), {"width": 1600})
    )
    monkeypatch.setattr(HARNESS, "cg_frame_to_array", lambda image: skewed)
    _patch_cg_frame(monkeypatch, (0.0, 0.0, 800.0, 900.0))
    widget = _CgWidget((800, 900), offset=(0, 0), size=(710, 743))
    diagnostics = {}
    with pytest.raises(RuntimeError, match="non_uniform_scale"):
        HARNESS.cg_grab(diagnostics)(widget)
    assert diagnostics["scale"] == [1.0, 2.0]


class _CgQuickView:
    """A top-level ``QQuickView`` host: no ``window()`` and no ``mapTo()``."""

    def __init__(self, size):
        self._size = size

    def width(self):
        return self._size[0]

    def height(self):
        return self._size[1]

    def winId(self):
        return 4242


def _patch_cg_frame(monkeypatch, frame):
    monkeypatch.setattr(HARNESS, "cg_window_frame", lambda widget: frame)


def test_cg_geometry_for_a_quickview_insets_the_title_bar():
    # A 355x400 point frame holding a 710x744 logical client area at 2x: the
    # 28 point title bar sits above the client area inside the capture.
    widget = _CgQuickView((710, 744))
    window_size, offset, widget_size, host_kind = HARNESS._widget_window_geometry(
        widget, (579.0, 294.0, 355.0, 400.0)
    )
    assert window_size == (355.0, 400.0)
    assert offset[0] == 0.0
    assert offset[1] == pytest.approx(28.0)
    assert widget_size == (355.0, pytest.approx(372.0))
    assert host_kind in {"qquickwindow", "top_level"}
    assert host_kind != "qwidget"


def test_cg_geometry_keeps_the_qwidget_child_path():
    widget = _CgWidget((800, 450), offset=(100, 50), size=(300, 200))
    window_size, offset, widget_size, host_kind = HARNESS._widget_window_geometry(
        widget, (0.0, 0.0, 800.0, 450.0)
    )
    assert window_size == (800.0, 450.0)
    assert offset == (100.0, 50.0)
    assert widget_size == (300.0, 200.0)
    assert host_kind == "qwidget"


def test_cg_grab_crops_a_top_level_quickview_to_its_client_area(monkeypatch):
    np = pytest.importorskip("numpy")
    # The 710x800 pixel capture of a 355x400 point frame: the client area is
    # the lower 744 rows, directly under the 28 point (56 pixel) title bar.
    painted = np.zeros((800, 710, 4), np.uint8)
    painted[:, :, 3] = 255
    monkeypatch.setattr(
        HARNESS, "capture_window_cg", lambda widget: (object(), {"width": 710})
    )
    monkeypatch.setattr(HARNESS, "cg_frame_to_array", lambda image: painted)
    _patch_cg_frame(monkeypatch, (579.0, 294.0, 355.0, 400.0))
    diagnostics = {}
    chart = HARNESS.cg_grab(diagnostics)(_CgQuickView((710, 744)))
    assert chart.shape[:2] == (744, 710)
    assert diagnostics["rect"] == [56, 0, 744, 710]
    assert diagnostics["scale"] == [2.0, 2.0]
    assert diagnostics["widget_offset"] == [28.0, 0.0]
    assert diagnostics["widget_size"] == [372.0, 355.0]
    assert diagnostics["image_size"] == [800, 710]
    assert diagnostics["window_size"] == [400.0, 355.0]
    assert diagnostics["chart_shape"] == [744, 710]
    assert diagnostics["host_kind"] in {"qquickwindow", "top_level"}


def test_cg_grab_rejects_a_capture_that_is_not_the_window_frame(monkeypatch):
    np = pytest.importorskip("numpy")
    # The image's aspect cannot be a uniform scaling of the requested frame.
    skewed = np.zeros((372, 2840, 4), np.uint8)
    skewed[:] = [10, 10, 10, 255]
    monkeypatch.setattr(
        HARNESS, "capture_window_cg", lambda widget: (object(), {"width": 2840})
    )
    monkeypatch.setattr(HARNESS, "cg_frame_to_array", lambda image: skewed)
    _patch_cg_frame(monkeypatch, (579.0, 294.0, 355.0, 400.0))
    diagnostics = {}
    with pytest.raises(RuntimeError, match="non_uniform_scale"):
        HARNESS.cg_grab(diagnostics)(_CgQuickView((710, 744)))
    assert diagnostics["host_kind"] in {"qquickwindow", "top_level"}


def test_cg_window_info_reports_the_owning_process_and_bounds(monkeypatch):
    monkeypatch.setattr(HARNESS, "_cg_window_id", lambda widget: 4242)
    entries = [
        {
            "kCGWindowNumber": 4242,
            "kCGWindowOwnerPID": 99,
            "kCGWindowLayer": 0,
            "kCGWindowIsOnscreen": 1,
            "kCGWindowBounds": {
                "X": 10.0,
                "Y": 20.0,
                "Width": 355.0,
                "Height": 400.0,
            },
        }
    ]
    monkeypatch.setattr(HARNESS, "_cg_window_list", lambda option=0: entries)
    info = HARNESS.cg_window_info(_CgQuickView((710, 744)))
    assert info["window_id"] == 4242
    assert info["window_owner_pid"] == 99
    assert info["window_bounds"] == (10.0, 20.0, 355.0, 400.0)
    assert info["window_onscreen"] is True


def test_capture_window_guard_rejects_a_foreign_owner():
    ours = {"window_id": 4242, "window_owner_pid": 501}
    assert HARNESS._capture_window_problem(ours, 4242, pid=501) is None
    foreign = {"window_id": 4242, "window_owner_pid": 9}
    problem = HARNESS._capture_window_problem(foreign, 4242, pid=501)
    assert problem is not None
    assert "pid 9" in problem and "foreign" in problem
    # A window number that is not the widget's is a different window entirely.
    other = {"window_id": 4243, "window_owner_pid": 501}
    assert "changed under the capture" in (
        HARNESS._capture_window_problem(other, 4242, pid=501)
    )


def test_occluding_window_rejects_a_foreign_window_over_the_chart():
    chart_frame = (100.0, 100.0, 200.0, 200.0)
    points = HARNESS.chart_sample_points(chart_frame, (0, 0), (200, 200))
    bounds = {"X": 0.0, "Y": 0.0, "Width": 800.0, "Height": 600.0}
    terminal = [
        {
            "kCGWindowNumber": 7,
            "kCGWindowOwnerPID": 1234,
            "kCGWindowLayer": 0,
            "kCGWindowBounds": bounds,
        }
    ]
    _point, hit = HARNESS._occluding_window(terminal, points, 4242)
    assert hit == (7, 1234)
    ours = [
        {
            "kCGWindowNumber": 4242,
            "kCGWindowOwnerPID": 501,
            "kCGWindowLayer": 0,
            "kCGWindowBounds": bounds,
        }
    ]
    assert HARNESS._occluding_window(ours, points, 4242) is None
    # The menu bar layer sits above content but does not occlude it.
    menu_bar = [
        {
            "kCGWindowNumber": 3,
            "kCGWindowOwnerPID": 1,
            "kCGWindowLayer": 24,
            "kCGWindowBounds": bounds,
        }
    ] + ours
    assert HARNESS._occluding_window(menu_bar, points, 4242) is None


def test_cg_grab_rejects_a_partly_clipped_chart(monkeypatch):
    np = pytest.importorskip("numpy")
    frame = np.zeros((400, 400, 4), np.uint8)
    frame[:, :, :] = [10, 10, 10, 255]
    monkeypatch.setattr(
        HARNESS, "capture_window_cg", lambda widget: (object(), {"width": 400})
    )
    monkeypatch.setattr(HARNESS, "cg_frame_to_array", lambda image: frame)
    _patch_cg_frame(monkeypatch, (0.0, 0.0, 400.0, 400.0))
    # The chart starts one pixel left of the captured rect: the crop is short.
    widget = _CgWidget((400, 400), offset=(-1, 0), size=(400, 400))
    diagnostics = {}
    with pytest.raises(RuntimeError, match="chart_crop_clipped"):
        HARNESS.cg_grab(diagnostics)(widget)
    assert diagnostics["clipped_fraction"] > 0.0
    assert diagnostics["expected_chart_shape"] == [400, 400]
    # No chart shape is recorded: the frame never became a comparison.
    assert "chart_shape" not in diagnostics


def test_cg_chart_crop_rejects_a_widget_outside_the_capture():
    rect, diagnostic = HARNESS.cg_chart_crop(
        (400, 400), (400, 400), (350, 350), (400, 400)
    )
    assert rect is None
    assert diagnostic["reason"] == "widget_crop_outside_capture"


def test_cg_chart_crop_maps_the_widget_through_the_capture_scale():
    rect, diagnostic = HARNESS.cg_chart_crop(
        (1486, 2840), (1420, 743), (0, 0), (710, 743), target_size=(743, 710)
    )
    assert rect == {"x": 0, "y": 0, "width": 1420, "height": 1486}
    assert diagnostic["scale"] == [2.0, 2.0]
    assert diagnostic["clipped_fraction"] == 0.0


def test_crop_and_resize_put_the_chart_on_the_reference_grid():
    np = pytest.importorskip("numpy")
    frame = np.zeros((900, 1600, 4), np.uint8)
    frame[:] = [0, 0, 0, 255]
    frame[100:500, 200:800] = [255, 128, 64, 255]
    rect, _diagnostic = HARNESS.cg_chart_crop(
        frame.shape, (800, 450), (100, 50), (300, 200)
    )
    cropped = HARNESS.crop_frame(frame, rect)
    resized = HARNESS.resize_frame_bilinear(cropped, 150, 100)
    assert resized.shape == (100, 150, 4)
    # A solid chart stays solid after the resample: no blur into the border.
    assert tuple(int(level) for level in resized[50, 75]) == (255, 128, 64, 255)


def test_fit_capture_to_reference_resizes_and_reports_aspect():
    np = pytest.importorskip("numpy")
    reference = np.zeros((743, 710, 4), np.uint8)
    capture = np.zeros((1486, 1420, 4), np.uint8)
    fitted, note = HARNESS._fit_capture_to_reference(capture, reference)
    assert fitted.shape == (743, 710, 4)
    assert note["resized"] is True
    assert abs(note["aspect_error"]) < 1e-6
    same, note = HARNESS._fit_capture_to_reference(reference, reference)
    assert same.shape == (743, 710, 4)
    assert note["resized"] is False


def test_frames_within_tolerance_ignores_a_small_moving_strip():
    np = pytest.importorskip("numpy")
    first = np.zeros((100, 100, 4), np.uint8)
    first[:, :, 3] = 255
    second = first.copy()
    # Two rows of live window content moved: 2% of the rows, far above the
    # tolerated fraction when counted over the whole frame.
    second[0:2] = [255, 255, 255, 255]
    strict = HARNESS.frames_within_tolerance(first, second)
    assert strict["settled"] is False
    assert strict["differing_fraction"] == pytest.approx(0.02)
    relaxed = HARNESS.frames_within_tolerance(first, second, max_fraction=0.02)
    assert relaxed["settled"] is True


def test_frames_within_tolerance_rejects_a_new_frame_and_a_shape_change():
    np = pytest.importorskip("numpy")
    first = np.zeros((100, 100, 4), np.uint8)
    second = np.full((100, 100, 4), 40, np.uint8)
    result = HARNESS.frames_within_tolerance(first, second)
    assert result["settled"] is False
    assert result["differing_fraction"] == 1.0
    changed = HARNESS.frames_within_tolerance(first, np.zeros((100, 101, 4), np.uint8))
    assert changed["settled"] is False
    assert changed["reason"] == "shape_mismatch"


def test_frames_within_tolerance_accepts_a_few_levels_of_movement():
    np = pytest.importorskip("numpy")
    first = np.zeros((20, 20, 4), np.uint8)
    second = np.full((20, 20, 4), HARNESS.CG_SETTLE_TOLERANCE, np.uint8)
    assert HARNESS.frames_within_tolerance(first, second)["settled"] is True
    just_over = np.full((20, 20, 4), HARNESS.CG_SETTLE_TOLERANCE + 1, np.uint8)
    assert HARNESS.frames_within_tolerance(first, just_over)["settled"] is False


def test_scene_readiness_applies_only_to_counter_and_cg_scene_runs():
    assert HARNESS.uses_scene_readiness(True, False, "cg") is True
    assert HARNESS.uses_scene_readiness(False, True, "cg") is True
    # The Qt pixel path keeps the chart-layer rule: Gate 1 must not move.
    assert HARNESS.uses_scene_readiness(False, True, "qt") is False
    assert HARNESS.uses_scene_readiness(False, False, "cg") is False


def test_scene_demand_is_painted_by_a_new_publication_or_a_reuse():
    class _Model:
        def __init__(self, publications):
            self.publications = publications

        def observations(self):
            return {"counters": {"publications": self.publications}}

    class _SceneWidget:
        def __init__(self, publications):
            self._enc_scene_model = _Model(publications)

    widget = _SceneWidget(7)
    assert HARNESS._scene_demand_painted(widget, 6, False) is True
    assert HARNESS._scene_demand_painted(widget, 7, False) is False
    # A reused step re-issues the presented camera: no new publication.
    assert HARNESS._scene_demand_painted(widget, 7, True) is True


def test_capture_with_tolerance_reports_the_differing_fraction():
    np = pytest.importorskip("numpy")
    frames = [
        np.zeros((10, 10, 4), np.uint8),
        np.zeros((10, 10, 4), np.uint8),
    ]
    frames[1][0, 0] = 40  # one live pixel, still within the fraction budget
    widget = _FakeWidget()
    grabs = list(frames)

    frame, stable, settle = HARNESS._capture(
        _FakeCheckEnc([NEW]),
        widget,
        expect_generation=widget._chart_visible_generation,
        expect_camera=HARNESS._camera_token(widget.camera),
        pre_demand_generation=widget._chart_visible_generation,
        settle_tolerance=HARNESS.CG_SETTLE_TOLERANCE,
        settle_max_fraction=0.02,
        painted_probe=lambda: True,
        grab=lambda _w: grabs.pop(0) if grabs else frames[-1],
    )
    assert stable is True
    assert settle["settle_tolerance"] == HARNESS.CG_SETTLE_TOLERANCE
    assert settle["settle_differing_fraction"] == pytest.approx(0.01)
    assert settle["settle_max_delta"] == 40


def test_readback_uses_the_capture_backend_grab():
    tool = _FakeCheckEnc([NEW])
    widget = _FakeWidget()
    grabbed = []

    def grab(target):
        grabbed.append(target)
        return NEW

    frame, attempts, error = HARNESS._readback(tool, widget, grab=grab)
    assert frame is NEW
    assert attempts == 1
    assert error is None
    assert grabbed == [widget]
    # The Qt scene-graph grab was never called.
    assert tool.grabs == 0


def test_cg_backend_without_permission_stops_before_measuring(monkeypatch, tmp_path):
    monkeypatch.setattr(HARNESS, "_cg_available", lambda: False)
    monkeypatch.setattr(
        HARNESS,
        "_measure",
        lambda *args, **kwargs: pytest.fail("a denied capture must not measure"),
    )
    monkeypatch.setattr(
        HARNESS,
        "_load_tool",
        lambda *args, **kwargs: pytest.fail("a denied capture must not build a view"),
    )
    report = HARNESS.run(tmp_path, repeats=1, enc_scene=True, capture_backend="cg")
    assert report["capture_backend"] == "cg"
    assert "Screen Recording permission not granted" in report["error"]
    assert "CGPreflightScreenCaptureAccess=false" in report["error"]
    assert "System Settings > Privacy & Security > Screen Recording" in report["error"]
    # No fabricated measurement: the permission gate runs before any sample.
    assert report["sequence"] == []
    assert report["repeats_summary"] == {}
    assert report["complete"] is False
    assert HARNESS.validate_report(report) == []


def test_capture_backend_is_reported_and_defaults_by_mode(monkeypatch, tmp_path):
    captured = []

    def fake_run(
        snapshot,
        scenarios,
        repeats,
        requested_dpr,
        experience_only,
        dump_frames,
        enc_scene=False,
        counters_only=False,
        capture_backend="qt",
    ):
        captured.append((enc_scene, counters_only, capture_backend))
        return HARNESS.empty_report(capture_backend=capture_backend)

    monkeypatch.setattr(HARNESS, "run", fake_run)
    argv = ["--snapshot", str(tmp_path), "--output", str(tmp_path / "r.json")]
    HARNESS.main(argv)
    HARNESS.main(argv + ["--enc-scene"])
    HARNESS.main(argv + ["--enc-scene", "--counters-only"])
    HARNESS.main(argv + ["--enc-scene", "--capture-backend", "qt"])
    HARNESS.main(argv + ["--enc-scene", "--capture-backend", "cg"])
    assert captured == [
        (False, False, "qt"),
        (True, False, "cg"),
        (True, True, "qt"),
        (True, False, "qt"),
        (True, False, "cg"),
    ]
    assert HARNESS.empty_report()["capture_backend"] == "qt"
    assert HARNESS.empty_report(capture_backend="cg")["capture_backend"] == "cg"


# --------------------------------------------------------------------------
# Gate 3: multi-cell identity, residency, memory and explicit missing cells.
# --------------------------------------------------------------------------

_START_CAMERA = {"lat": 47.6, "lon": -122.4, "zoom": 9.5}
_ALTERNATE_CAMERA = {"lat": 48.5, "lon": -123.0, "zoom": 9.0}


def _gate3_row(
    step,
    camera=None,
    cells=("A", "B"),
    states=None,
    coverage=("A", "B"),
    extent=1.0,
    structure=True,
    counters=None,
    memory=None,
    stable=True,
    digests=None,
    signature=None,
    repeat=0,
    density="dense",
):
    order = [
        {"path": f"/data/cells/{name}.000", "name": f"{name}.000"} for name in cells
    ]
    if states is None:
        states = [
            {
                "path": record["path"],
                "name": record["name"],
                "state": "ready",
                "reason": "",
            }
            for record in order
        ]
    scene_counters = dict(counters or {})
    if memory is not None:
        scene_counters["memory"] = memory
    return {
        "step": step,
        "density": density,
        "repeat": repeat,
        "camera": dict(camera or _START_CAMERA),
        "ready": True,
        "cells_order": order,
        "cell_states": list(states),
        "coverage_selection": list(coverage),
        "coverage_enc_extent": extent,
        "structure_matches": structure,
        "stable_readback": stable,
        "capture_settle": {"digests": list(digests or ["1x1:aa"])},
        "frame_signature": list(signature) if signature is not None else None,
        "signature_drift": None,
        "enc_scene_counters": scene_counters or None,
    }


def _residency(mesh, scene=0, store=0):
    return {
        "mesh_triangulations": mesh,
        "scene_misses": scene,
        "store_misses": store,
    }


def _memory_snapshot(**overrides):
    memory = dict(
        HARNESS.CACHE_BYTE_CAPS,
        mesh_resident_bytes=10,
        scene_resident_bytes=10,
        store_resident_bytes=10,
        bytes_measured=True,
    )
    memory.update(overrides)
    return memory


def test_ordered_identity_requires_the_same_cells_in_the_same_order():
    matching = [
        _gate3_row("cold", coverage=("A", "B")),
        _gate3_row("switch_cell", coverage=("B", "A"), cells=("B", "A")),
    ]
    assert HARNESS.ordered_identity_exact(matching) is True
    reversed_order = [
        _gate3_row("cold", coverage=("A", "B")),
        _gate3_row("switch_cell", coverage=("A", "B"), cells=("B", "A")),
    ]
    assert HARNESS.ordered_identity_exact(reversed_order) is False
    different_membership = [_gate3_row("cold", coverage=("A", "B"), cells=("A",))]
    assert HARNESS.ordered_identity_exact(different_membership) is False
    # A step that has a reference selection but no scene plan is not a pass.
    no_plan = [_gate3_row("cold", cells=(), coverage=("A", "B"))]
    assert HARNESS.ordered_identity_exact(no_plan) is False
    assert HARNESS.ordered_identity_exact([]) is None


def test_coverage_regions_match_aggregates_structure_and_band():
    assert HARNESS.coverage_regions_match([_gate3_row("cold", extent=1.0)]) is True
    assert HARNESS.coverage_regions_match([_gate3_row("cold", extent=1.2)]) is False
    assert (
        HARNESS.coverage_regions_match([_gate3_row("cold", structure=False)]) is False
    )
    assert (
        HARNESS.coverage_regions_match(
            [_gate3_row("cold", extent=None, structure=False)]
        )
        is None
    )


def test_switch_and_return_must_not_retessellate_a_revisited_camera():
    flat = [
        _gate3_row("cold", counters=_residency(4)),
        _gate3_row(
            "switch_cell",
            camera=_ALTERNATE_CAMERA,
            cells=("C", "D"),
            counters=_residency(6),
        ),
        _gate3_row("return", counters=_residency(6)),
    ]
    assert HARNESS.switch_return_no_retessellation(flat) is True
    grown = [
        _gate3_row("cold", counters=_residency(4)),
        _gate3_row(
            "switch_cell",
            camera=_ALTERNATE_CAMERA,
            cells=("C", "D"),
            counters=_residency(6),
        ),
        _gate3_row("return", counters=_residency(9)),
    ]
    assert HARNESS.switch_return_no_retessellation(grown) is False
    # A switch back to a camera this repeat already painted is a revisit too.
    switch_back = [
        _gate3_row("cold", counters=_residency(4)),
        _gate3_row("switch_cell", camera=_ALTERNATE_CAMERA, counters=_residency(6)),
        _gate3_row("return", counters=_residency(6)),
        _gate3_row("switch_cell", camera=_ALTERNATE_CAMERA, counters=_residency(8)),
    ]
    assert HARNESS.switch_return_no_retessellation(switch_back) is False
    # A run with no switch or return step yields no verdict rather than a pass.
    assert HARNESS.switch_return_no_retessellation([_gate3_row("cold")]) is None


def test_memory_cap_reports_caps_and_enforces_residency():
    row = _gate3_row("cold", counters={}, memory=_memory_snapshot())
    assert HARNESS.memory_cap_reported([row]) is True
    over = _memory_snapshot(mesh_resident_bytes=256 * 1024 * 1024 + 1)
    assert HARNESS.memory_cap_reported([_gate3_row("cold", memory=over)]) is False
    missing_cap = _memory_snapshot(scene_cap_bytes=None)
    assert (
        HARNESS.memory_cap_reported([_gate3_row("cold", memory=missing_cap)]) is False
    )
    assert HARNESS.memory_cap_reported([_gate3_row("cold")]) is None


def test_missing_cell_requires_an_explicit_state_and_reason():
    explicit = [
        _gate3_row(
            "cold",
            states=[
                {
                    "path": "/data/cells/A.000",
                    "name": "A.000",
                    "state": "ready",
                    "reason": "",
                },
                {
                    "path": "/data/cells/B.000",
                    "name": "B.000",
                    "state": "unavailable",
                    "reason": "error: unreadable",
                },
            ],
        )
    ]
    assert HARNESS.missing_cell_explicit(explicit) is True
    silent = [
        _gate3_row(
            "cold",
            states=[
                {
                    "path": "/data/cells/A.000",
                    "name": "A.000",
                    "state": "ready",
                    "reason": "",
                },
                {
                    "path": "/data/cells/B.000",
                    "name": "B.000",
                    "state": "unavailable",
                    "reason": " ",
                },
            ],
        )
    ]
    assert HARNESS.missing_cell_explicit(silent) is False
    # A planned cell with no state record is a silent absence.
    truncated = [
        _gate3_row(
            "cold",
            states=[
                {
                    "path": "/data/cells/A.000",
                    "name": "A.000",
                    "state": "ready",
                    "reason": "",
                }
            ],
        )
    ]
    assert HARNESS.missing_cell_explicit(truncated) is False
    assert HARNESS.missing_cell_explicit([_gate3_row("cold", states=[])]) is None


def test_no_stale_frame_requires_stability_and_revisit_digests():
    settled = [
        _gate3_row("cold", digests=["1x1:aa"]),
        _gate3_row(
            "pan",
            camera=dict(_START_CAMERA, lat=_START_CAMERA["lat"] + 0.02),
            digests=["1x1:bb"],
        ),
        _gate3_row("pan_back", digests=["1x1:aa"]),
    ]
    assert HARNESS.no_stale_frame(settled) is True
    stale = [_gate3_row("cold", stable=False, digests=["1x1:aa"])]
    assert HARNESS.no_stale_frame(stale) is False
    changed = [
        _gate3_row("cold", digests=["1x1:aa"]),
        _gate3_row("pan_back", digests=["1x1:cc"]),
    ]
    assert HARNESS.no_stale_frame(changed) is False
    assert HARNESS.no_stale_frame([_gate3_row("cold", stable=None)]) is None


def test_no_stale_frame_accepts_identical_reissues_and_matching_revisits():
    # Repeats 2-4 re-issue an already-settled camera, so identical readback is
    # expected there; only the first occurrence must report stability.
    alternate = dict(_START_CAMERA, lat=_START_CAMERA["lat"] + 0.02)
    reissued = [
        _gate3_row("cold", digests=["1x1:aa"]),
        _gate3_row("cold", stable=False, digests=["1x1:aa"]),
        _gate3_row("pan", camera=alternate, digests=["1x1:bb"]),
        _gate3_row("pan", camera=alternate, stable=False, digests=["1x1:bb"]),
        _gate3_row("pan_back", stable=False, digests=["1x1:aa"]),
    ]
    assert HARNESS.no_stale_frame(reissued) is True


def test_no_stale_frame_rejects_a_revisit_that_drifted():
    drifted = [
        _gate3_row("cold", digests=["1x1:aa"]),
        _gate3_row(
            "pan",
            camera=dict(_START_CAMERA, lat=_START_CAMERA["lat"] + 0.02),
            digests=["1x1:bb"],
        ),
        _gate3_row("pan_back", stable=False, digests=["1x1:cc"]),
    ]
    assert HARNESS.no_stale_frame(drifted) is False


def test_no_stale_frame_treats_each_new_repeat_as_a_new_composition():
    rows = [
        _gate3_row("cold", digests=["1x1:aa"]),
        _gate3_row("cold", stable=False, digests=["1x1:aa"], repeat=1),
    ]
    assert HARNESS.no_stale_frame(rows) is False


def test_no_stale_frame_still_requires_stability_without_a_camera_key():
    row = _gate3_row("cold", stable=False, digests=["1x1:aa"])
    row["camera"] = None
    assert HARNESS.no_stale_frame([row]) is False


def test_frame_signature_is_a_coarse_block_mean_grid():
    import numpy as np

    frame = np.zeros((32, 32, 4), dtype="uint8")
    frame[:16, :, :] = 100
    frame[16:, :, :] = 200
    signature = HARNESS.frame_signature(frame, grid=4)
    # 4x4 blocks, four channels each, row-major with channels innermost.
    assert len(signature) == 4 * 4 * 4
    assert signature[: 2 * 4 * 4] == [100.0] * 32
    assert signature[2 * 4 * 4 :] == [200.0] * 32
    assert HARNESS.frame_signature(np.zeros((0, 4, 4))) == []


def test_signature_drift_tolerates_a_little_live_noise_only():
    reference = [10.0] * 64
    noise = [10.0] * 64
    noise[0] = 11.5
    matched = HARNESS.signature_drift(noise, reference)
    assert matched["matches"] is True
    assert matched["blocks"] == 16
    assert matched["max_delta"] == 1.5
    # One block far off in sixteen is 6.25%, past the 5% budget.
    different = [10.0] * 64
    different[4] = 40.0
    drift = HARNESS.signature_drift(different, reference)
    assert drift["matches"] is False
    assert drift["differing_fraction"] == 0.0625
    # A missing counterpart was not a comparable frame.
    assert HARNESS.signature_drift([], reference)["matches"] is False
    assert HARNESS.signature_drift(None, reference)["matches"] is False


def test_no_stale_frame_accepts_a_revisit_with_coarse_signature_drift():
    # Window captures are not byte-reproducible: the revisit digest differs
    # from the settled one, but the block-mean composition still matches.
    reference = [10.0] * 64
    noisy = [10.0] * 64
    noisy[0] = 11.0
    rows = [
        _gate3_row("cold", digests=["1x1:aa"], signature=reference),
        _gate3_row(
            "pan",
            camera=dict(_START_CAMERA, lat=_START_CAMERA["lat"] + 0.02),
            digests=["1x1:bb"],
            signature=[50.0] * 64,
        ),
        _gate3_row("pan_back", stable=False, digests=["1x1:cc"], signature=noisy),
    ]
    assert HARNESS.no_stale_frame(rows) is True


def test_no_stale_frame_rejects_a_revisit_with_a_different_composition():
    rows = [
        _gate3_row("cold", digests=["1x1:aa"], signature=[10.0] * 64),
        _gate3_row(
            "pan_back", stable=False, digests=["1x1:cc"], signature=[200.0] * 64
        ),
    ]
    assert HARNESS.no_stale_frame(rows) is False


def test_no_stale_frame_signature_never_excuses_an_unsettled_new_composition():
    rows = [_gate3_row("cold", stable=False, signature=[10.0] * 64)]
    assert HARNESS.no_stale_frame(rows) is False


def test_gate3_checks_report_every_name_and_verdict():
    samples = [
        _gate3_row("cold", counters=_residency(4), memory=_memory_snapshot()),
        _gate3_row(
            "switch_cell",
            camera=_ALTERNATE_CAMERA,
            cells=("C", "D"),
            coverage=("C", "D"),
            counters=_residency(6),
            memory=_memory_snapshot(),
        ),
        _gate3_row("return", counters=_residency(6), memory=_memory_snapshot()),
    ]
    checks = HARNESS.gate3_checks(samples)
    assert tuple(checks) == HARNESS.GATE3_CHECK_NAMES
    assert all(value is True for value in checks.values())
    report = HARNESS.gate3_report(samples)
    assert report["cells_order_steps"] == 3
    assert report["max_cells"] == 2
    assert [row["step"] for row in report["switch_return"]] == ["switch_cell", "return"]
    assert report["switch_return"][0]["new_cells"] == ["C", "D"]
    assert report["switch_return"][1]["new_cells"] == []
    assert report["memory"]["mesh_cap_bytes"] == 256 * 1024 * 1024
    assert report["coverage_enc_extent"] == {"min": 1.0, "max": 1.0}
    assert report["stable_steps"] == 3


def test_gate3_report_lists_a_missing_cell_explicitly():
    samples = [
        _gate3_row(
            "cold",
            states=[
                {
                    "path": "/data/cells/A.000",
                    "name": "A.000",
                    "state": "oversized",
                    "reason": "scene exceeds the cache budget",
                }
            ],
            cells=("A",),
        )
    ]
    report = HARNESS.gate3_report(samples)
    assert report["missing_cells"] == [
        {
            "density": "dense",
            "step": "cold",
            "repeat": 0,
            "name": "A.000",
            "state": "oversized",
            "reason": "scene exceeds the cache budget",
        }
    ]


def test_scene_records_fall_back_to_the_viewport_property():
    class _Widget:
        _enc_scene_model = None

        def property(self, name):
            if name == "enc_scene_cells_order":
                return ({"path": "/data/cells/A.000", "name": "A.000"},)
            return None

    records = HARNESS.scene_cell_records(_Widget())
    assert [record["path"] for record in records] == ["/data/cells/A.000"]
    assert HARNESS.scene_cell_states(_Widget()) == []


def test_cache_memory_reports_static_caps_without_a_layer():
    memory = HARNESS._cache_memory(None)
    assert memory["mesh_cap_bytes"] == 256 * 1024 * 1024
    assert memory["scene_cap_bytes"] == 64 * 1024 * 1024
    assert memory["store_cap_bytes"] == 256 * 1024 * 1024
    assert memory["bytes_measured"] is False
    assert memory["mesh_resident_bytes"] is None


def test_enc_scene_session_records_the_ordered_plan_and_memory(monkeypatch):
    from dataclasses import dataclass

    @dataclass
    class _CameraSpec:
        center_lat: float
        center_lon: float
        zoom: float

    class _Cache:
        def __init__(self, max_bytes, resident_bytes):
            self.max_bytes = max_bytes
            self.resident_bytes = resident_bytes

    class _Model:
        cells_order = (
            {
                "path": "/data/cells/A.000",
                "name": "A.000",
                "rank_key": (0, 1.0),
                "version": (1,),
                "coverage_known": True,
            },
        )
        cell_states = (
            {
                "path": "/data/cells/A.000",
                "name": "A.000",
                "state": "unavailable",
                "reason": "error: unreadable",
            },
        )
        published = None

        def __init__(self):
            self.counters = {"publications": 1}
            self._mesh_cache = _Cache(256 * 1024 * 1024, 1024)
            self._scene_cache = _Cache(64 * 1024 * 1024, 512)
            self._scene_store = _Cache(256 * 1024 * 1024, 0)

        def observations(self):
            return {
                "state": "ready",
                "generation": 1,
                "counters": dict(self.counters),
            }

    model = _Model()

    class _Layer:
        enc_metadata = {"timings": {"assembly_ms": 3.5}}

        def request(self, camera, replace=False):
            pass

        def observations(self):
            return {"counters": {}}

    class _Widget:
        _enc_scene_model = model
        _enc_scene_item = None

        def __init__(self):
            self.counters = {"chart_publications": 0}
            self.camera = None
            self._chart_visible_generation = 0
            self._barbs = None

        def set_camera(self, camera):
            self.camera = camera
            self._chart_visible_generation += 1
            self.counters["chart_publications"] += 1

    class _CheckEnc:
        FIDELITY = {
            name: {"lat": 47.6, "lon": -122.4, "zoom": 9.5}
            for name in HARNESS.KNOWN_SCENARIOS
        }

        def _wait(self, predicate, timeout):
            return True

        def _readiness_conjuncts(self, layer, widget, before):
            return {"ready": True}

        def _pump(self, milliseconds):
            pass

        def _grab(self, widget):
            raise AssertionError("counters-only must not grab")

    monkeypatch.setattr(HARNESS, "check_enc_module", lambda: _CheckEnc())
    monkeypatch.setattr(HARNESS, "_RenderingClock", _FakeClock)
    monkeypatch.setattr(HARNESS, "_coverage", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        HARNESS, "_capture", lambda *args, **kwargs: pytest.fail("capture in counters")
    )
    report = {"notes": [], "identity": {}}
    samples = HARNESS._session(
        report,
        _Widget(),
        _Layer(),
        _CameraSpec(47.6, -122.4, 9.5),
        (),
        None,
        None,
        ("san-francisco",),
        1,
        1,
        False,
        None,
        True,
        True,
    )
    assert len(samples) == len(HARNESS.DENSITIES) * len(HARNESS.STEPS)
    assert all(len(sample["cells_order"]) == 1 for sample in samples)
    assert all(sample["cells_order"][0]["path"].endswith("A.000") for sample in samples)
    assert all(sample["cell_states"][0]["state"] == "unavailable" for sample in samples)
    memory = samples[0]["enc_scene_counters"]["memory"]
    assert memory["mesh_cap_bytes"] == 256 * 1024 * 1024
    assert memory["scene_cap_bytes"] == 64 * 1024 * 1024
    assert memory["store_cap_bytes"] == 256 * 1024 * 1024
    assert memory["mesh_resident_bytes"] == 1024
    assert memory["scene_resident_bytes"] == 512
    assert memory["bytes_measured"] is True
    # The explicit missing state is visible to the Gate 3 verdicts.
    assert HARNESS.missing_cell_explicit(samples) is True


def test_validate_report_requires_a_complete_gate3_set():
    report = HARNESS.empty_report(scenarios=("san-francisco",), repeats=1)
    report["checks"].update(HARNESS.gate3_checks([]))
    assert HARNESS.validate_report(report) == []
    del report["checks"]["missing_cell_explicit"]
    problems = HARNESS.validate_report(report)
    assert "missing check: missing_cell_explicit" in problems


def test_switch_targets_the_multi_cell_view_when_it_is_listed():
    # The default two-scenario run keeps the switch target it always had.
    assert (
        HARNESS.switch_scenario(("san-francisco", "puget"), "san-francisco") == "puget"
    )
    assert (
        HARNESS.switch_scenario(("san-francisco", "puget"), "puget") == "san-francisco"
    )
    # An anchor-only run still crosses somewhere rather than switching to
    # itself, and the multi-cell view wins when it is the stand-in.
    assert HARNESS.switch_scenario(("puget",), "puget") == "multi-cell"
    assert HARNESS.switch_scenario(("san-francisco",), "san-francisco") == "multi-cell"
    assert HARNESS.switch_scenario(("multi-cell",), "multi-cell") == "san-francisco"
    for anchor in HARNESS.KNOWN_SCENARIOS:
        target = HARNESS.switch_scenario((anchor,), anchor)
        assert target != anchor
        assert target in HARNESS.KNOWN_SCENARIOS
    # The multi-cell camera wins whenever the run lists it.
    assert (
        HARNESS.switch_scenario(
            ("san-francisco", "puget", "multi-cell"), "san-francisco"
        )
        == "multi-cell"
    )
    assert (
        HARNESS.switch_scenario(("san-francisco", "puget", "multi-cell"), "puget")
        == "multi-cell"
    )
