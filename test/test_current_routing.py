"""Analytical current advection, frame conventions, gap recovery and routes."""

import math
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import weatherrouting

from gweatherrouting.core.marine.chs import DownloadCancelledError
from gweatherrouting.core.marine.fields import CurrentField, FieldSelection
from gweatherrouting.core.marine.manager import MarineManager
from gweatherrouting.core.marine.models import canonical_json
from gweatherrouting.core.marine.routing import (
    ARRIVAL_METRES,
    KNOT,
    CurrentIsoRouter,
    CurrentRoutingEnvironment,
    DataGap,
    FrozenRoutingWind,
    FractionalRoutingGrib,
    InvalidMarineLegError,
    RoutingDataGapError,
    distance_bearing,
    download_gap_coverage,
    routing_utc,
    routing_completion,
    step_with_coverage,
)

START = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def current_field(east=0, north=0, end_east=None, masked=False, resolution=9000):
    metadata = dict(
        schema_version=1,
        provider="test",
        model="Analytical field",
        composition="total",
        vector_reference="true_east_north",
        sources=[],
        resolution_m=resolution,
        run=START.isoformat(),
    )
    x = np.full((2, 2, 2), float(east))
    if end_east is not None:
        x[1] = end_east
    if masked:
        x[0, 0, 0] = np.nan
    return CurrentField(
        canonical_json(metadata),
        np.array([START.timestamp(), (START + timedelta(hours=1)).timestamp()]),
        np.array([-1, 1]),
        np.array([-1, 1]),
        x,
        np.full((2, 2, 2), float(north)),
    )


def setup_router(field=None, wind=None, speed=6):
    field = field if field is not None else current_field()
    wind = wind if wind is not None else Mock(get_wind_at=Mock(return_value=(0, 10)))
    env = CurrentRoutingEnvironment(wind, FieldSelection([field]))
    polar = Mock(get_speed=Mock(return_value=speed))
    router = CurrentIsoRouter(polar, wind, environment=env)
    return router, env, polar


