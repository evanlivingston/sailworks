"""The gate runner must normalize a timed-out child's captured streams."""

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace


def _runner():
    path = Path(__file__).resolve().parents[1] / "tools" / "gate_runner.py"
    spec = importlib.util.spec_from_file_location("timeout_gate_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_timeout_record_decodes_bytes_streams(monkeypatch, tmp_path):
    runner = _runner()
    gate = {
        "title": "timeout normalization",
        "milestone": "#0 test",
        "kind": "correctness",
        "tool": "tools/check_enc_viewport.py",
        "endpoint": "probe",
        "requires": [],
        "dependencies": [],
        "timeout_s": 5,
        "command": ["{python}", "-c", "pass"],
        "defaults": {"scenarios": ["one"], "frames": 1, "repeats": 1},
        "tool_output": "{outdir}/{gate}.tool.json",
        "result": "{outdir}/{gate}.tool.json",
        "reference": {"kind": "recorded-evidence", "source": "test"},
        "open_requirements": [],
    }
    monkeypatch.setitem(runner.gates.GATES, "timeout-test", gate)

    def timed_out(command, **_kwargs):
        raise subprocess.TimeoutExpired(
            command,
            5,
            output=b"stdout \xe2\x9c\x93",
            stderr=b"stderr bytes",
        )

    monkeypatch.setattr(runner.subprocess, "run", timed_out)
    args = SimpleNamespace(
        output=str(tmp_path),
        snapshot=None,
        full=False,
        frames=None,
        repeats=None,
        reference=None,
    )

    record = runner.run_gate("timeout-test", args)

    assert record["verdict"] == runner.VERDICT_NOT_EVALUATED
    assert record["checks"] == []
    log = (tmp_path / "timeout-test.tool.log").read_text(encoding="utf-8")
    assert log == "stdout \u2713stderr bytes"
