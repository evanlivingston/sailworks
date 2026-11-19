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
    panstats.held("enc_scene")
    panstats.resumed("enc_scene")
    assert panstats.summary() == {}
    assert panstats.summary_line() == ""
    assert panstats.flush() == ""
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
        "resumes=0 session_s=3.0"
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
    assert list(written) == FIELDS
    assert written["frames_moving_hz"] == 62.5
    assert written["frame_p50_ms"] == 16.0


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
