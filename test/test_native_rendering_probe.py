"""Deterministic input/frame attribution, without Qt or a physical display."""

import ast
from datetime import datetime
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "native_probe", ROOT / "tools/native_rendering_probe.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class TestNativeRenderRecorder(unittest.TestCase):
    def test_stale_render_cannot_acknowledge_new_input(self):
        recorder = probe.RenderRecorder()
        recorder.dispatch(1, 1.01, {"zoom": 1})
        recorder.submit(10, 0, 1.02, {"zoom": 1})
        recorder.synchronize()
        recorder.dispatch(1.03, 1.04, {"zoom": 2})
        recorder.submit(11, 1, 1.05, {"zoom": 2})
        recorder.rendered(1.06)
        result = recorder.result()
        self.assertEqual(result["inputs"][0]["status"], "observed")
        self.assertEqual(result["inputs"][1]["status"], "timeout_unobserved")
        self.assertEqual(result["callbacks"][0]["submission"]["sequence"], 10)
        self.assertFalse(result["presentation_latency_measured"])
        self.assertFalse(result["gpu_completion_measured"])

    def test_coalesced_inputs_remain_in_denominator(self):
        recorder = probe.RenderRecorder()
        for index in range(3):
            recorder.dispatch(index, index + 0.1, {})
        recorder.submit(20, 2, 2.2, {})
        recorder.synchronize()
        recorder.rendered(2.3)
        result = recorder.result()
        self.assertEqual(
            [i["status"] for i in result["inputs"]],
            ["superseded_unobserved", "superseded_unobserved", "observed"],
        )
        self.assertAlmostEqual(result["inputs"][2]["input_to_after_rendering_ms"], 200)
        self.assertAlmostEqual(result["inputs"][2]["dispatch_late_ms"], 100)

    def test_duplicate_callbacks_do_not_create_extra_input_samples(self):
        recorder = probe.RenderRecorder()
        recorder.dispatch(1, 1, {})
        recorder.submit(1, 0, 1.1, {})
        recorder.synchronize()
        recorder.rendered(1.2)
        recorder.rendered(1.3)
        result = recorder.result()
        self.assertEqual(len(result["inputs"]), 1)
        self.assertEqual(len(result["callbacks"]), 2)
        self.assertAlmostEqual(result["inputs"][0]["input_to_after_rendering_ms"], 200)

    def test_unattributed_and_missing_callbacks_are_not_success(self):
        recorder = probe.RenderRecorder()
        recorder.rendered(0.5)
        recorder.dispatch(1, 1, {})
        result = recorder.result()
        self.assertEqual(result["inputs"][0]["status"], "timeout_unobserved")
        self.assertIsNone(result["inputs"][0]["input_to_after_rendering_ms"])
        self.assertIsNone(result["callbacks"][0]["submission"])

    def test_out_of_order_callbacks_keep_earliest_timestamp(self):
        recorder = probe.RenderRecorder()
        recorder.dispatch(0.5, 1, {})
        recorder.submit(1, 0, 1.1, {}, {"base_image_key": 12})
        recorder.synchronize()
        recorder.rendered(1.3)
        recorder.rendered(1.2)
        result = recorder.result()
        self.assertAlmostEqual(result["inputs"][0]["input_to_after_rendering_ms"], 200)
        self.assertAlmostEqual(
            result["inputs"][0]["intended_to_after_rendering_ms"], 700
        )
        self.assertEqual(result["submissions"][0]["content"]["base_image_key"], 12)

    def test_callback_before_dispatch_does_not_acknowledge_it(self):
        recorder = probe.RenderRecorder()
        recorder.dispatch(1, 1, {})
        recorder.submit(1, 0, 1.1, {})
        recorder.synchronize()
        recorder.rendered(0.9)
        self.assertEqual(recorder.result()["inputs"][0]["status"], "timeout_unobserved")

    def test_probe_has_no_forced_paint_or_readback(self):
        tree = ast.parse((ROOT / "tools/native_rendering_probe.py").read_text())
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(calls & {"grab", "grabFramebuffer", "grabWindow", "_present"})

    def test_legacy_comparison_rejects_new_endpoint_schema(self):
        spec = importlib.util.spec_from_file_location(
            "benchmark_protocol", ROOT / "tools/benchmark_rendering.py"
        )
        protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(protocol)
        with self.assertRaisesRegex(ValueError, "schema_version"):
            protocol.compare_reports({"schema_version": 1}, {"schema_version": 2})


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Requires Qt event loop")
class TestNativeReplayAdapter(unittest.TestCase):
    def make_canvas(self, render=True):
        from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

        self.app = QCoreApplication.instance() or QCoreApplication([])

        class Signals(QObject):
            beforeSynchronizing = Signal()
            afterRendering = Signal()

        self.signals = Signals()
        image = SimpleNamespace(cacheKey=lambda: 1)
        scene = SimpleNamespace(frame=None, counters={"texture_uploads": 0})

        def present(frame):
            scene.frame = frame

        scene.present = present
        self.original_present = present
        canvas = SimpleNamespace(
            _center_lat=1,
            _center_lon=2,
            _zoom=3,
            time_control=SimpleNamespace(time=datetime(2026, 10, 16)),
            _gpu=SimpleNamespace(
                active=True,
                initialized=True,
                scene=scene,
                widget=SimpleNamespace(quickWindow=lambda: self.signals),
            ),
        )
        sequence = 0

        def update():
            nonlocal sequence
            sequence += 1
            frame = SimpleNamespace(
                sequence=sequence,
                base=SimpleNamespace(image=image),
                navigation=SimpleNamespace(image=image),
                raster_wind=None,
                wind=(),
            )

            def draw():
                scene.present(frame)
                self.signals.beforeSynchronizing.emit()
                self.signals.afterRendering.emit()

            if render:
                QTimer.singleShot(0, draw)

        canvas.update = update
        return canvas

    def config(self):
        return dict(
            scenario="stationary", frames=3, interval_ms=1, native_drain_timeout_s=0.01
        )

    def test_event_driven_replay_restores_adapter_without_grab_methods(self):
        canvas = self.make_canvas()
        result = probe.replay(canvas, self.config())
        self.assertEqual(len(result["inputs"]), 3)
        self.assertTrue(all(i["status"] == "observed" for i in result["inputs"]))
        self.assertIs(canvas._gpu.scene.present, self.original_present)
        self.assertEqual(result["gpu_counter_delta"], {"texture_uploads": 0})

    def test_missing_render_callbacks_exit_with_explicit_timeout_status(self):
        canvas = self.make_canvas(render=False)
        result = probe.replay(canvas, self.config())
        self.assertEqual(len(result["inputs"]), 3)
        self.assertTrue(
            all(i["status"] == "timeout_unobserved" for i in result["inputs"])
        )
        self.assertIs(canvas._gpu.scene.present, self.original_present)

    def test_optional_metadata_is_bound_to_submitted_frame(self):
        canvas = self.make_canvas()
        config = self.config()
        config["_frame_metadata"] = lambda _canvas: {"active": True, "symbols": 7}
        result = probe.replay(canvas, config)
        self.assertTrue(result["submissions"])
        for submission in result["submissions"]:
            self.assertEqual(
                submission["content"]["numerical_weather"],
                {"active": True, "symbols": 7},
            )

    def test_fallback_fails_and_restores_adapter(self):
        canvas = self.make_canvas(render=False)
        canvas.update = lambda: setattr(canvas._gpu, "active", False)
        with self.assertRaisesRegex(RuntimeError, "fallback"):
            probe.replay(canvas, self.config())
        self.assertIs(canvas._gpu.scene.present, self.original_present)


if __name__ == "__main__":
    unittest.main()