class TestCurrentRouting(unittest.TestCase):
    def test_repeated_samples_reuse_data_but_selection_and_cancel_take_effect(self):
        _, env, _ = setup_router(current_field(east=1))
        first = env.sample((0, 0), START)
        env.last_sample["latitude"] = 123
        self.assertEqual(env.sample((0, 0), START), first)
        self.assertEqual(env.last_sample["latitude"], 0)
        self.assertEqual(env.wind.get_wind_at.call_count, 1)
        env.selection = FieldSelection([current_field(east=2)])
        self.assertEqual(env.sample((0, 0), START)[2], 2)
        env.cancel.set()
        with self.assertRaises(DownloadCancelledError):
            env.sample((0, 0), START)

    def test_nearest_sampling_requires_shoreline_and_records_source_location(self):
        field = replace(
            current_field(east=1, masked=True, resolution=500),
            latitudes=np.array([0, 0.004]),
            longitudes=np.array([0, 0.004]),
        )
        wind = Mock(get_wind_at=Mock(return_value=(0, 10)))
        environment = CurrentRoutingEnvironment(wind, FieldSelection([field]))
        points = Mock(side_effect=lambda positions: [True] * len(positions))
        lines = Mock(side_effect=lambda segments: [True] * len(segments))
        CurrentIsoRouter(
            Mock(get_speed=Mock(return_value=6)),
            wind,
            environment=environment,
            points_validity=points,
            lines_validity=lines,
        )
        environment.sample((0.003, 0.003), START)
        self.assertEqual(
            environment.last_sample["current_sampling"], ("nearest_wet_cell",)
        )
        self.assertEqual(
            environment.last_sample["current_sample_location"]["latitude"], 0.004
        )
        self.assertTrue(points.called)
        self.assertTrue(lines.called)
        lines.side_effect = lambda segments: [False] * len(segments)
        environment._sample_cache.clear()
        with self.assertRaises(RoutingDataGapError):
            environment.sample((0.003, 0.003), START)
        _, strict_env, _ = setup_router(field)
        self.assertIsNone(strict_env.selection.neighbor_valid)
        with self.assertRaises(RoutingDataGapError):
            strict_env.sample((0.003, 0.003), START)

    def test_progress_estimates_remaining_distance_across_waypoints(self):
        routing = SimpleNamespace(
            position=(0, 0),
            track=[(0, 0), (0, 1), (0, 2)],
            wp=1,
            end=False,
            log=[],
            _startingNewPoint=True,
        )
        self.assertEqual(routing_completion(routing), 0)
        leg = distance_bearing((0, 0), (0, 1))[0]
        self.assertAlmostEqual(routing_completion(routing, leg / 2), 25)
        routing.wp, routing.position = 2, (0, 1)
        self.assertAlmostEqual(routing_completion(routing), 50)
        routing.wp = 3
        self.assertEqual(routing_completion(routing), 99)
        routing.end = True
        self.assertEqual(routing_completion(routing), 100)

    def test_streamed_frontier_matches_exhaustive_pruning_and_reports_progress(self):
        router, env, _ = setup_router()
        router.use_numpy = False
        end = (0, 0.5)
        first = self.point()
        previous = [
            weatherrouting.IsoPoint((0, x), time=START, next_wp_dist=30)
            for x in (0, 0.001, 0.002)
        ]
        expected = {}
        for index, point in enumerate(previous):
            for heading in router.headings(point, end):
                candidate = router.advance(point, 600, heading, end)
                candidate.prev_idx = index
                _, bearing = distance_bearing(first.pos, candidate.pos)
                key = round(math.degrees(bearing) / 2)
                if key not in expected:
                    expected[key] = [candidate]
                else:
                    expected[key].append(candidate)
                    expected[key].sort(key=lambda item: item.next_wp_dist)
                    if len(expected[key]) > 2:
                        expected[key] = expected[key][:2]
        env.progress = Mock()
        result = router.route(
            SimpleNamespace(isochrones=[[first], previous]), START, 1, first.pos, end
        )
        self.assertEqual(
            [(p.pos, p.prev_idx, p.time) for p in result.isochrones[-1]],
            [(p.pos, p.prev_idx, p.time) for group in expected.values() for p in group],
        )
        self.assertEqual(env.progress.call_args_list[0].args[1:], (0, 3))
        self.assertEqual(env.progress.call_args_list[-1].args[1:], (3, 3))

    def point(self):
        return weatherrouting.IsoPoint((0, 0), time=START)

    def test_helping_opposing_and_cross_current_vectors(self):
        for east, north, expected in (
            (KNOT, 0, 7),
            (-KNOT, 0, 5),
            (0, KNOT, math.sqrt(37)),
        ):
            router, _, _ = setup_router(current_field(east, north))
            result = router.advance(self.point(), 600, math.pi / 2, (0, 0.5))
            self.assertAlmostEqual(result.speed, expected, places=5)
            self.assertEqual(result.time, START + timedelta(minutes=10))
            if north:
                self.assertGreater(result.pos[0], 0)
                self.assertLess(result.brg, 90)

    def test_water_relative_wind_and_polar_units(self):
        router, env, polar = setup_router(current_field(east=2))
        wd, ws, *_ = env.sample((0, 0), START)
        self.assertAlmostEqual(ws, math.hypot(10, 2))
        self.assertAlmostEqual(wd, math.atan2(2, 10))
        router.velocity((0, 0), START, math.pi / 2)
        speed, angle = polar.get_speed.call_args.args
        self.assertAlmostEqual(speed, math.hypot(10, 2) / KNOT)
        self.assertAlmostEqual(angle, math.pi / 2 - wd)

    def test_midpoint_integrates_linearly_changing_current(self):
        router, _, _ = setup_router(current_field(east=0, end_east=6), speed=0)
        result = router.advance(self.point(), 600, math.pi / 2, (0, 0.5))
        distance, _ = distance_bearing((0, 0), result.pos)
        self.assertAlmostEqual(distance, 300, places=3)

    def test_masked_candidate_is_rejected_while_valid_candidate_survives(self):
        for numpy in (False, True):
            router, env, _ = setup_router()
            router.use_numpy = numpy
            gap = DataGap((0.001, 0.002), START, "current", "masked_or_missing")
            valid = weatherrouting.IsoPoint(
                (0, 0.01), time=START + timedelta(minutes=10), next_wp_dist=10
            )
            router.headings = Mock(return_value=[0])
            router.advance_candidates = Mock(
                return_value=[RoutingDataGapError([gap]), valid]
            )
            result = router.route(None, START, 1, (0, 0), (0, 0.5))
            self.assertEqual(result.isochrones[-1], [valid])
            self.assertEqual(result.excluded_current_samples, (gap.to_json(),))
            provenance = env.provenance([])
            self.assertEqual(provenance["excluded_current_samples"], [gap.to_json()])
            self.assertIn("usable environmental coverage", provenance["optimality"])

    def test_all_masked_candidates_still_stop_with_clear_gap(self):
        router, env, _ = setup_router(current_field(masked=True))
        with self.assertRaises(RoutingDataGapError):
            router.route(None, START, 1, (0, 0), (0, 0.5))
        self.assertTrue(env.excluded_samples)

    def test_absent_coverage_is_recovered_before_pruning_search(self):
        for kind, reason in (("current", "no_coverage"), ("wind", "no_coverage")):
            router, env, _ = setup_router()
            gap = DataGap((0.001, 0.002), START, kind, reason)
            valid = weatherrouting.IsoPoint(
                (0, 0.01), time=START + timedelta(minutes=10), next_wp_dist=10
            )
            router.headings = Mock(return_value=[0])
            router.advance_candidates = Mock(
                return_value=[RoutingDataGapError([gap]), valid]
            )
            with self.assertRaises(RoutingDataGapError):
                router.route(None, START, 1, (0, 0), (0, 0.5))
            self.assertFalse(env.excluded_samples)

    def test_missing_time_mask_and_wind_never_become_zero(self):
        for field, point in (
            (current_field(masked=True), self.point()),
            (
                current_field(),
                weatherrouting.IsoPoint((0, 0), time=START + timedelta(hours=2)),
            ),
        ):
            router, _, _ = setup_router(field)
            with self.assertRaises(RoutingDataGapError) as raised:
                router.advance(point, 600, math.pi / 2, (0, 0.5))
            self.assertEqual(raised.exception.gaps[0].kind, "current")
            self.assertIn("UTC", str(raised.exception))
        router, _, _ = setup_router(wind=Mock(get_wind_at=Mock(return_value=None)))
        with self.assertRaises(RoutingDataGapError) as raised:
            router.advance(self.point(), 600, math.pi / 2, (0, 0.5))
        self.assertEqual(raised.exception.gaps[0].kind, "wind")

    def test_endpoint_outside_time_is_detected(self):
        router, _, _ = setup_router()
        point = weatherrouting.IsoPoint((0, 0), time=START + timedelta(minutes=59))
        with self.assertRaises(RoutingDataGapError):
            router.advance(point, 120, math.pi / 2, (0, 0.5))

    def test_coastline_checked_before_sampling_land(self):
        router, env, _ = setup_router()
        router.lines_validity = lambda segments: [False] * len(segments)
        env.sample = Mock(wraps=env.sample)
        with self.assertRaises(InvalidMarineLegError):
            router.advance(self.point(), 600, math.pi / 2, (0, 0.5))
        self.assertEqual(env.sample.call_count, 1)

    def test_cancel_during_propagation(self):
        router, env, _ = setup_router()
        env.cancel.set()
        with self.assertRaises(DownloadCancelledError):
            router.advance(self.point(), 600, math.pi / 2, (0, 0.5))

    def run_route(self, east):
        router, env, polar = setup_router(current_field(east=east))
        routing = weatherrouting.Routing(
            lambda *args: router,
            polar,
            [(0, 0), (0, 0.02)],
            env.wind,
            START,
        )
        for _ in range(6):
            result = routing.step()
            if routing.end:
                break
        self.assertTrue(routing.end)
        self.assertLessEqual(
            distance_bearing(result.path[-1].pos, (0, 0.02))[0], ARRIVAL_METRES
        )
        self.assertEqual(result.path[0].time, START)
        self.assertTrue(
            all(a.time < b.time for a, b in zip(result.path, result.path[1:]))
        )
        provenance = env.provenance(result.path)
        self.assertEqual(
            provenance["currents"]["actual_sources"], [env.selection.fields[0].id]
        )
        self.assertTrue(provenance["samples"])
        return (result.path[-1].time - START).total_seconds()

    def test_real_engine_route_arrival_changes_with_currents(self):
        helping = self.run_route(KNOT)
        slack = self.run_route(0)
        opposing = self.run_route(-KNOT)
        self.assertLess(helping, slack)
        self.assertLess(slack, opposing)
        self.assertAlmostEqual(helping / opposing, 5 / 7, delta=0.06)

    def test_utc_offsets_refer_to_same_instant(self):
        self.assertEqual(
            routing_utc(START.astimezone(timezone(timedelta(hours=-7)))), START
        )

    def test_gap_download_requests_are_bounded_and_deduplicated(self):
        manager = SimpleNamespace(coverage_planner=Mock())
        manager.coverage_planner.download.return_value = ((), ())
        gaps = [DataGap((49.1, -123.1), START, "current", "no_coverage")] * 10
        download_gap_coverage(manager, gaps, threading.Event())
        self.assertEqual(manager.coverage_planner.download.call_count, 1)
        area, start, end, *_ = manager.coverage_planner.download.call_args.args
        self.assertEqual(end - start, timedelta(days=1))
        self.assertLessEqual(area.east - area.west, 0.25)
        self.assertLessEqual(area.north - area.south, 0.25)

    def test_recovery_retries_same_step_and_preserves_disabled_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory)
            field = current_field()
            env = CurrentRoutingEnvironment(Mock(), manager.fields.snapshot())
            routing = SimpleNamespace(
                current_environment=env, algorithm=SimpleNamespace()
            )
            gap = RoutingDataGapError(
                [DataGap((0, 0), START, "current", "no_coverage")]
            )
            routing.step = Mock(side_effect=[gap, "result"])
            manager.coverage_planner.download = Mock(return_value=([field], []))
            messages = []
            result = step_with_coverage(routing, manager, True, messages.append)
            self.assertEqual(result, "result")
            self.assertEqual(routing.step.call_count, 2)
            self.assertTrue(messages)
            self.assertEqual(env.downloads, [field.id])
            manager.fields.enable(field.id, False)
            env.selection = manager.fields.snapshot()
            routing.step = Mock(side_effect=gap)
            with self.assertRaises(ValueError):
                step_with_coverage(routing, manager, True, messages.append)
            self.assertEqual(manager.fields.snapshot().fields, ())

    def test_no_auto_download_for_wind_gaps_or_when_disabled(self):
        for auto, kind, reason in (
            (False, "current", "no_coverage"),
            (True, "wind", "no_coverage"),
            (True, "current", "masked_or_missing"),
        ):
            env = CurrentRoutingEnvironment(Mock(), FieldSelection([]))
            routing = SimpleNamespace(
                current_environment=env,
                step=Mock(
                    side_effect=RoutingDataGapError(
                        [DataGap((0, 0), START, kind, reason)]
                    )
                ),
            )
            with patch(
                "gweatherrouting.core.marine.routing.download_gap_coverage"
            ) as download:
                with self.assertRaises(RoutingDataGapError):
                    step_with_coverage(routing, Mock(), auto, Mock())
                download.assert_not_called()

    def test_recovery_stops_when_download_does_not_cover_failed_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory)
            env = CurrentRoutingEnvironment(Mock(), manager.fields.snapshot())
            gap = RoutingDataGapError(
                [DataGap((30, 30), START, "current", "no_coverage")]
            )
            routing = SimpleNamespace(
                current_environment=env,
                algorithm=SimpleNamespace(),
                step=Mock(side_effect=gap),
            )
            manager.coverage_planner.download = Mock(
                return_value=([current_field()], [])
            )
            with self.assertRaisesRegex(ValueError, "did not fill"):
                step_with_coverage(routing, manager, True, Mock())
            self.assertEqual(routing.step.call_count, 1)
            self.assertEqual(manager.coverage_planner.download.call_count, 1)

    def test_wind_selection_is_frozen_and_falls_back(self):
        first = Mock(name="first")
        first.name = "first"
        first.start_time = START
        first.get_wind_at.return_value = None
        second = Mock()
        second.name = "second"
        second.start_time = START
        second.get_wind_at.return_value = (90, 10)
        manager = SimpleNamespace(gribs=[first, second])
        wind = FrozenRoutingWind(manager)
        manager.gribs.clear()
        self.assertEqual(wind.get_wind_at(START, 0, 0), (90, 10))
        self.assertEqual(wind.used, {"second"})

    def test_core_routes_use_current_adapter_only_when_enabled(self):
        from gweatherrouting.core.core import Core
        from gweatherrouting.core.geo.track import Track
        from weatherrouting.routers.linearbestisorouter import LinearBestIsoRouter

        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory)
            manager.fields.add(current_field())
            wind = SimpleNamespace(gribs=[])
            core = SimpleNamespace(grib_manager=wind, marine_manager=manager)
            track = Track("test", [(0, 0), (0, 0.02)])
            with patch(
                "gweatherrouting.core.core.weatherrouting.Polar", return_value=Mock()
            ):
                route = Core.create_routing(
                    core,
                    CurrentIsoRouter,
                    "test",
                    track,
                    START,
                    None,
                    [],
                    True,
                    use_currents=True,
                )
                self.assertIsInstance(route.algorithm, CurrentIsoRouter)
                self.assertEqual(route.time.tzinfo, timezone.utc)
                self.assertEqual(
                    route.current_environment.selection.fields[0].id,
                    manager.fields.snapshot().fields[0].id,
                )
                manager.fields.enable(manager.fields.snapshot().fields[0].id, False)
                self.assertEqual(len(route.current_environment.selection.fields), 1)
                legacy = Core.create_routing(
                    core, LinearBestIsoRouter, "test", track, START, None, [], True
                )
                self.assertIsNone(legacy.current_environment)
                self.assertIs(legacy.grib, wind)

    def test_real_grib_fractional_time_and_forecast_bounds(self):
        from osgeo import gdal
        from gweatherrouting.core.grib import Grib

        data = gdal.GetDriverByName("MEM").Create("", 2, 2, 4, gdal.GDT_Float64)
        data.SetGeoTransform((-0.125, 0.25, 0, 0.375, 0, -0.25))
        for index, (element, hour, value) in enumerate(
            (("UGRD", 0, 0), ("VGRD", 0, -10), ("UGRD", 3, 6), ("VGRD", 3, -10)), 1
        ):
            band = data.GetRasterBand(index)
            band.SetMetadata(
                dict(GRIB_ELEMENT=element, GRIB_FORECAST_SECONDS=str(hour * 3600))
            )
            band.WriteArray(np.full((2, 2), float(value)))
        source = Grib.__new__(Grib)
        source.dataset, source.start_time, source.end_time = (
            data,
            START,
            START + timedelta(hours=3),
        )
        source.name = "test-grib"
        wind = FrozenRoutingWind(SimpleNamespace(gribs=[source]))
        direction, speed = wind.get_wind_at(START + timedelta(minutes=90), 0.1, 0.1)
        self.assertAlmostEqual(speed, math.hypot(3, 10))
        self.assertAlmostEqual(direction, math.degrees(math.atan2(-3, 10)) % 360)
        self.assertIsNone(wind.get_wind_at(START - timedelta(seconds=1), 0.1, 0.1))
        self.assertIsNone(
            wind.get_wind_at(START + timedelta(hours=3, seconds=1), 0.1, 0.1)
        )

    def test_local_grib_reads_match_legacy_sampling_and_reuse_windows(self):
        from osgeo import gdal
        from gweatherrouting.core.grib import Grib

        for transform in (
            (-0.625, 0.25, 0, 0.625, 0, -0.25),
            (0.625, -0.25, 0, -0.625, 0, 0.25),
        ):
            with self.subTest(transform=transform):
                data = gdal.GetDriverByName("MEM").Create("", 5, 5, 4, gdal.GDT_Float64)
                data.SetGeoTransform(transform)
                for index, (element, hour) in enumerate(
                    (("UGRD", 0), ("VGRD", 0), ("UGRD", 3), ("VGRD", 3)), 1
                ):
                    band = data.GetRasterBand(index)
                    band.SetMetadata(
                        dict(
                            GRIB_ELEMENT=element, GRIB_FORECAST_SECONDS=str(hour * 3600)
                        )
                    )
                    band.WriteArray(np.arange(25).reshape(5, 5) + index)
                source = Grib.__new__(Grib)
                source.dataset = data
                source.start_time = START
                source.end_time = START + timedelta(hours=3)
                optimized = FractionalRoutingGrib(source)
                # Routing wrapper uses the same shared interpolation as the display.
                for latitude, longitude in (
                    (0.1, 0.1),
                    (-0.1, -0.1),
                    (0, 0),
                    (0.5, -0.5),
                ):
                    for minutes in (0, 40, 90, 180):
                        instant = START + timedelta(minutes=minutes)
                        expected = source.get_wind_at(instant, latitude, longitude)
                        with patch.object(
                            Grib,
                            "get_rindex_data",
                            side_effect=AssertionError("Full grid scan"),
                        ):
                            actual = optimized.get_wind_at(instant, latitude, longitude)
                        if expected is None:
                            self.assertIsNone(actual)
                        else:
                            np.testing.assert_allclose(actual, expected, rtol=1e-14)
                cached = dict(source.sampling.arrays)
                optimized.get_wind_at(START + timedelta(minutes=15), 0.1, 0.1)
                self.assertEqual(set(source.sampling.arrays), set(cached))
                self.assertTrue(
                    all(source.sampling.arrays[k] is v for k, v in cached.items())
                )
                self.assertIsNone(optimized.get_wind_at(START, 10, 10))
