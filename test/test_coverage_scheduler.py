"""Bounded latest-view ownership, cancellation, and bundle fidelity checks."""

import json
from pathlib import Path
import sys
import tempfile
from threading import Event, Timer
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from coverage_scheduler import CoverageScheduler
import architecture_run_protocol as protocol
import experiment_vertical_renderer as renderer


class TestScheduler(unittest.TestCase):
    def test_new_request_discards_already_ready_older_result(self):
        release = Event()

        def prepare(request, cancel):
            if request.generation == 2:
                release.wait(2)
            return request.identity

        scheduler = CoverageScheduler(prepare)
        try:
            first = scheduler.request(("A",))
            with scheduler.condition:
                self.assertTrue(
                    scheduler.condition.wait_for(
                        lambda: scheduler.result is not None, 2
                    )
                )
            latest = scheduler.request(("B",))
            self.assertIsNone(scheduler.take())
            self.assertIn(("discarded-ready", first.generation), scheduler.events)
            release.set()
            self.assertEqual(self.wait_result(scheduler)[0], latest)
        finally:
            release.set()
            scheduler.close()

    def test_return_to_same_view_keeps_new_generation_and_discards_old_a(self):
        started, release = Event(), Event()

        def prepare(request, cancel):
            if request.generation == 1:
                started.set()
                release.wait(2)
            return request.identity

        scheduler = CoverageScheduler(prepare)
        try:
            old_a = scheduler.request(("A",))
            self.assertTrue(started.wait(2))
            scheduler.request(("B",))
            new_a = scheduler.request(("A",))
            self.assertEqual(old_a.identity, new_a.identity)
            release.set()
            request, value, error = self.wait_result(scheduler)
            self.assertEqual(request, new_a)
            self.assertNotEqual(request.generation, old_a.generation)
            self.assertIn(("discarded", old_a.generation), scheduler.events)
        finally:
            release.set()
            scheduler.close()

    def wait_result(self, scheduler):
        with scheduler.condition:
            self.assertTrue(
                scheduler.condition.wait_for(lambda: scheduler.result is not None, 2)
            )
        return scheduler.take()

    def test_one_active_one_replaceable_pending_and_stale_result_discard(self):
        started, release, cancelled = Event(), Event(), Event()
        calls = []

        def prepare(request, cancel):
            calls.append(request.generation)
            if request.generation == 1:
                started.set()
                self.assertTrue(release.wait(2))
                if cancel.is_set():
                    cancelled.set()
            return request.identity

        scheduler = CoverageScheduler(prepare)
        try:
            scheduler.request(("old",))
            self.assertTrue(started.wait(2))
            scheduler.request(("intermediate",))
            latest = scheduler.request(("latest",))
            release.set()
            request, value, error = self.wait_result(scheduler)
            self.assertEqual((request, value, error), (latest, ("latest",), None))
            self.assertEqual(calls, [1, 3])
            self.assertTrue(cancelled.is_set())
            self.assertIn(("discarded", 1), scheduler.events)
        finally:
            release.set()
            scheduler.close()

    def test_failure_is_explicit_not_empty_success(self):
        def prepare(request, cancel):
            raise ValueError("source/resource rejected")

        scheduler = CoverageScheduler(prepare)
        try:
            scheduler.request(("view",))
            _, value, error = self.wait_result(scheduler)
            self.assertIsNone(value)
            self.assertIn("source/resource rejected", error)
        finally:
            scheduler.close()

    def test_close_cancels_and_joins_active_work(self):
        started = Event()

        def prepare(request, cancel):
            started.set()
            self.assertTrue(cancel.wait(2))

        scheduler = CoverageScheduler(prepare)
        scheduler.request(("view",))
        self.assertTrue(started.wait(2))
        scheduler.close()
        self.assertFalse(scheduler.thread.is_alive())
        self.assertIsNone(scheduler.take())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            scheduler.request(("late",))

    def test_process_cancel_has_explicit_status_and_is_reaped(self):
        with tempfile.TemporaryDirectory() as temporary:
            cancel = Event()
            timer = Timer(0.1, cancel.set)
            timer.start()
            try:
                status = protocol.supervise(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    temporary,
                    protocol.environment(),
                    Path(temporary) / "cancelled",
                    timeout=5,
                    cancel=cancel,
                )
            finally:
                timer.cancel()
            self.assertTrue(status["cancelled"])
            self.assertFalse(status["timed_out"])
            self.assertIsNotNone(status["exit_code"])
            with self.assertRaises(ValueError):
                protocol.require_clean(status)


