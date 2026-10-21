"""N1 native gate controls, without creating a window or graphics context."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_field_upload_attribution_requires_warm_and_reload_positives():
    tool = load("check_native_weather_store")
    handles = [
        [1, 2, 3, 4],
        [5, 6, 7, 8],
        [9, 10, 11, 12],
        [1, 2, 3, 4],
        [13, 14, 15, 16],
        [17, 18, 19, 20],
    ]
    phases = [
        {"began": 100 + i * 2, "ended": 101 + i * 2, "group": {"handles": group}}
        for i, group in enumerate(handles)
    ]
    events = [{"event": "configured"}] + [
        {"event": "uploadTexture", "texture": texture, "time": 100.5 + i * 2}
        for i, group in enumerate(handles)
        if i != 3
        for texture in group
    ]
    trace = tool.classify_trace(events, {"phases": phases})
    assert (
        trace["warm_positive"]
        and trace["warm_return_zero"]
        and trace["reload_positive"]
    )
    assert not tool.classify_trace([], {"phases": phases})["warm_positive"]
    events.append({"event": "uploadTexture", "texture": 1, "time": 106.5})
    assert not tool.classify_trace(events, {"phases": phases})["warm_return_zero"]


def test_second_map_failure_is_not_reported_as_cache_acceptance():
    tool = load("check_native_weather_store")
    phases = [
        {"began": 100, "ended": 101, "group": None},
        {"began": 102, "ended": 103, "group": None},
    ]
    trace = tool.classify_trace([], {"phases": phases})
    assert not trace["warm_positive"]
    assert not trace["warm_return_zero"]
    assert not trace["reload_positive"]


def test_native_store_gate_requires_every_case_not_only_clean_shutdown():
    runner = load("gate_runner")
    gate = runner.gates.GATES["native-field-store"]
    report = {
        "checks": {check["metric"].split(".")[1]: True for check in gate["checks"]}
    }
    assert all(result["verdict"] == "pass" for result in runner.evaluate(gate, report))
    report["checks"]["two_maps_draw"] = False
    assert any(result["verdict"] == "fail" for result in runner.evaluate(gate, report))
    assert any(
        result["verdict"] == "fail"
        for result in runner.evaluate(gate, {"checks": {"clean_native_exit": True}})
    )


def test_cached_shader_metadata_does_not_prevent_known_pixel_readback():
    tool = load("check_native_weather_store")
    group = {"handles": [1, 2, 3, 4], "shader_compiled": False}
    assert tool.ready_for_readback(True, group, False)
    # This only permits independent pixel validation, not gate success.
    runner = load("gate_runner")
    results = runner.evaluate(
        runner.gates.GATES["native-field-store"],
        {"checks": {"two_maps_draw": False, "clean_native_exit": True}},
    )
    assert any(result["verdict"] == "fail" for result in results)


@pytest.mark.parametrize(
    "ready,group,failed",
    [
        (False, {"handles": [1, 2, 3, 4]}, False),
        (True, None, False),
        (True, {"handles": [1, None, 3, 4]}, False),
        (True, {"handles": []}, False),
        (True, {"handles": [1, 2, 3, 4]}, True),
    ],
)
def test_native_readback_still_rejects_missing_resources_and_shader_errors(
    ready, group, failed
):
    assert not load("check_native_weather_store").ready_for_readback(
        ready, group, failed
    )
