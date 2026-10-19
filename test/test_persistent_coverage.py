"""Persistent mailbox service reuses ownership and explicitly rejects errors."""

import json
from pathlib import Path
import sys
import tempfile
from threading import Thread
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from persistent_coverage import atomic_json, serve_requests


class TestPersistentService(unittest.TestCase):
    def test_fast_service_defers_grabs_and_oracles_until_validation_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = []
            app = SimpleNamespace(processEvents=lambda: None)

            def timed_export(*args, **kwargs):
                events.append("timed-export")
                return dict(oracle_pending=True, exact_reference_selections=0)

            def oracle_export(*args, **kwargs):
                events.append("oracle")
                return dict(exact_reference_selections=25)

            thread = Thread(
                target=lambda: serve_requests(
                    object(),
                    app,
                    lambda: None,
                    lambda: events.append("grab"),
                    dict(service_directory=str(root), snapshot_sha256="same"),
                    timed_export,
                    lambda canvas, view: None,
                    validation_export=oracle_export,
                    request_frame=lambda: events.append("frame"),
                )
            )
            thread.start()
            try:
                self.wait_file(root / "ready.json")
                atomic_json(root / "request.json", dict(generation=1, view={}, stamp=1))
                self.assertIsNone(self.wait_file(root / "response-1.json")["error"])
                self.assertNotIn("grab", events)
                self.assertNotIn("oracle", events)
                atomic_json(root / "validate.json", dict(generations=[1]))
                checked = self.wait_file(root / "validation-result.json")
                self.assertIsNone(checked["completed"][0]["error"])
                self.assertEqual(events.count("grab"), 2)
                self.assertEqual(events.count("oracle"), 1)
            finally:
                atomic_json(root / "stop.json", dict(stop=True))
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def wait_file(self, path):
        deadline = time.monotonic() + 2
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(path.exists(), str(path))
        return json.loads(path.read_text())

    def test_reuses_service_for_latest_requests_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls, outputs = [], []
            app = SimpleNamespace(processEvents=lambda: None)
            canvas = SimpleNamespace(view=None)

            def apply(canvas, view):
                if view.get("bad"):
                    raise ValueError("viewport identity rejected")
                canvas.view = view

            def export(canvas, app, directory, snapshot, stamps):
                calls.append((canvas.view, stamps, snapshot))
                return dict(exact=True)

            def run():
                outputs.append(
                    serve_requests(
                        canvas,
                        app,
                        lambda: None,
                        lambda: None,
                        dict(service_directory=str(root), snapshot_sha256="same"),
                        export,
                        apply,
                    )
                )

            thread = Thread(target=run)
            thread.start()
            try:
                ready = self.wait_file(root / "ready.json")
                for generation, view in (
                    (1, {"x": 10}),
                    (3, {"x": 20}),
                    (4, {"bad": True}),
                ):
                    atomic_json(
                        root / "request.json",
                        dict(generation=generation, view=view, stamp=1),
                    )
                    response = self.wait_file(root / f"response-{generation}.json")
                    self.assertEqual(response["pid"], ready["pid"])
                    self.assertEqual(response["generation"], generation)
                    if generation == 4:
                        self.assertIn("identity rejected", response["error"])
                    else:
                        self.assertIsNone(response["error"])
                        self.assertEqual(
                            set(response["phase_seconds"]),
                            {
                                "apply_view_seconds",
                                "request_and_first_readback_seconds",
                                "overlay_settle_seconds",
                                "settled_readback_seconds",
                                "export_and_numerical_oracle_seconds",
                            },
                        )
                        self.assertLessEqual(
                            sum(response["phase_seconds"].values()),
                            response["prepare_seconds"],
                        )
                self.assertEqual(len(calls), 2)
            finally:
                atomic_json(root / "stop.json", dict(stop=True))
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outputs[0]["completed"]), 3)

    def test_atomic_mailbox_contains_only_latest_complete_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request.json"
            atomic_json(path, dict(generation=1))
            atomic_json(path, dict(generation=2))
            self.assertEqual(json.loads(path.read_text()), dict(generation=2))
            self.assertFalse(path.with_suffix(".pending").exists())


if __name__ == "__main__":
    unittest.main()
