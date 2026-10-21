"""Headless checks of probe oracles and fail-closed gate behavior."""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "key,pixel", [("A", [255, 64, 128, 255]), ("B", [128, 64, 128, 255])]
)
def test_pixel_control_requires_known_finite_content(key, pixel):
    probe = _load("experiment_resident_texture_item")
    pixels = np.tile(np.array(pixel, dtype=np.uint8), (3, 4, 1))
    assert probe.pixel_check(pixels, key)
    assert not probe.pixel_check(np.zeros_like(pixels), key)
    pixels[1, 1, 0] = 10
    assert not probe.pixel_check(pixels, key)


def test_missing_control_requires_zero_alpha_everywhere():
    probe = _load("experiment_resident_texture_item")
    pixels = np.zeros((3, 4, 4), dtype=np.uint8)
    assert probe.pixel_check(pixels, "missing")
    pixels[1, 1, 3] = 1
    assert not probe.pixel_check(pixels, "missing")
    assert not probe.pixel_check(pixels[:0], "missing")


def test_upload_trace_requires_warm_positive_and_detects_return_upload():
    probe = _load("experiment_resident_texture_item")
    phases = [
        {"began": 100, "ended": 101, "rhi": 11},
        {"began": 102, "ended": 103, "rhi": 22},
        {"began": 104, "ended": 105, "rhi": 11},
    ]
    events = [
        {"event": "configured"},
        {"event": "uploadTexture", "time": 100.5, "texture": 11},
        {"event": "uploadTexture", "time": 102.5, "texture": 22},
    ]
    assert probe.trace_checks(events, phases)["warm_positive"]
    assert probe.trace_checks(events, phases)["return_to_A_zero"]
    assert not probe.trace_checks([], phases)["warm_positive"]
    events.append({"event": "uploadTexture", "time": 104.5, "texture": 11})
    assert not probe.trace_checks(events, phases)["return_to_A_zero"]


def test_compatibility_gate_requires_every_declared_check():
    runner = _load("gate_runner")
    gate = runner.gates.GATES["native-field-item"]
    report = {
        "checks": {check["metric"].split(".")[1]: True for check in gate["checks"]}
    }
    assert len(gate["checks"]) == 19
    assert all(result["verdict"] == "pass" for result in runner.evaluate(gate, report))
    for check in gate["checks"]:
        name = check["metric"].split(".")[1]
        report["checks"][name] = False
        assert any(
            result["verdict"] == "fail" for result in runner.evaluate(gate, report)
        )
        report["checks"][name] = True
    assert all(result["verdict"] == "fail" for result in runner.evaluate(gate, {}))


def test_public_adoption_survives_former_parent_deletion_until_explicit_release():
    from PySide6.QtCore import QObject
    import shiboken6

    probe = _load("experiment_resident_texture_item")
    parent = QObject()
    child = QObject(parent)
    destroyed = []
    child.destroyed.connect(lambda: destroyed.append(True))
    adoption = probe.adopt_texture(child)
    assert not adoption["before"]["native_parent_is_none"]
    assert adoption["after"]["valid"]
    assert adoption["after"]["python_owned"]
    assert child.parent() is None
    shiboken6.delete(parent)
    assert shiboken6.isValid(child)
    assert not destroyed
    shiboken6.delete(child)
    assert destroyed == [True]


def test_public_adoption_refuses_an_already_invalid_wrapper():
    from PySide6.QtCore import QObject
    import shiboken6

    probe = _load("experiment_resident_texture_item")
    child = QObject()
    shiboken6.delete(child)
    with pytest.raises(RuntimeError, match="cannot adopt an invalid"):
        probe.adopt_texture(child)
