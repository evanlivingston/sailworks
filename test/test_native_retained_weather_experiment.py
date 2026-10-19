"""Native control comparisons must preserve input attribution and denominators."""

from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

SPEC = importlib.util.spec_from_file_location(
    "native_retained_probe",
    Path(__file__).resolve().parents[1] / "tools/experiment_native_retained_weather.py",
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def callback(revision, stamp, active):
    return dict(
        after_rendering=stamp,
        submission=dict(
            revision=revision, content=dict(numerical_weather=dict(active=active))
        ),
    )


def run():
    return dict(
        startup={"forecasts": 33},
        actual_viewport=[100, 100],
        native_render_probe=dict(
            inputs=[
                dict(
                    revision=i,
                    dispatched=i + 1,
                    state={"longitude": i},
                    dispatch_late_ms=0,
                )
                for i in range(2)
            ],
            callbacks=[
                callback(0, 1.01, False),
                callback(0, 1.02, True),
                callback(1, 2.05, True),
                callback(1, 3, True),
            ],
            gpu_counter_delta={},
        ),
        retained_control_events=[
            dict(kind="field_build", at=0.5),
            dict(kind="verified_field", at=0.6),
        ],
    )


class TestNativeRetainedComparison(unittest.TestCase):
    def reports(self):
        reference = dict(
            protocol="native-retained-field-e2b-v1",
            control="reference",
            workload=dict(interval_ms=100, frames=2),
            environment={},
            app_identity={},
            harness_identity={},
            run=run(),
        )
        candidate = deepcopy(reference)
        candidate["control"] = "retained"
        return reference, candidate

    def test_input_phase_excludes_drain_and_keeps_first_any_separate(self):
        observed = probe.observations(run(), 100)
        self.assertEqual(observed["inputs"], 2)
        self.assertEqual(observed["any_rendered_revisions"], 2)
        self.assertEqual(observed["numerical_rendered_revisions"], 2)
        self.assertEqual(observed["input_phase_callbacks"], 3)
        self.assertEqual(observed["total_callbacks_including_drain"], 4)
        self.assertGreater(
            observed["first_numerical_render_ms"]["p50_ms"],
            observed["first_any_render_ms"]["p50_ms"],
        )
        self.assertFalse(observed["presentation_measured"])

    def test_old_render_cannot_acknowledge_latest_input(self):
        case = run()
        case["native_render_probe"]["callbacks"] = [callback(0, 2.05, True)]
        observed = probe.observations(case, 100)
        self.assertEqual(observed["inputs"], 2)
        self.assertEqual(observed["any_rendered_revisions"], 1)
        self.assertEqual(observed["numerical_rendered_revisions"], 1)

    def test_matched_control_is_not_an_end_to_end_speedup_claim(self):
        compared = probe.compare(*self.reports())
        self.assertTrue(compared["trajectory_match"])
        self.assertFalse(compared["end_to_end_speedup_proven"])

    def test_changed_trajectory_and_app_identity_are_rejected(self):
        reference, candidate = self.reports()
        candidate["run"]["native_render_probe"]["inputs"][0]["state"]["longitude"] = 10
        with self.assertRaisesRegex(ValueError, "trajectory"):
            probe.compare(reference, candidate)
        reference, candidate = self.reports()
        candidate["app_identity"] = {"commit": "different"}
        with self.assertRaisesRegex(ValueError, "app_identity"):
            probe.compare(reference, candidate)

    def test_missing_inputs_and_warmup_failure_are_rejected(self):
        reference, candidate = self.reports()
        candidate["run"]["native_render_probe"]["inputs"].pop()
        with self.assertRaisesRegex(ValueError, "input stream"):
            probe.compare(reference, candidate)
        reference, candidate = self.reports()
        candidate["run"]["retained_control_events"].append(
            dict(kind="field_build", at=1.5)
        )
        with self.assertRaisesRegex(ValueError, "not warm"):
            probe.compare(reference, candidate)

    def test_numerical_block_is_private_and_restored(self):
        side, array = lambda: "side", lambda: "array"
        index = SimpleNamespace(array=array)
        sampler = SimpleNamespace(_side=side, indices=[index])
        with probe.unavailable_numerics(sampler):
            with self.assertRaisesRegex(AssertionError, "numerical preparation"):
                sampler._side()
            with self.assertRaisesRegex(AssertionError, "numerical preparation"):
                index.array()
        self.assertIs(sampler._side, side)
        self.assertIs(index.array, array)


if __name__ == "__main__":
    unittest.main()
