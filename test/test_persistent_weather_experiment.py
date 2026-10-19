"""Generation, budget and interpolation controls for E2c's two-resource model."""

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np

SPEC = importlib.util.spec_from_file_location(
    "persistent_probe",
    Path(__file__).resolve().parents[1] / "tools/experiment_persistent_weather.py",
)
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class TestPersistentField(unittest.TestCase):
    def pair(self):
        field = probe.Field.create("one", [[1, 2], [3, 4]], [[5, 6], [7, 8]])
        values = probe.frozen([[[1, 10, 20, 0.25], [-1, 30, 40, 0.25]]])
        return field, probe.Selection("one", values)

    def test_indirection_and_empty_slot_keep_exact_anchors(self):
        field, selection = self.pair()
        actual = probe.reconstruct(field, selection)
        np.testing.assert_array_equal(actual[0, 0], [4, 5, 10, 20])
        self.assertTrue(np.isnan(actual[0, 1, :2]).all())
        np.testing.assert_array_equal(actual[0, 1, 2:], [30, 40])
        self.assertFalse(field.values.flags.writeable)
        self.assertFalse(selection.values.flags.writeable)

    def test_endpoint_does_not_consume_unused_nan(self):
        field = probe.Field.create(
            "one", [[1, 2], [np.nan, np.nan]], [[np.nan, np.nan], [3, 4]]
        )
        selection = probe.Selection(
            "one", probe.frozen([[[0, 0, 0, 0], [1, 0, 0, 1], [0, 0, 0, 0.5]]])
        )
        actual = probe.reconstruct(field, selection)
        np.testing.assert_array_equal(actual[0, :2, :2], [[1, 2], [3, 4]])
        self.assertTrue(np.isnan(actual[0, 2, :2]).all())

    def test_generation_change_cannot_bind_old_selection(self):
        field, selection = self.pair()
        with self.assertRaisesRegex(ValueError, "Stale"):
            probe.reconstruct(replace(field, generation="two"), selection)

    def test_budget_counts_replacement_and_output_overlap(self):
        field, selection = self.pair()
        needed = 2 * (field.values.nbytes + 2 * selection.values.nbytes)
        self.assertEqual(probe.validate_binding(field, selection, needed), needed)
        with self.assertRaisesRegex(ValueError, "reservation"):
            probe.validate_binding(field, selection, needed - 1)

    def test_bad_indices_padding_weights_and_nonfinite_anchors_reject(self):
        field, selection = self.pair()
        for channel, value in ((0, -2), (0, 2), (0, 0.5), (1, np.nan), (3, 1.1)):
            values = selection.values.copy()
            values[0, 0, channel] = value
            with (
                self.subTest(channel=channel, value=value),
                self.assertRaises(ValueError),
            ):
                probe.validate_binding(field, replace(selection, values=values))

    def test_field_copies_buffers_and_rejects_unbounded_vector(self):
        data = np.array([[1.0, 2.0]])
        field = probe.Field.create("one", data, data)
        data[:] = 99
        np.testing.assert_array_equal(field.values[0, 0], [1, 2, 1, 2])
        with self.assertRaisesRegex(ValueError, "Unbounded"):
            probe.Field.create("one", [[251, 0]], [[1, 1]])

    def test_selection_caps_before_land_filter_and_rejects_coverage_escape(self):
        points = np.array([[0.0, 0.0], [0.0, 20.0]])
        endpoints = np.ones((1, 2, 2), dtype=np.float32)
        retained = SimpleNamespace(
            covers=lambda _view: True,
            staged=SimpleNamespace(
                first=endpoints,
                second=endpoints,
                contains=lambda _t: True,
                weight=lambda _t: 0.5,
            ),
            lookup={(0.0, 0.0): 0, (0.0, 20.0): 1},
        )
        view = SimpleNamespace(
            width=20,
            height=20,
            density="dense",
            over_land=False,
            _center_lat=0,
            _center_lon=0,
            _zoom=1,
            get_scale=lambda: 1,
        )
        land = SimpleNamespace(
            refresh_source=lambda _scale: None, contains=lambda lat, lon: lon == 0
        )
        field = probe.Field.create("one", endpoints, endpoints)
        selected = probe.select(
            field,
            retained,
            1,
            view,
            points,
            land,
            lambda _density: (10, 1),
            lambda lat, lon, *_args: (lon, lat),
        )
        # Cap chooses first node, then shoreline removes it. Choosing next water
        # node instead would silently change the reference portrayal.
        self.assertEqual(np.count_nonzero(selected.values[..., 0] >= 0), 0)
        retained.covers = lambda _view: False
        with self.assertRaisesRegex(ValueError, "escape"):
            probe.select(field, retained, 1, view, points, land, None, None)


if __name__ == "__main__":
    unittest.main()
