"""Tests for the opt-in pan metrics collector."""

import json
import os
import subprocess
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
    "paint_ms_p50",
    "paint_ms_p95",
    "render_ms_p50",
    "render_ms_p95",
    "gap_ms_p50",
    "gap_ms_p95",
    "repaints",
    "repaints_hz",
    "repaints_moving",
    "repaints_moving_hz",
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


class FakeQuickHost(FakeWindow):
    """A wrapper whose ``quickWindow()`` exposes a ``beforeRendering``."""

    def __init__(self, explode=False):
        super().__init__(explode)
        self.quick = type("Quick", (), {})()
        self.quick.beforeRendering = FakeSignal(explode)

    def quickWindow(self):
        return self.quick


def boom(*_args, **_kwargs):
    raise AssertionError("clock read while disabled")


class Sampler:
    """Stand-in for a launched ``sample`` child process."""

    def __init__(self, argv, alive=False):
        self.argv = argv
        self.alive = alive
        self.terminated = False

    def poll(self):
        return None if self.alive and not self.terminated else 0

    def terminate(self):
        self.terminated = True


@pytest.fixture(autouse=True)
def sampler(monkeypatch):
    """Record sampler spawns so no real ``sample`` process ever starts."""
    spawned = []

    def fake_popen(argv, **kwargs):
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        process = Sampler(argv)
        spawned.append(process)
        return process

    monkeypatch.setattr(panstats.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(panstats.shutil, "which", lambda _name: panstats.SAMPLE_BIN)
    return spawned


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
    panstats.repaint_requested()
    assert panstats.summary() == {}
    assert panstats.summary_line() == ""
    assert panstats.flush() == ""
    assert panstats._marker_ring is None
    assert panstats._hitch_records == []
    assert panstats._camera_ms == []
    assert panstats._camera_ms_pending == 0.0
    assert panstats._camera_ms_moving == 0.0
    assert panstats._repaints == 0
    assert panstats._repaints_moving == 0
    assert panstats._repaint_ring is None
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
        "camera_busy_pct=0.0 paint_ms_p50=0.0 paint_ms_p95=0.0 "
        "render_ms_p50=0.0 render_ms_p95=0.0 gap_ms_p50=0.0 gap_ms_p95=0.0 "
        "repaints=0 repaints_hz=0.0 repaints_moving=0 repaints_moving_hz=0.0"
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


def test_paint_and_render_percentiles(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    samples = ((10.0, 4.0), (20.0, 6.0), (30.0, 8.0), (40.0, 10.0), (500.0, 100.0))
    for step, (paint, render) in enumerate(samples):
        clock.now = step * 0.2 + 0.050
        panstats.painted(paint)
        clock.now = step * 0.2 + 0.060
        panstats._render_started()
        clock.now = step * 0.2 + 0.060 + render / 1000.0
        panstats.frame_presented()

    record = panstats.summary()
    assert record["paint_ms_p50"] == 30.0
    assert record["paint_ms_p95"] == 500.0
    assert record["render_ms_p50"] == 8.0
    assert record["render_ms_p95"] == 100.0


def test_hitch_carries_paint_and_render_sums(monkeypatch, tmp_path):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())
    clock.now = 0.010
    panstats.frame_presented()
    clock.now = 0.020
    panstats.painted(12.0)
    clock.now = 0.030
    panstats.painted(8.0)
    clock.now = 0.040
    panstats._render_started()
    clock.now = 0.300
    panstats.frame_presented()  # 290 ms gap containing both paint samples

    panstats.flush()
    written = json.loads(path.read_text(encoding="utf-8").strip())
    hitch = written["hitch_records"][0]
    assert hitch["paint_ms"] == 20.0
    assert hitch["render_ms"] == 260.0


def test_disabled_paint_and_render_read_no_clock(monkeypatch):
    monkeypatch.setattr(time, "perf_counter", boom)
    window = FakeQuickHost(explode=True)

    assert panstats.connect_window(window) is None
    panstats.painted(10.0)
    panstats._render_started()

    assert panstats._paint_ring is None
    assert panstats._render_ring is None
    assert panstats._render_basis is None
    assert panstats._paint_ms == []
    assert panstats._render_ms == []
    assert window.quick.beforeRendering.slots == []
    assert window.afterRendering.slots == []


def test_render_signal_connected_once_and_released(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    monkeypatch.setattr(time, "perf_counter", Clock())
    window = FakeQuickHost()

    panstats.connect_window(window, lambda: None)
    panstats.connect_window(window, lambda: None)
    assert window.quick.beforeRendering.slots == [panstats._render_started]

    panstats.disconnect_window(window)
    assert window.quick.beforeRendering.slots == []
    assert panstats._render_connections == {}


def test_odd_and_missing_samples_do_not_raise(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    empty = panstats.summary()
    assert empty["paint_ms_p50"] == 0.0
    assert empty["render_ms_p95"] == 0.0

    panstats.painted(-4.0)
    panstats.painted(0.0)
    clock.now = 0.002
    panstats._render_started()
    clock.now = 0.001  # a clock that went backwards cannot make a negative sample
    panstats.frame_presented()

    record = panstats.summary()
    assert record["paint_ms_p50"] == 0.0
    assert record["paint_ms_p95"] == 0.0
    assert record["render_ms_p50"] == 0.0
    assert record["render_ms_p95"] == 0.0


def test_stall_during_motion_captures_once(monkeypatch, tmp_path, sampler):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock(10.0)
    monkeypatch.setattr(time, "perf_counter", clock)
    panstats.frame_presented()
    clock.now = 10.4
    panstats.camera_changed()
    clock.now = 10.6
    dump = tmp_path / "pan.jsonl.stall"

    assert panstats.check_stall(clock.now) == str(dump)
    assert len(sampler) == 1
    assert sampler[0].argv[:2] == ["/usr/bin/sample", str(os.getpid())]
    assert sampler[0].argv[2:] == ["1", "1", "-file", str(dump) + ".native"]

    text = dump.read_text(encoding="utf-8")
    assert text.count("panstats-stall:") == 1
    header = text.splitlines()[0]
    assert "age_ms=600.0" in header
    assert "camera_ms=200.0" in header
    assert "dump=1/5" in header
    assert "sample=ok" in header
    assert "panstats: camera_hz=" in header
    assert "(most recent call first)" in text

    # The same stall episode captures only once.
    clock.now = 10.7
    assert panstats.check_stall(clock.now) is None
    assert len(sampler) == 1
    assert dump.read_text(encoding="utf-8") == text


def test_stall_can_ignore_startup_and_use_a_longer_threshold(
    monkeypatch, tmp_path, sampler
):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    monkeypatch.setenv(panstats.ENV_STALL_S, "2.0")
    monkeypatch.setenv(panstats.ENV_STALL_AFTER_S, "5.0")
    clock = Clock(0.0)
    monkeypatch.setattr(time, "perf_counter", clock)
    panstats.frame_presented()
    clock.now = 0.4
    panstats.camera_changed()
    clock.now = 0.6
    # A startup gap is not a drag stall, even above the default threshold.
    assert panstats.check_stall(clock.now) is None
    assert sampler == []

    # After the ignore window, a 3 s gap with a warm camera is captured.
    clock.now = 5.0
    panstats.frame_presented()
    clock.now = 7.8
    panstats.camera_changed()
    clock.now = 8.0
    dump = tmp_path / "pan.jsonl.stall"
    assert panstats.check_stall(clock.now) == str(dump)
    assert "age_ms=3000.0" in dump.read_text(encoding="utf-8")
    assert len(sampler) == 1


def test_idle_camera_does_not_capture(monkeypatch, tmp_path, sampler):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock(5.0)
    monkeypatch.setattr(time, "perf_counter", clock)
    panstats.camera_changed()
    clock.now = 10.0
    panstats.frame_presented()
    clock.now = 10.6

    assert panstats.check_stall(clock.now) is None
    assert sampler == []
    assert not (tmp_path / "pan.jsonl.stall").exists()


def test_a_long_stall_captures_even_when_the_camera_event_is_old(
    monkeypatch, tmp_path, sampler
):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock(10.0)
    monkeypatch.setattr(time, "perf_counter", clock)
    panstats.frame_presented()
    clock.now = 10.4
    panstats.camera_changed()
    # Three seconds later the drag still has no frame.  The camera event is
    # old, but it arrived after the last frame, so this is a drag stall.
    clock.now = 13.0
    dump = tmp_path / "pan.jsonl.stall"
    assert panstats.check_stall(clock.now) == str(dump)
    assert "age_ms=3000.0" in dump.read_text(encoding="utf-8")
    assert "camera_ms=2600.0" in dump.read_text(encoding="utf-8")
    assert len(sampler) == 1


def test_second_episode_captures_again_then_caps(monkeypatch, tmp_path, sampler):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock(100.0)
    monkeypatch.setattr(time, "perf_counter", clock)

    def episode():
        # A frame lands, then the drag stalls while input keeps arriving: the
        # camera change is after the last frame, so the episode is a drag even
        # if the event is already old when the watchdog looks.
        clock.now += 1.0
        panstats.frame_presented()
        clock.now += 0.6
        panstats.camera_changed()
        return panstats.check_stall(clock.now)

    dump = tmp_path / "pan.jsonl.stall"
    assert episode() == str(dump)
    assert episode() == str(dump)
    assert dump.read_text(encoding="utf-8").count("panstats-stall:") == 2
    assert "dump=2/5" in dump.read_text(encoding="utf-8")

    for _ in range(panstats.PAN_STATS_STALL_DUMPS):
        episode()

    text = dump.read_text(encoding="utf-8")
    assert text.count("panstats-stall:") == panstats.PAN_STATS_STALL_DUMPS
    assert len(sampler) == panstats.PAN_STATS_STALL_DUMPS
    assert panstats._stall_dumps == panstats.PAN_STATS_STALL_DUMPS
    assert panstats.check_stall(clock.now + 1.0) is None


def test_missing_sample_still_writes_header(monkeypatch, tmp_path, sampler):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    monkeypatch.setattr(panstats.shutil, "which", lambda _name: None)
    clock = Clock(0.0)
    monkeypatch.setattr(time, "perf_counter", clock)
    panstats.frame_presented()
    clock.now = 0.4
    panstats.camera_changed()
    clock.now = 0.6
    dump = tmp_path / "pan.jsonl.stall"

    assert panstats.check_stall(clock.now) == str(dump)
    assert sampler == []
    header = dump.read_text(encoding="utf-8").splitlines()[0]
    assert "sample=missing" in header
    assert not (tmp_path / "pan.jsonl.stall.native").exists()


def test_stall_switch_writes_to_stderr(monkeypatch, capfd):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock(5.0)
    monkeypatch.setattr(time, "perf_counter", clock)
    panstats.frame_presented()
    clock.now = 5.4
    panstats.camera_changed()
    clock.now = 5.6

    assert panstats.check_stall(clock.now) == "stderr"
    _, err = capfd.readouterr()
    assert "panstats-stall: age_ms=600.0 camera_ms=200.0" in err
    assert "sample=unavailable" in err


def test_disabled_starts_no_watchdog_timer_or_file(monkeypatch, tmp_path, sampler):
    monkeypatch.setattr(time, "perf_counter", boom)
    window = FakeWindow(explode=True)

    assert panstats._start_watchdog() is None
    assert panstats._start_heartbeat_timer() is None
    assert panstats.ensure_connected(window) is None
    panstats.heartbeat()
    assert panstats.check_stall() is None
    assert panstats._watchdog is None
    assert panstats._heartbeat_timer is None
    assert sampler == []
    assert not (tmp_path / "pan.jsonl.stall").exists()


def test_watchdog_thread_is_daemon_and_stops(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    thread = panstats._start_watchdog()

    assert thread is not None
    assert thread.daemon is True
    assert thread.is_alive()
    assert panstats._start_watchdog() is thread

    panstats.shutdown()
    assert panstats._watchdog is None
    thread.join(2.0)
    assert not thread.is_alive()


def test_shutdown_terminates_live_sampler(monkeypatch, tmp_path):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock(0.0)
    monkeypatch.setattr(time, "perf_counter", clock)
    panstats.frame_presented()
    clock.now = 0.4
    panstats.camera_changed()
    clock.now = 0.6
    assert panstats.check_stall(clock.now) == "stderr"

    live = Sampler([panstats.SAMPLE_BIN], alive=True)
    panstats._sampler = live
    panstats.shutdown()

    assert live.terminated is True
    assert panstats._sampler is None
    assert panstats.shutdown() is None  # idempotent


def test_repaint_requests_counted_and_split_by_motion(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())  # 0.000
    panstats.frame_presented()  # first frame: no interval
    clock.now = 0.010
    panstats.repaint_requested()
    clock.now = 0.020
    panstats.repaint_requested()
    clock.now = 0.040
    panstats.frame_presented()  # 40 ms moving interval
    clock.now = 0.500
    panstats.repaint_requested()  # idle: the camera change is 500 ms old
    clock.now = 0.520
    panstats.repaint_requested()
    clock.now = 2.000

    record = panstats.summary()
    assert list(record) == FIELDS
    assert record["repaints"] == 4
    assert record["repaints_hz"] == 2.0
    assert record["repaints_moving"] == 2
    # Two moving requests over the 40 ms of moving-frame wall time.
    assert record["repaints_moving_hz"] == 50.0


def test_repaints_without_motion_have_no_moving_rate(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    clock.now = 1.000
    panstats.repaint_requested()
    clock.now = 2.000

    record = panstats.summary()
    assert record["repaints"] == 1
    assert record["repaints_hz"] == 1.0
    assert record["repaints_moving"] == 0
    assert record["repaints_moving_hz"] == 0.0


def test_hitch_carries_repaint_requests(monkeypatch, tmp_path):
    path = tmp_path / "pan.jsonl"
    monkeypatch.setenv(panstats.ENV_VAR, str(path))
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    panstats.camera_changed(object())
    clock.now = 0.005
    panstats.repaint_requested()  # before the previous frame: not in the gap
    clock.now = 0.010
    panstats.frame_presented()
    clock.now = 0.020
    panstats.repaint_requested()
    clock.now = 0.030
    panstats.repaint_requested()
    clock.now = 0.300
    panstats.frame_presented()  # 290 ms gap carrying the two later requests

    record = panstats.summary()
    assert record["hitches"] == 1
    assert record["repaints"] == 3

    panstats.flush()
    written = json.loads(path.read_text(encoding="utf-8").strip())
    hitch = written["hitch_records"][0]
    assert hitch["gap_ms"] == 290.0
    assert hitch["repaints"] == 2


def test_repaint_ring_is_bounded(monkeypatch):
    monkeypatch.setenv(panstats.ENV_VAR, "1")
    clock = Clock()
    monkeypatch.setattr(time, "perf_counter", clock)

    for step in range(panstats.SAMPLE_RING_SIZE * 2):
        clock.now = step * 0.001
        panstats.repaint_requested()

    assert panstats._repaints == panstats.SAMPLE_RING_SIZE * 2
    assert len(panstats._repaint_ring) == panstats.SAMPLE_RING_SIZE
