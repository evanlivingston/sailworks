"""The #64 mechanism gate must reject missing data and nonzero reuse costs."""

import importlib.util
from pathlib import Path

import pytest


def _runner():
    path = Path(__file__).resolve().parents[1] / "tools" / "gate_runner.py"
    spec = importlib.util.spec_from_file_location("residency_gate_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report():
    return {
        "inputs": [
            {"covered_before_input": True, "ready": True, "same_field": True}
            for _ in range(4)
        ],
        "field_preparations": 0,
        "misses": 0,
        "decoded_band_accesses": 0,
        "field_publications": 0,
        "selection_publications": 4,
        "retained_bytes": 1024,
        "texture_probe_ok": True,
        "new_field_textures": 0,
        "source_retirement": {
            "old_weather_hidden": True,
            "provider_bytes": 0,
            "live_textures": 0,
        },
        "source_reload_ready": True,
        "teardown": {
            "workers_stopped": True,
            "retained_field_bytes": 0,
            "textures": {"live_textures": 0},
        },
    }


def test_reuse_gate_accepts_the_mechanism_without_claiming_gpu_acceptance():
    runner = _runner()
    gate = runner.gates.GATES["residency-reuse"]
    assert all(
        result["verdict"] == "pass" for result in runner.evaluate(gate, _report())
    )
    assert gate["open_requirements"]
    assert (
        runner.gates.GATES["residency"]["tool"]
        == "tools/benchmark_startup_rendering.py"
    )


@pytest.mark.parametrize(
    "metric,value",
    [
        ("field_preparations", 1),
        ("misses", 1),
        ("decoded_band_accesses", 1),
        ("field_publications", 1),
        ("new_field_textures", 1),
        ("texture_probe_ok", False),
        ("selection_publications", 0),
        ("retained_bytes", 134217729),
        ("inputs", []),
    ],
)
def test_reuse_gate_rejects_nonzero_costs_and_vacuous_success(metric, value):
    runner = _runner()
    report = _report()
    report[metric] = value
    assert any(
        result["verdict"] == "fail"
        for result in runner.evaluate(runner.gates.GATES["residency-reuse"], report)
    )


def test_upload_classifier_rejects_wrong_clocks_and_shared_atlases():
    path = (
        Path(__file__).resolve().parents[1] / "tools" / "check_resident_field_reuse.py"
    )
    spec = importlib.util.spec_from_file_location("resident_reuse_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = {
        "textures_before": {
            "rhi_field_pointers": [1, 2, 3, 4],
            "field_atlas_observed": False,
        },
        "initialization_window": [1000, 1010],
        "input_window": [1010, 1020],
    }
    events = (
        [{"event": "configured"}]
        + [
            {"event": "uploadTexture", "texture": texture, "time": 1001}
            for texture in (1, 2, 3, 4)
        ]
        + [{"event": "uploadTexture", "texture": 5, "time": 1015}]
    )
    trace = module.classify_uploads(events, report)
    assert trace["warm_field_submissions"] == 4
    assert trace["input_all_submissions"] == 1
    assert trace["input_field_submissions"] == 0
    assert trace["field_resources_identifiable"]
    # A real field re-upload must not be mistaken for selection traffic.
    events.append({"event": "uploadTexture", "texture": 1, "time": 1016})
    assert module.classify_uploads(events, report)["input_field_submissions"] == 1
    wrong_clock = [
        {**event, "time": 5} if event["event"] == "uploadTexture" else event
        for event in events
    ]
    assert module.classify_uploads(wrong_clock, report)["warm_field_submissions"] == 0
    report["textures_before"]["field_atlas_observed"] = True
    assert not module.classify_uploads(events, report)["field_resources_identifiable"]


@pytest.mark.parametrize(
    "metric,value",
    [
        ("warm_field_submissions", 0),
        ("input_all_submissions", 0),
        ("input_field_submissions", 1),
        ("configured", False),
        ("field_resources_identifiable", False),
    ],
)
def test_upload_gate_rejects_inactive_or_ambiguous_tracing(metric, value):
    runner = _runner()
    report = _report()
    report["upload_trace"] = {
        "configured": True,
        "field_resources_identifiable": True,
        "warm_field_submissions": 4,
        "input_field_submissions": 0,
        "input_all_submissions": 9,
    }
    gate = runner.gates.GATES["residency-upload"]
    assert all(result["verdict"] == "pass" for result in runner.evaluate(gate, report))
    report["upload_trace"][metric] = value
    assert any(result["verdict"] == "fail" for result in runner.evaluate(gate, report))
