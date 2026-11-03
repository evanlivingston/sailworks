import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "startup_benchmark_probe_test",
    Path(__file__).resolve().parents[1] / "tools/benchmark_startup_rendering.py",
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_missing_native_probe_has_actionable_error():
    with pytest.raises(RuntimeError, match="Qt viewport replay path"):
        benchmark.require_native_probe({})
    probe = {"inputs": []}
    assert benchmark.require_native_probe({"native_render_probe": probe}) is probe
