"""Saved tide reuse and current prefetch without network or native data files."""

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from gweatherrouting.core.marine.chs import DownloadCancelledError
from gweatherrouting.core.marine.fields import FieldSelection
from gweatherrouting.core.marine.manager import MarineManager
from gweatherrouting.core.marine.models import Sample, Series
from gweatherrouting.core.marine.prefetch import CoveragePrefetch
from gweatherrouting.core.marine.routing import (
    CurrentRoutingEnvironment,
    DataGap,
    RoutingDataGapError,
    step_with_coverage,
)
from test.test_current_routing import START, current_field
from test.test_marine import dataset
from test.test_current_preflight import routing
from gweatherrouting.core.marine.preflight import check_current_coverage


def tide_data(hours=2):
    value = dataset()
    return replace(
        value,
        requested_end=START + timedelta(hours=hours),
        series=(
            Series(
                "water_level",
                "m",
                tuple(
                    Sample(START + timedelta(minutes=15 * i), 2.5, "2")
                    for i in range(hours * 4 + 1)
                ),
            ),
        ),
    )


class TestSavedTides(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = MarineManager(directory.name)
        self.value = tide_data()
        self.cancel = threading.Event()

    def test_repeat_download_reuses_saved_samples_even_when_not_enabled(self):
        self.manager.add(self.value)
        provider = Mock()
        result = self.manager.download_station(
            provider, self.value.station, START, START + timedelta(hours=1), self.cancel
        )
        self.assertEqual(result, self.value)
        provider.download.assert_not_called()
        self.assertFalse(self.manager.is_enabled(self.value.id))

    def test_requested_bounds_do_not_hide_nulls_or_time_gaps(self):
        series = self.value.series[0]
        for samples in (
            series.samples[::3],
            tuple(
                replace(s, value=None) if i == 3 else s
                for i, s in enumerate(series.samples)
            ),
        ):
            value = replace(self.value, series=(replace(series, samples=samples),))
            self.manager.add(value)
            self.assertIsNone(
                self.manager.cached_station(
                    value.station, START, START + timedelta(hours=2)
                )
            )

    def test_partly_covered_request_is_not_incorrectly_skipped(self):
        self.manager.add(self.value)
        provider = Mock(download=Mock(return_value=tide_data(3)))
        self.manager.download_station(
            provider, self.value.station, START, START + timedelta(hours=3), self.cancel
        )
        provider.download.assert_called_once()
        self.assertEqual(
            provider.download.call_args.args[1:3],
            (START + timedelta(hours=2), START + timedelta(hours=3)),
        )

    def test_concurrent_panels_join_a_download_before_the_ui_saves_it(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def download(*_):
            started.set()
            release.wait(2)
            return self.value

        provider = Mock(download=Mock(side_effect=download))
        results, errors = [], []

        def run():
            try:
                results.append(
                    self.manager.download_station(
                        provider,
                        self.value.station,
                        START,
                        self.value.requested_end,
                        self.cancel,
                    )
                )
            except Exception as error:
                errors.append(error)

        first, second = threading.Thread(target=run), threading.Thread(target=run)
        first.start()
        self.assertTrue(started.wait(1))
        second.start()
        release.set()
        first.join(2)
        second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [self.value, self.value])
        provider.download.assert_called_once()

    def test_shifted_window_fetches_only_missing_tail_with_sample_brackets(self):
        self.manager.add(self.value)
        provider = Mock(download=Mock(return_value=tide_data(3)))
        result = self.manager.download_station(
            provider,
            self.value.station,
            START + timedelta(minutes=7),
            START + timedelta(hours=2, minutes=7),
            self.cancel,
        )
        self.assertEqual(
            provider.download.call_args.args[1:3],
            (
                START + timedelta(hours=2),
                START + timedelta(hours=2, minutes=15),
            ),
        )
        self.assertEqual(result.series[0].samples, tide_data(3).series[0].samples)

    def test_compatible_saved_windows_are_joined_without_network(self):
        self.manager.add(self.value)
        later = tide_data(3)
        later = replace(
            later,
            requested_start=START + timedelta(hours=2),
            series=(replace(later.series[0], samples=later.series[0].samples[8:]),),
        )
        self.manager.add(later)
        provider = Mock()
        result = self.manager.download_station(
            provider,
            self.value.station,
            START,
            START + timedelta(hours=3),
            self.cancel,
        )
        provider.download.assert_not_called()
        self.assertEqual(result.series[0].samples, tide_data(3).series[0].samples)

    def test_metadata_changes_require_a_coherent_fresh_window(self):
        self.manager.add(self.value)
        fresh = replace(tide_data(3), metadata_json='{"datum": "changed"}')
        provider = Mock(download=Mock(return_value=fresh))
        result = self.manager.download_station(
            provider,
            self.value.station,
            START,
            START + timedelta(hours=3),
            self.cancel,
        )
        self.assertEqual(provider.download.call_count, 2)
        self.assertEqual(
            provider.download.call_args.args[1:3], (START, START + timedelta(hours=3))
        )
        self.assertEqual(result, fresh)


class TestCurrentPrefetch(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = MarineManager(directory.name)
        self.manager.coverage_planner.download = Mock(
            return_value=([current_field()], [])
        )
        self.env = CurrentRoutingEnvironment(Mock(), FieldSelection([]))
        self.gap = DataGap((0, 0), START, "current", "no_coverage")

    def prefetch(self, gaps):
        result = CoveragePrefetch(self.manager, self.env, gaps)
        self.addCleanup(result.close)
        self.env.prefetch = result
        return result

    def settled(self, worker):
        deadline = time.monotonic() + 2
        while worker.pending and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(worker.pending, 0)

    def test_download_starts_ahead_and_routing_does_not_wait_unnecessarily(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def download(*_):
            started.set()
            release.wait(2)
            return [current_field()], []

        self.manager.coverage_planner.download.side_effect = download
        worker = self.prefetch([self.gap])
        self.assertTrue(started.wait(1))
        route = SimpleNamespace(
            current_environment=self.env, step=Mock(return_value="step")
        )
        self.assertEqual(step_with_coverage(route, self.manager, True, Mock()), "step")
        self.assertEqual(self.env.selection.fields, ())
        release.set()
        self.settled(worker)

    def test_actual_gap_joins_prefetch_and_is_installed_only_between_steps(self):
        worker = self.prefetch([self.gap] * 10)
        self.settled(worker)
        self.assertEqual(self.env.selection.fields, ())
        route = SimpleNamespace(
            current_environment=self.env,
            algorithm=SimpleNamespace(),
            step=Mock(return_value="step"),
        )
        self.assertEqual(step_with_coverage(route, self.manager, True, Mock()), "step")
        self.assertEqual(len(self.env.selection.fields), 1)
        worker.wait_for([self.gap])
        worker.wait_for([self.gap])
        self.manager.coverage_planner.download.assert_called_once()
        self.assertEqual(worker.ready(), ([], []))

    def test_overlapping_download_satisfies_other_tiles_without_redownloading(self):
        other = replace(self.gap, position=(0.3, 0.3))
        worker = self.prefetch([self.gap, other])
        self.settled(worker)
        self.manager.coverage_planner.download.assert_called_once()
        self.assertEqual(len(worker.ready()[0]), 1)

    def test_waiting_for_overlap_delivers_the_field_that_filled_it(self):
        other = replace(self.gap, position=(0.3, 0.3))
        worker = self.prefetch([self.gap, other])
        self.settled(worker)
        fields, issues = worker.wait_for([other])
        self.assertEqual(len(fields), 1)
        self.assertEqual(issues, [])
        self.manager.coverage_planner.download.assert_called_once()

    def test_prefetch_failure_waits_until_that_coverage_is_required(self):
        self.manager.coverage_planner.download.side_effect = ValueError("offline")
        worker = self.prefetch([self.gap])
        self.settled(worker)
        route = SimpleNamespace(
            current_environment=self.env,
            algorithm=SimpleNamespace(),
            step=Mock(return_value="step"),
        )
        self.assertEqual(step_with_coverage(route, self.manager, True, Mock()), "step")
        route.step.side_effect = RoutingDataGapError([self.gap])
        with self.assertRaisesRegex(ValueError, "offline"):
            step_with_coverage(route, self.manager, True, Mock())
        self.manager.coverage_planner.download.assert_called_once()

    def test_cancellation_releases_waiters_and_stops_background_work(self):
        started = threading.Event()

        def download(area, start, end, cancel, progress):
            started.set()
            cancel.wait(2)
            raise DownloadCancelledError()

        self.manager.coverage_planner.download.side_effect = download
        worker = self.prefetch([self.gap])
        self.assertTrue(started.wait(1))
        self.env.cancel.set()
        with self.assertRaises(DownloadCancelledError):
            worker.wait_for([self.gap])
        worker._thread.join(1)
        self.assertFalse(worker._thread.is_alive())

    def test_prefetch_is_bounded_and_does_not_download_model_masks(self):
        gaps = [replace(self.gap, position=(0, i / 4)) for i in range(40)]
        # Start blocked, so assertions do not race through the whole queue.
        self.env.cancel.set()
        worker = self.prefetch([replace(self.gap, reason="masked_or_missing"), *gaps])
        self.assertEqual(len(worker._jobs), 32)
        self.manager.coverage_planner.download.assert_not_called()

    def test_preflight_provides_missing_requests_without_downloading(self):
        route = routing(current_field())
        report = check_current_coverage(route, True)
        self.assertTrue(report.download_gaps)
        self.assertTrue(
            all(g.reason in ("no_coverage", "time_gap") for g in report.download_gaps)
        )
        self.assertFalse(route.current_environment.selection.used)
        self.assertFalse(check_current_coverage(routing(), True).download_gaps)
