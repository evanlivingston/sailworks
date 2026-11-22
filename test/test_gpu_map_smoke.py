"""The GPU map smoke must fail on a CPU fallback, not report it as a pass."""

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools/check_gpu_map_smoke.py"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("check_gpu_map_smoke", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hardware_status_is_a_gpu_result(smoke):
    assert smoke.status_kind("GPU: OpenGL") == "gpu"
    assert smoke.status_kind("GPU: Metal, weather Metal") == "gpu"


def test_waiting_for_a_gpu_is_not_a_gpu_result(smoke):
    assert smoke.status_kind("GPU requested; waiting for a visible map") == "pending"


def test_cpu_fallback_is_its_own_failure(smoke):
    assert smoke.status_kind("CPU fallback: no hardware backend") == "fallback"
    assert smoke.status_kind("CPU fallback: shader needs hardware") == "fallback"


def test_unavailable_and_pending_are_not_gpu(smoke):
    assert smoke.status_kind("CPU") == "unavailable"
    assert smoke.status_kind("Viewport unavailable: no display") == "unavailable"
    assert smoke.status_kind("") == "pending"
    assert smoke.status_kind("pending") == "pending"


def test_main_passes_only_when_a_gpu_frame_presented(smoke, monkeypatch):
    monkeypatch.setattr(
        smoke,
        "run",
        lambda timeout, report: {
            "kind": "gpu",
            "renderer_status": "GPU (weather OpenGL)",
            "presented": True,
        },
    )
    assert smoke.main([]) == 0

    monkeypatch.setattr(
        smoke,
        "run",
        lambda timeout, report: {
            "kind": "gpu",
            "renderer_status": "GPU (weather OpenGL)",
            "presented": False,
        },
    )
    assert smoke.main([]) == 1

    monkeypatch.setattr(
        smoke,
        "run",
        lambda timeout, report: {
            "kind": "fallback",
            "renderer_status": "CPU fallback: no hardware backend",
            "presented": True,
        },
    )
    assert smoke.main([]) == 1
