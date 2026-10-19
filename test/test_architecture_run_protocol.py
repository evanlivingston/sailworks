"""Whole-path evidence must include actual child outcome and matched inputs."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import architecture_run_protocol as protocol
import compare_architecture_runs as compare


class TestSupervision(unittest.TestCase):
    def test_persist_zero_and_nonzero_exit_independently_of_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for code in (0, 3):
                prefix = root / f"child-{code}"
                command = [
                    sys.executable,
                    "-c",
                    f"print('child output'); raise SystemExit({code})",
                ]
                status = protocol.supervise(command, root, dict(os.environ), prefix)
                self.assertEqual(status["exit_code"], code)
                self.assertEqual(
                    json.loads(prefix.with_suffix(".status.json").read_text())[
                        "exit_code"
                    ],
                    code,
                )
                if code:
                    with self.assertRaisesRegex(ValueError, "cleanly"):
                        protocol.require_clean(status)
                else:
                    protocol.require_clean(status)

    def test_timeout_is_explicit_and_child_is_reaped(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = protocol.supervise(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                temporary,
                dict(os.environ),
                Path(temporary) / "timeout",
                timeout=0.05,
            )
            self.assertTrue(status["timed_out"])
            self.assertIsNotNone(status["exit_code"])
            with self.assertRaisesRegex(ValueError, "cleanly"):
                protocol.require_clean(status)

    def test_unknown_status_cannot_be_inferred_success(self):
        with self.assertRaisesRegex(ValueError, "cleanly"):
            protocol.require_clean(
                dict(exit_code=None, timed_out=False, exception=None)
            )


class TestPairGate(unittest.TestCase):
    def pair(self):
        host = dict(backend="Metal", dpr=2, samples=4, alpha_bits=8)
        host["qt_platform"] = "cocoa"
        prime = dict(
            protocol="equal-native-event-loop-priming-v1",
            requested_ms=2000,
            gui_tick_seconds=list(range(32)),
            render_callback_seconds=list(range(32)),
            effective_render_target=dict(samples=4, width=1420, height=1470, dpr=2),
        )
        clocks = [f"2026-10-16T08:{k:02d}:00" for k in range(12)]
        inputs = [
            dict(revision=i, state={"longitude": i, "forecast_time": clocks[i % 12]})
            for i in range(24)
        ]
        reference = dict(
            native_fidelity=dict(host=host, priming=prime),
            native_render_probe=dict(inputs=inputs),
        )
        candidate = dict(
            host=deepcopy(host),
            priming=deepcopy(prime),
            static_complete_pixels_exact=True,
            nonblank_guard_passed=True,
            native_render_probe=dict(inputs=deepcopy(inputs)),
        )
        status = dict(exit_code=0, timed_out=False, exception=None)
        resources = dict.fromkeys(
            (
                "snapshot_sha256",
                "view",
                "targets",
                "dpr",
                "opacity",
                "sea_color",
            ),
            "same",
        )
        resources["clocks"] = clocks
        resources["field_meta"] = [
            dict(
                step=k,
                before=k,
                after=k + 1,
                generation=f"gen-{k}",
                node_count=1000,
                file=f"field_{k:03d}.npz",
            )
            for k in range(12)
        ]
        resources["files"] = dict.fromkeys(
            ("background.npy", "navigation.npy", "oracle.png"), "same"
        )
        resources["files"].update({f"field_{k:03d}.npz": "same" for k in range(12)})
        return [
            reference,
            candidate,
            status,
            deepcopy(status),
            resources,
            deepcopy(resources),
        ]

    def test_matches_complete_pair(self):
        compare.check_pair(*self.pair())

    def test_camera_mismatch_rejected_even_with_matching_times(self):
        values = self.pair()
        values[1]["native_render_probe"]["inputs"][3]["state"]["longitude"] += 1
        with self.assertRaisesRegex(ValueError, "trajectories differ"):
            compare.check_pair(*values)

    def test_unavailable_only_cannot_pass_as_cold_load_performance(self):
        values = self.pair()
        values[1]["scenario"] = "coverage_miss"
        values[1]["coverage_miss_steps"] = [1, 2, 3]
        with self.assertRaisesRegex(ValueError, "safety-only"):
            compare.check_pair(*values)

    def test_bad_process_host_priming_or_content_cannot_pass(self):
        for fault in ("exit", "host", "prime", "content", "trajectory", "missing"):
            values = self.pair()
            if fault == "exit":
                values[3]["exit_code"] = 1
            elif fault == "host":
                values[1]["host"]["backend"] = "OpenGL"
            elif fault == "prime":
                values[1]["priming"]["gui_tick_seconds"] = []
            elif fault == "content":
                values[5]["files"]["oracle.png"] = "different"
            elif fault == "trajectory":
                values[1]["native_render_probe"]["inputs"][0]["state"] = {}
            else:
                values[1]["native_render_probe"]["inputs"].pop()
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                compare.check_pair(*values)


if __name__ == "__main__":
    unittest.main()
