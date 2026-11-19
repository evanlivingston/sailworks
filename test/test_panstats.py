"""Tests for the opt-in pan metrics collector."""

import json
import time

import pytest

from sailworks.qt import panstats

FIELDS = [
    "camera_hz",
    "frames_moving_hz",
    "frame_p50_ms",
    "frame_p95_ms",
    "frames_idle_hz",
    "publishes_moving",
    "holds",
    "resumes",
    "session_s",
    "hitches",
    "hitch_max_ms",
    "hitch_top",
    "raster_skips",
    "camera_ms_p50",
    "camera_ms_p95",
    "camera_ms_max",
    "camera_busy_pct",
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv(panstats.ENV_VAR, raising=False)
    panstats.reset()
    panstats._connections.clear()
    yield
    panstats.reset()
    panstats._connections.clear()


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeSignal:
    def __init__(self, explode=False):
        self.slots = []
        self.explode = explode

    def connect(self, slot, *args):
        if self.explode:
            raise AssertionError("connected while disabled")
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)


class FakeWindow:
    def __init__(self, explode=False):
        self.afterRendering = FakeSignal(explode)
        self.destroyed = FakeSignal(explode)


def boom(*_args, **_kwargs):
    raise AssertionError("clock read while disabled")


def test_disabled_reads_no_clock(monkeypatch):
    monkeypatch.setattr(time, "perf_counter", boom)
    window = FakeWindow(explode=True)

    assert panstats.enabled() is False
    assert panstats.connect_window(window) is None
    panstats.camera_changed(object())
    panstats.frame_presented(1.0)
    panstats.published("chart")
    panstats.skipped("raster_skip")
    panstats.held("enc_scene")
    panstats.resumed("enc_scene")
    assert panstats.summary() == {}
    assert panstats.summary_line() == ""
    assert panstats.flush() == ""
    assert panstats._marker_ring is None
    assert panstats._hitch_records == []
    assert panstats._camera_ms == []
    assert panstats._camera_ms_pending == 0.0
    assert panstats._camera_ms_moving == 0.0
    assert window.afterRendering.slots == []


def test_enabled_rates_and_percentiles(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())
    for now in (0.010, 0.020, 0.030, 0.040):
        clock.now = now
        panstats.frame_presented()
    clock.now = 1.000
    panstats.frame_presented()
    clock.now = 2.000
    panstats.frame_presented()
    clock.now = 0.050
    panstats.published("chart")
    clock.now = 2.100
    panstats.published("chart")
    panstats.held("enc_scene")
    panstats.resumed("enc_scene")
    clock.now = 2.200

    record = panstats.summary()
    assert list(record) == FIELDS
    assert record["camera_hz"] == 0.5
    assert record["frames_moving_hz"] == 100.0
    assert record["frame_p50_ms"] == 10.0
    assert record["frame_p95_ms"] == 10.0
    assert record["frames_idle_hz"] == 1.0
    assert record["publishes_moving"] == 1
    assert record["holds"] == 1
    assert record["resumes"] == 1
    assert record["session_s"] == 2.2


def test_motion_boundary(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())
    clock.now = 0.050
    panstats.frame_presented()  # first frame: no interval
    clock.now = 0.099
    panstats.frame_presented()  # 49 ms after a camera change: moving
    clock.now = 0.101
    panstats.frame_presented()  # 101 ms after it: idle
    clock.now = 0.150

    record = panstats.summary()
    assert record["frame_p50_ms"] == 49.0
    assert record["frame_p95_ms"] == 49.0
    assert record["frames_moving_hz"] == pytest.approx(20.4, abs=0.1)
    assert record["frames_idle_hz"] == pytest.approx(500.0, abs=0.1)


def test_summary_line_order_and_rounding(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)
    panstats.camera_changed(object())
    clock.now = 0.012
    panstats.frame_presented()
    clock.now = 0.024
    panstats.frame_presented()
    clock.now = 3.000

    line = panstats.summary_line()
    assert line.startswith("panstats: ")
    keys = [part.split("=")[0] for part in line[len("panstats: ") :].split()]
    assert keys == FIELDS
    assert line == (
        "panstats: camera_hz=0.3 frames_moving_hz=83.3 frame_p50_ms=12.0 "
        "frame_p95_ms=12.0 frames_idle_hz=0.0 publishes_moving=0 holds=0 "
        "resumes=0 session_s=3.0 hitches=0 hitch_max_ms=0.0 hitch_top=none "
        "raster_skips=0 camera_ms_p50=0.0 camera_ms_p95=0.0 camera_ms_max=0.0 "
        "camera_busy_pct=0.0"
    )


def test_json_written_to_path(monkeypatch, tmp_path, capsys):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)
    panstats.camera_changed(object())
    clock.now = 0.016
    panstats.frame_presented()
    clock.now = 0.032
    panstats.frame_presented()
    clock.now = 1.032

    line = panstats.flush()
    assert line == capsys.readouterr().out.strip()
    assert line.startswith("panstats: ")

    written = json.loads(path.read_text(encoding="utf-8").strip())
    assert list(written) == FIELDS + ["hitch_records"]
    assert written["frames_moving_hz"] == 62.5
    assert written["frame_p50_ms"] == 16.0
    assert written["hitch_records"] == []


