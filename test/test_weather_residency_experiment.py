"""Experiment-only cache adapter and exact-query comparison safeguards."""

from collections import OrderedDict
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

SPEC = importlib.util.spec_from_file_location(
    "residency",
    Path(__file__).resolve().parents[1] / "tools/experiment_weather_residency.py",
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class FakeIndex:
    width, height = 2, 2

    def __init__(self):
        self.arrays = OrderedDict()
        self.reads = 0

    def array(self, record):
        value = self.arrays.get(record.band)
        if value is None:
            self.reads += 1
            value = np.full((2, 2), record.band, dtype=np.float64)
            self.arrays[record.band] = value
            if len(self.arrays) > 6:
                self.arrays.popitem(last=False)
        self.arrays.move_to_end(record.band)
        return value


class TestBandBudget(unittest.TestCase):
    def test_global_eviction_and_hits_across_sources(self):
        budget = probe.BandBudget(64)
        first, second = FakeIndex(), FakeIndex()
        probe.install_budget(first, budget, 0)
        probe.install_budget(second, budget, 1)
        first.array(SimpleNamespace(band=1))
        second.array(SimpleNamespace(band=1))
        first.array(SimpleNamespace(band=1))
        second.array(SimpleNamespace(band=2))
        self.assertIn(1, first.arrays)
        self.assertNotIn(1, second.arrays)
        self.assertIn(2, second.arrays)
        self.assertEqual(budget.bytes, 64)
        self.assertEqual(budget.peak, 64)
        self.assertEqual(budget.evictions, 1)
        self.assertEqual(budget.hits, 1)
        self.assertEqual(budget.loads, 3)

    def test_oversized_band_rejected_before_read(self):
        index = FakeIndex()
        budget = probe.BandBudget(31)
        probe.install_budget(index, budget, 0)
        with self.assertRaisesRegex(
            probe.ResidencyLimitExceeded, "decoded band exceeds"
        ):
            index.array(SimpleNamespace(band=1))
        self.assertEqual(index.reads, 0)
        self.assertEqual(budget.bytes, 0)

    def test_reference_stops_instead_of_evicting_or_changing_sources(self):
        index = FakeIndex()
        budget = probe.BandBudget(32, evict=False)
        probe.install_budget(index, budget, 0)
        index.array(SimpleNamespace(band=1))
        with self.assertRaisesRegex(probe.ResidencyLimitExceeded, "safety stop"):
            index.array(SimpleNamespace(band=2))
        self.assertEqual(index.reads, 1)
        self.assertEqual(budget.evictions, 0)

    def test_per_source_six_band_eviction_remains_consistent(self):
        index = FakeIndex()
        budget = probe.BandBudget(320)
        probe.install_budget(index, budget, 0)
        for band in range(8):
            index.array(SimpleNamespace(band=band))
        self.assertEqual(len(index.arrays), 6)
        self.assertEqual(budget.bytes, 192)
        self.assertNotIn(0, index.arrays)
        self.assertIn(7, index.arrays)
        index.arrays.clear()
        self.assertEqual(budget.bytes, 0)

    def test_reloaded_values_match_original_without_warm_cache(self):
        reference, candidate = FakeIndex(), FakeIndex()
        probe.install_budget(candidate, probe.BandBudget(32), 0)
        for band in (1, 2, 1, 7, 2):
            record = SimpleNamespace(band=band)
            np.testing.assert_array_equal(
                reference.array(record), candidate.array(record)
            )

    def test_budget_failure_is_not_a_recoverable_sampler_exception(self):
        self.assertFalse(
            issubclass(
                probe.ResidencyLimitExceeded,
                (ValueError, RuntimeError, TypeError, IndexError),
            )
        )


class TestResidencyComparison(unittest.TestCase):
    def reports(self):
        query = dict(
            seconds=0,
            longitude_shift=0,
            **probe.value_identity(
                np.array([[1.0, np.nan], [2.0, 3.0]]), [None, "source"]
            ),
        )
        reference = dict(
            mode="reference",
            snapshot_sha256="snapshot",
            forecast_time="time",
            view={},
            viewport=[10, 10],
            harness_sha256="harness",
            selected_sources=2,
            queries=[query],
            peak_retained_bytes=32,
            limit_bytes=64,
        )
        candidate = deepcopy(reference)
        candidate["mode"] = "bounded"
        return reference, candidate

    def test_identical_values_masks_provenance_and_queries_pass(self):
        self.assertEqual(
            probe.compare(*self.reports()), dict(status="passed", matched_queries=1)
        )

    def test_changes_and_incomplete_workloads_rejected(self):
        for key in (
            "values_sha256",
            "missing_sha256",
            "provenance_sha256",
            "seconds",
            "longitude_shift",
            "usable_points",
        ):
            reference, candidate = self.reports()
            candidate["queries"][0][key] = "changed"
            with self.subTest(key=key), self.assertRaises(ValueError):
                probe.compare(reference, candidate)
        reference, candidate = self.reports()
        candidate["queries"] = []
        with self.assertRaisesRegex(ValueError, "Mismatched queries"):
            probe.compare(reference, candidate)

    def test_clock_sources_harness_budget_and_blank_weather_rejected(self):
        for key in ("forecast_time", "selected_sources", "harness_sha256"):
            reference, candidate = self.reports()
            candidate[key] = "changed"
            with self.subTest(key=key), self.assertRaises(ValueError):
                probe.compare(reference, candidate)
        reference, candidate = self.reports()
        candidate["peak_retained_bytes"] = 65
        with self.assertRaisesRegex(ValueError, "retention budget"):
            probe.compare(reference, candidate)
        reference, candidate = self.reports()
        reference["queries"][0]["usable_points"] = 0
        with self.assertRaisesRegex(ValueError, "No usable"):
            probe.compare(reference, candidate)

    def test_nonfinite_classes_and_provenance_have_separate_hashes(self):
        a = probe.value_identity([[np.nan, 1]], [None])
        b = probe.value_identity([[np.inf, 1]], [None])
        c = probe.value_identity([[np.nan, 1]], ["other"])
        self.assertEqual(a["values_sha256"], b["values_sha256"])
        self.assertNotEqual(a["missing_sha256"], b["missing_sha256"])
        self.assertNotEqual(a["provenance_sha256"], c["provenance_sha256"])


if __name__ == "__main__":
    unittest.main()
