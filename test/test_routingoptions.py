"""Routing options preserve engine calculations while owning each run's inputs."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import patch
import time
import unittest

import weatherrouting

from gweatherrouting.core import Core
from gweatherrouting.core.geo.poi import POI
from gweatherrouting.core.geo.track import Track
from gweatherrouting.core.marine.chs import DownloadCancelledError
from gweatherrouting.core.marine.manager import MarineManager
from gweatherrouting.core.marine.routing import CurrentIsoRouter, step_with_coverage
from gweatherrouting.core.modifiedgrib import ModifiedGribManager
from gweatherrouting.core.routingjob import (
    RouteRequest,
    RoutingWind,
    calculate_route,
    serialize_routing_points,
)
from gweatherrouting.core.routingoptions import (
    AXES,
    comparison_scenarios,
    configured_algorithm,
    scenario_request,
)
from test.test_current_routing import START, current_field


class CurrentWind:
    name = "constant"
    path = "constant"
    centre = "fixture"
    start_time = START
    end_time = START + timedelta(days=2)

    def get_wind_at(self, instant, lat, lon):
        return 270, 7.2


class TestRoutingOptions(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.core = Core.__new__(Core)
        self.core.grib_manager = SimpleNamespace(gribs=[CurrentWind()])
        self.core.marine_manager = MarineManager(self.directory.name)
        self.algorithm = weatherrouting.list_routing_algorithms()[0]["class"]
        self.request = RouteRequest(
            "routing-test",
            (0, 0),
            (0, 0.05),
            START.replace(tzinfo=None),
            str(Path(__file__).parents[1] / "gweatherrouting/data/polars/first317.pol"),
            self.algorithm,
            False,
        )

    def calculate(self, request=None, **kwargs):
        return calculate_route(
            self.core,
            request or self.request,
            self.core.grib_manager.gribs,
            (),
            Event(),
            lambda *args: None,
            **kwargs,
        )

    def test_parameterized_track_and_scenario_match_existing_engine(self):
        params = {"subdiv": 2, "concurrent": False, "min_increase": 3.5}
        request = replace(
            self.request,
            parameters=tuple(params.items()),
            start=None,
            waypoints=((0, 0), (0, 0.15), (0, 0.30)),
            polar_efficiency=0.85,
            wind_speed_pct=20,
            wind_dir_offset=15,
        )
        original = {key: p.value for key, p in self.algorithm.PARAMS.items()}
        result = self.calculate(request)
        reference = deepcopy(self.algorithm.PARAMS)
        for key, value in params.items():
            reference[key].value = value
        with patch.object(self.algorithm, "PARAMS", reference):
            old = self.core.create_routing(
                self.algorithm,
                request.polar_file,
                Track("route", list(request.waypoints)),
                request.departure,
                None,
                (),
                True,
                grib_override=ModifiedGribManager(CurrentWind(), 1.2, 15),
                polar_efficiency=0.85,
            )
            while not old.end:
                expected = old.step()
        self.assertEqual(result.points, serialize_routing_points(old, expected.path))
        self.assertEqual(
            original, {key: p.value for key, p in self.algorithm.PARAMS.items()}
        )
        self.assertEqual(result.provenance["options"]["parameters"], params)

    def test_comparisons_use_gtk_defaults_order_names_and_absolute_efficiency(self):
        self.assertEqual(AXES[-1][2], "85, 100")
        scenarios = comparison_scenarios(
            {"time_offset_hours": "-6, 0, 6", "polar_efficiency_pct": "85, 100"}
        )
        self.assertEqual(len(scenarios), 6)
        self.assertEqual([s.time_offset_hours for s in scenarios], [-6, -6, 0, 0, 6, 6])
        request = scenario_request(self.request, scenarios[0])
        self.assertEqual(request.name, "routing-test_t-6h_eff85%")
        self.assertEqual(request.departure, self.request.departure - timedelta(hours=6))
        self.assertEqual(request.polar_efficiency, 0.85)
        for axes in (
            {"time_offset_hours": ""},
            {"wind_speed_pct": "-100"},
            {"wind_dir_offset": "nan"},
            {"polar_efficiency_pct": "0"},
            {"time_offset_hours": ",".join(str(i) for i in range(257))},
        ):
            with self.assertRaises(ValueError):
                comparison_scenarios(axes)

    def test_algorithm_params_are_isolated_and_invalid_values_rejected(self):
        first = configured_algorithm(self.algorithm, (("subdiv", 3),))
        second = configured_algorithm(self.algorithm, (("subdiv", 4),))
        self.assertEqual(first.PARAMS["subdiv"].value, 3)
        self.assertEqual(second.PARAMS["subdiv"].value, 4)
        self.assertIsNot(first.PARAMS["subdiv"], self.algorithm.PARAMS["subdiv"])
        for values in (
            {"subdiv": 0},
            {"subdiv": 1.5},
            {"concurrent": 1},
            {"unknown": 1},
            {"min_increase": float("nan")},
        ):
            with self.assertRaises(ValueError):
                configured_algorithm(self.algorithm, values.items())

    def test_concurrent_wind_queries_do_not_share_simultaneous_gdal_access(self):
        lock = Lock()
        active, peak = [0], [0]

        class Wind(CurrentWind):
            def get_wind_at(self, *args):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.005)
                with lock:
                    active[0] -= 1
                return 270, 7.2

        wind = RoutingWind([Wind()], Event())
        self.addCleanup(wind.close)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: wind.get_wind_at(START, 0, 0), range(12)))
        self.assertEqual(peak[0], 1)
        self.assertEqual(results, [(270, 7.2)] * 12)

    def test_current_route_matches_original_engine_and_keeps_utc_provenance(self):
        current = current_field(east=0.4)
        self.core.marine_manager.fields.add(current)
        request = replace(
            self.request,
            destination=(0, 0.005),
            departure=START,
            algorithm=CurrentIsoRouter,
            use_currents=True,
        )
        result = self.calculate(request, confirm=lambda report: True)
        old = self.core.create_routing(
            CurrentIsoRouter,
            request.polar_file,
            POI("end", request.destination),
            request.departure,
            request.start,
            (),
            True,
            use_currents=True,
        )
        while not old.end:
            expected = step_with_coverage(
                old, self.core.marine_manager, False, lambda message: None
            )
        self.assertEqual(result.points, serialize_routing_points(old, expected.path))
        self.assertEqual(result.provenance["routing"], "isochrone-with-currents")
        self.assertEqual(result.provenance["currents"]["actual_sources"], [current.id])
        self.assertTrue(result.provenance["samples"])
        self.assertTrue(result.points[-1][2].endswith("+00:00"))
        self.assertEqual(result.points[-1][3], 270)

    def test_preflight_refusal_or_blocked_departure_cannot_save_a_route(self):
        request = replace(self.request, algorithm=CurrentIsoRouter, use_currents=True)
        reports = []
        with self.assertRaises(DownloadCancelledError):
            self.calculate(
                request, confirm=lambda report: reports.append(report) or True
            )
        self.assertTrue(reports[0].blocked)
        self.assertEqual(self.core.marine_manager.fields.fields, {})

    def test_automatic_coverage_uses_existing_recovery_and_ui_commit_hooks(self):
        from gweatherrouting.core.routingcoverage import prepare_coverage

        cancel = Event()
        environment = SimpleNamespace(cancel=None, wind_recovery=None, prefetch=None)
        route = SimpleNamespace(
            current_environment=environment,
            position=(0, 0),
            time=START,
            grib=CurrentWind(),
        )
        coverage = SimpleNamespace(needs_warning=False, download_gaps=("gap",))

        def save(gribs):
            return None

        with (
            patch(
                "gweatherrouting.core.routingcoverage.WindCoverageRecovery"
            ) as recovery,
            patch("gweatherrouting.core.routingcoverage.CoveragePrefetch") as prefetch,
            patch(
                "gweatherrouting.core.routingcoverage.check_current_coverage",
                return_value=coverage,
            ),
        ):
            recovery.return_value.missing.return_value = ["wind gap"]
            prepare_coverage(
                route,
                self.core.marine_manager,
                True,
                cancel,
                lambda message: None,
                lambda report: False,
                save,
            )
            self.assertIs(environment.cancel, cancel)
            recovery.assert_called_once_with(route, save=save)
            recovery.return_value.refresh.assert_called_once()
            recovery.return_value.recover.assert_called_once_with(["wind gap"])
            prefetch.assert_called_once_with(
                self.core.marine_manager, environment, coverage.download_gaps
            )
            recovery.return_value.prefetch.assert_called_once()