class TestBundle(unittest.TestCase):
    def test_empty_layer_suppression_never_hides_real_or_unknown_symbols(self):
        self.assertTrue(renderer.empty_layer_can_suppress(0, 0, [0, 0]))
        for independent, frame, finite in (
            (1, 0, [0, 0]),
            (0, 1, [0, 0]),
            (0, None, []),
            (0, 0, [1, 0]),
            (0, 0, [0, 1]),
        ):
            with self.subTest(independent=independent, frame=frame, finite=finite):
                self.assertFalse(
                    renderer.empty_layer_can_suppress(independent, frame, finite)
                )

    def test_empty_reference_requires_explicit_support_and_exact_no_vectors(self):
        empty = np.full((1, 2, 4), np.nan, dtype=np.float32)
        empty[..., 2:] = 1  # Anchors are finite, vectors are missing.
        with self.assertRaisesRegex(ValueError, "explicit empty-view"):
            renderer.selection_availability(0, 0, empty, empty)
        self.assertEqual(
            renderer.selection_availability(0, 0, empty, empty, allow_empty=True),
            "reference-empty",
        )
        for candidate, reference in ((0, 1), (1, 0), (2, 1)):
            with (
                self.subTest(candidate=candidate, reference=reference),
                self.assertRaisesRegex(ValueError, "count differs"),
            ):
                renderer.selection_availability(
                    candidate, reference, empty, empty, allow_empty=True
                )
        drawable = empty.copy()
        drawable[0, 0, :2] = (0, 0)  # Calm wind is valid, not missing.
        with self.assertRaisesRegex(ValueError, "drawable endpoint"):
            renderer.selection_availability(0, 0, empty, drawable, allow_empty=True)

    def test_heartbeat_summary_is_strict_json_serializable(self):
        summary = renderer.tick_gap_summary([0.0, 0.016, 0.033, 0.1])
        self.assertEqual(summary["count"], 3)
        json.dumps(summary, allow_nan=False)
        json.dumps(renderer.tick_gap_summary([]), allow_nan=False)

    def test_preparation_view_requires_exact_settled_geometry(self):
        calls = []
        canvas = SimpleNamespace(
            width=lambda: 710,
            height=lambda: 735,
            _zoom=10.25,
            _center_lat=36.0,
            _center_lon=-122.0,
            _invalidate_base=lambda: calls.append("base"),
            invalidate_navigation=lambda: calls.append("navigation"),
            update=lambda: calls.append("update"),
        )
        requested = dict(
            width=710, height=735, _zoom=10.25, _center_lat=36.0, _center_lon=-121.0
        )
        renderer.apply_preparation_view(canvas, requested)
        self.assertEqual(canvas._center_lon, -121.0)
        self.assertEqual(calls, ["base", "navigation", "update"])
        for key, value in (("width", 711), ("height", 734), ("_zoom", 10.0)):
            calls.clear()
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "actual=.*requested="),
            ):
                renderer.apply_preparation_view(canvas, dict(requested, **{key: value}))
            self.assertEqual(calls, [])

    def bundle(self, root):
        for name in ("background", "navigation"):
            np.save(root / (name + ".npy"), np.array([0, 128, 255], dtype=np.uint8))
        np.savez(
            root / "field.npz",
            points=np.array([[36.123456789, -122.123456789]], dtype=np.float64),
        )
        (root / "oracle.png").write_bytes(b"oracle")
        metadata = dict(snapshot_sha256="same", field_meta=[dict(file="field.npz")])
        metadata["files"] = {
            name: protocol.file_hash(root / name)
            for name in ("background.npy", "navigation.npy", "field.npz", "oracle.png")
        }
        (root / "resources.json").write_text(json.dumps(metadata))

    def test_load_preserves_image_bytes_and_coordinate_precision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.bundle(root)
            _, arrays, _ = renderer.read_bundle(
                root, expected=dict(snapshot_sha256="same")
            )
            self.assertEqual(arrays["background"].dtype, np.uint8)
            self.assertEqual(arrays["background"].tolist(), [0, 128, 255])
            self.assertEqual(arrays["field.npz"]["points"].dtype, np.float64)
            self.assertEqual(arrays["field.npz"]["points"][0, 0], 36.123456789)
            self.assertFalse(arrays["field.npz"]["points"].flags.writeable)

    def test_identity_checksum_and_budget_rejections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.bundle(root)
            with self.assertRaisesRegex(ValueError, "identity"):
                renderer.read_bundle(root, expected=dict(snapshot_sha256="other"))
            with self.assertRaisesRegex(ValueError, "budget"):
                renderer.read_bundle(root, byte_limit=1)
            (root / "oracle.png").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                renderer.read_bundle(root)


if __name__ == "__main__":
    unittest.main()
