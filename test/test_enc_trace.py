import json
from datetime import datetime

import pytest

from sailworks.qt import enc_trace


@pytest.fixture(autouse=True)
def reset_trace():
    enc_trace.reset()
    yield
    enc_trace.reset()


def test_unset_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.delenv(enc_trace.ENV_PATH, raising=False)
    monkeypatch.delenv(enc_trace.ENV_SLOW_MS, raising=False)
    enc_trace.reset()

    assert enc_trace.enabled() is False
    assert enc_trace.event("demand", lat=1.0) is None
    assert enc_trace.slow(reason="present_slow") is None
    target = tmp_path / "trace.jsonl"
    assert not target.exists()


def test_event_writes_one_flushed_json_line(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(target))
    enc_trace.reset()

    assert enc_trace.enabled() is True
    line = enc_trace.event(
        "scene_publish", generation=4, cells=7, assembly_ms=12.5, elapsed_ms=40.0
    )

    assert line is not None
    written = target.read_text(encoding="utf-8").splitlines()
    assert written == [line]
    record = json.loads(written[0])
    assert record["event"] == "scene_publish"
    assert record["generation"] == 4
    assert record["cells"] == 7
    assert record["assembly_ms"] == 12.5
    assert record["elapsed_ms"] == 40.0
    # ISO local timestamp with millisecond precision.
    parsed = datetime.fromisoformat(record["time"])
    assert parsed.microsecond % 1000 == 0


def test_second_event_appends(monkeypatch, tmp_path):
    target = tmp_path / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(target))
    enc_trace.reset()

    enc_trace.event("demand", zoom=13.0)
    enc_trace.slow(reason="present_slow", elapsed_ms=900.0)

    records = [
        json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event"] for record in records] == ["demand", "slow"]
    assert records[1]["reason"] == "present_slow"


def test_format_line_shape():
    line = enc_trace.format_line(
        "state", {"state": "ready", "generation": 3}, now=datetime(2026, 1, 2, 3, 4, 5)
    )
    record = json.loads(line)
    assert record == {
        "time": "2026-01-02T03:04:05.000",
        "event": "state",
        "state": "ready",
        "generation": 3,
    }


def test_is_slow_threshold(monkeypatch):
    monkeypatch.delenv(enc_trace.ENV_SLOW_MS, raising=False)
    enc_trace.reset()

    assert enc_trace.is_slow(None) is False
    assert enc_trace.is_slow(400.0) is False
    assert enc_trace.is_slow(500.0) is False
    assert enc_trace.is_slow(500.1) is True
    assert enc_trace.is_slow(700.0, limit_ms=1000.0) is False


def test_slow_ms_env_override(monkeypatch):
    monkeypatch.setenv(enc_trace.ENV_SLOW_MS, "120")
    enc_trace.reset()

    assert enc_trace.slow_ms() == 120.0
    assert enc_trace.is_slow(150.0) is True
    assert enc_trace.is_slow(100.0) is False


def test_slow_ms_env_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv(enc_trace.ENV_SLOW_MS, "not-a-number")
    enc_trace.reset()

    assert enc_trace.slow_ms() == enc_trace.DEFAULT_SLOW_MS


def test_present_timeout_decision():
    assert enc_trace.present_timeout(4000.0, False) is False
    assert enc_trace.present_timeout(5000.0, False) is True
    assert enc_trace.present_timeout(9000.0, True) is False
    assert enc_trace.present_timeout(None, False) is False


def test_dominant_phase():
    assert enc_trace.dominant_phase({"worker": 300.0, "chart": 20.0}) == "worker"
    assert enc_trace.dominant_phase({"worker": 300.0, "present": 40.0}) == "worker"
    assert enc_trace.dominant_phase({"worker": None, "chart": 12.0}) == "chart"
    assert enc_trace.dominant_phase({}) is None
    assert enc_trace.dominant_phase({"worker": 0.0}) is None


def test_phase_ms_converts_stage_seconds():
    phases = enc_trace.phase_ms(
        {"cell_open": 0.25, "assembly_ms": 1183.0, "cells": {"a": {}}, "bad": None},
        already_ms=("assembly_ms",),
        exclude=("cells",),
    )

    assert phases == {"cell_open": 250.0, "assembly_ms": 1183.0}
    # Without the exclusion the nested detail is dropped, never mis-converted.
    assert enc_trace.phase_ms({"cells": {"a": {}}, "clip": 0.002}) == {"clip": 2.0}


def test_build_phases_never_counts_acquire_twice():
    phases = enc_trace.build_phases(
        {"plan": 0.004, "cell_open": 0.9, "mesh": 0.95, "assemble": 0.12}
    )

    assert phases == {"plan": 4.0, "acquire": 900.0, "mesh": 50.0, "assemble": 120.0}
    # 0.9 + 0.05 + 0.12 + 0.004 < the 1183 ms the assembly measured.
    assert sum(phases.values()) <= 1183.0


def test_scene_publish_carries_phases_in_milliseconds(monkeypatch, tmp_path):
    target = tmp_path / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(target))
    enc_trace.reset()
    timings = {
        "plan": 0.004,
        "cell_open": 0.9,
        "mesh": 0.95,
        "assemble": 0.12,
        "assembly_ms": 1183.0,
        "cells": {"US5CA01M": {"open": 900.0, "mesh": 50.0}},
    }
    enc_trace.event(
        "scene_publish",
        assembly_ms=timings["assembly_ms"],
        phases=enc_trace.build_phases(timings),
        stage_ms=enc_trace.phase_ms(
            timings, already_ms=("assembly_ms",), exclude=("cells",)
        ),
        phase_cells=enc_trace.cell_detail(timings["cells"]),
    )

    record = json.loads(target.read_text(encoding="utf-8").splitlines()[-1])
    assert record["event"] == "scene_publish"
    assert record["phases"]["acquire"] == 900.0
    assert all(isinstance(value, float) for value in record["phases"].values())
    assert sum(record["phases"].values()) <= record["assembly_ms"]
    assert record["stage_ms"]["mesh"] == 950.0
    assert record["phase_cells"][0]["cell"] == "US5CA01M"


def test_cell_detail_is_bounded_and_heaviest_first():
    cells = {f"cell{index}": {"open": float(index)} for index in range(20)}

    rows = enc_trace.cell_detail(cells)

    assert len(rows) == enc_trace.CELL_DETAIL_LIMIT
    assert [row["ms"] for row in rows] == sorted(
        (row["ms"] for row in rows), reverse=True
    )
    assert rows[0]["cell"] == "cell19"


def test_tracing_off_records_no_phases(monkeypatch, tmp_path):
    monkeypatch.delenv(enc_trace.ENV_PATH, raising=False)
    enc_trace.reset()

    phases = enc_trace.build_phases({"plan": 0.001})
    assert enc_trace.event("scene_publish", phases=phases) is None
    assert not (tmp_path / "trace.jsonl").exists()


def test_camera_fields_rounds_identity():
    class Camera:
        center_lat = 48.150001234
        center_lon = -124.799999
        zoom = 12.99999
        width = 1200
        height = 800

    assert enc_trace.camera_fields(Camera()) == {
        "lat": 48.15,
        "lon": -124.8,
        "zoom": 13.0,
        "width": 1200,
        "height": 800,
    }
    assert enc_trace.camera_fields(None) == {}