def test_connect_and_disconnect_idempotent(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    window = FakeWindow()

    assert panstats.connect_window(window) is not None
    assert panstats.connect_window(window) is None
    assert len(window.afterRendering.slots) == 1

    assert panstats.disconnect_window(window) is True
    assert panstats.disconnect_window(window) is False
    assert window.afterRendering.slots == []

    assert panstats.connect_window(window) is not None
    assert len(window.afterRendering.slots) == 1


def test_connect_window_disabled_keeps_no_slots(monkeypatch):
    window = FakeWindow(explode=True)
    assert panstats.connect_window(window, lambda: None) is None
    assert window.afterRendering.slots == []
    assert window.destroyed.slots == []


def test_raster_skip_is_not_a_publication(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())
    clock.now = 0.010
    panstats.frame_presented()
    clock.now = 0.020
    panstats.frame_presented()
    clock.now = 0.030
    panstats.published("raster_skip")
    panstats.skipped("raster_skip")
    clock.now = 0.040
    panstats.published("chart")

    record = panstats.summary()
    assert record["publishes_moving"] == 1
    assert record["raster_skips"] == 2


def test_hitch_carries_marker_kinds_and_ages(monkeypatch, tmp_path):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())  # 0.000, before the previous frame
    panstats.frame_presented()
    clock.now = 0.010
    panstats.camera_changed(object())
    clock.now = 0.050
    panstats.published("chart")
    clock.now = 0.120
    panstats.held("enc_scene")
    clock.now = 0.200
    panstats.frame_presented()  # 200 ms since the previous frame

    record = panstats.summary()
    assert record["hitches"] == 1
    assert record["hitch_max_ms"] == 200.0
    assert record["publishes_moving"] == 1

    panstats.flush()
    written = json.loads(path.read_text(encoding="utf-8").strip())
    assert len(written["hitch_records"]) == 1
    hitch = written["hitch_records"][0]
    assert hitch["gap_ms"] == 200.0
    assert [marker["kind"] for marker in hitch["markers"]] == [
        "camera",
        "published(chart)",
        "held",
    ]
    assert [marker["age_ms"] for marker in hitch["markers"]] == [190.0, 150.0, 80.0]


def test_hitch_top_is_the_dominant_marker_kind(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())
    panstats.frame_presented()  # 0.000
    clock.now = 0.150
    panstats.camera_changed(object())
    panstats.published("chart")
    clock.now = 0.200
    panstats.frame_presented()  # 200 ms: camera + chart
    clock.now = 0.250
    panstats.camera_changed(object())
    clock.now = 0.280
    panstats.published("chart")
    clock.now = 0.310
    panstats.frame_presented()  # 110 ms: camera + chart
    clock.now = 0.400
    panstats.published("chart")
    clock.now = 0.500
    panstats.frame_presented()  # 190 ms: chart only

    record = panstats.summary()
    assert record["hitches"] == 3
    assert record["hitch_max_ms"] == 200.0
    assert record["hitch_top"] == "published(chart)"


def test_json_hitch_list_and_ring_are_bounded(monkeypatch, tmp_path):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())
    panstats.frame_presented()
    for step in range(1, 41):
        clock.now = step * 0.3
        panstats.camera_changed(object())
        panstats.published("chart")
        panstats.frame_presented()

    panstats.flush()
    written = json.loads(path.read_text(encoding="utf-8").strip())
    assert written["hitches"] == 40
    assert len(written["hitch_records"]) == panstats.HITCH_RECORDS_MAX
    assert len(written["hitch_records"]) <= 20
    assert len(panstats._marker_ring) == panstats.RING_SIZE


def test_idle_gaps_are_not_hitches(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())
    clock.now = 0.500
    panstats.frame_presented()
    clock.now = 0.900
    panstats.frame_presented()

    record = panstats.summary()
    assert record["hitches"] == 0
    assert record["hitch_max_ms"] == 0.0
    assert record["hitch_top"] == "none"


def test_camera_handler_times_summarised(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object(), 10.0)  # before the first frame: unwindowed
    clock.now = 0.050
    panstats.frame_presented()
    clock.now = 0.100
    panstats.camera_changed(object(), 20.0)
    clock.now = 0.150
    panstats.frame_presented()  # 100 ms moving interval, 20 ms of handler
    clock.now = 0.200
    panstats.camera_changed(object(), 30.0)
    clock.now = 0.250
    panstats.frame_presented()  # 100 ms moving interval, 30 ms of handler

    record = panstats.summary()
    assert record["camera_ms_p50"] == 20.0
    assert record["camera_ms_p95"] == 30.0
    assert record["camera_ms_max"] == 30.0
    # 50 ms of handler over 200 ms of moving-frame wall time.
    assert record["camera_busy_pct"] == 25.0


def test_camera_busy_pct_zero_without_motion(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object(), 7.0)
    clock.now = 0.500
    panstats.frame_presented()
    clock.now = 0.700
    panstats.frame_presented()  # idle: no moving wall time to divide by

    record = panstats.summary()
    assert record["camera_ms_max"] == 7.0
    assert record["camera_busy_pct"] == 0.0


def test_hitch_carries_camera_handler_time(monkeypatch, tmp_path):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object(), 5.0)
    clock.now = 0.010
    panstats.frame_presented()
    clock.now = 0.020
    panstats.camera_changed(object(), 12.5)
    clock.now = 0.030
    panstats.camera_changed(object(), 7.5)
    clock.now = 0.300
    panstats.frame_presented()  # 290 ms gap containing the two handlers

    panstats.flush()
    written = json.loads(path.read_text(encoding="utf-8").strip())
    hitch = written["hitch_records"][0]
    assert hitch["gap_ms"] == 290.0
    assert hitch["camera_ms"] == 20.0
