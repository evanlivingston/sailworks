"""Offline station providers, source policies and saved provenance."""

import json
import hashlib
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from gweatherrouting.core.geo.routing import Routing
from gweatherrouting.core.marine.chs import CHSProvider, DownloadCancelledError
from gweatherrouting.core.marine.manager import MarineManager
from gweatherrouting.core.marine.models import (
    Capability,
    Dataset,
    Sample,
    Series,
    Station,
    canonical_json,
    sample_station,
)

START = datetime(2026, 10, 11, tzinfo=timezone.utc)
STATION = Station("example:one", "one", "Example channel", 50, -125, ())


def dataset(speed=2, direction=90, start=START, qc="2", station=STATION):
    times = (start, start + timedelta(minutes=15))
    return Dataset(
        "example",
        station,
        START,
        start,
        times[-1],
        (
            Series("current_speed", "kn", tuple(Sample(t, speed, qc) for t in times)),
            Series(
                "current_direction",
                "deg",
                tuple(Sample(t, direction, qc) for t in times),
            ),
        ),
        "{}",
        "[]",
        "Example attribution",
        "https://example.invalid/licence",
    )


class TestMarineSampling(unittest.TestCase):
    def test_current_flows_toward_true_bearing_in_metres_per_second(self):
        for degrees, east, north in ((0, 0, 1), (90, 1, 0), (180, 0, -1), (270, -1, 0)):
            result = sample_station(
                dataset(1, degrees), Capability.STATION_CURRENT, START
            )
            self.assertEqual(result.status, "available")
            self.assertAlmostEqual(result.values[0], east * 1852 / 3600)
            self.assertAlmostEqual(result.values[1], north * 1852 / 3600)
            self.assertEqual(result.qc_flags, ("2",))

    def test_reversing_current_interpolates_vectors_not_compass_degrees(self):
        value = dataset(direction=0)
        direction = replace(
            value.series[1],
            samples=(
                Sample(START, 0, "2"),
                Sample(START + timedelta(minutes=15), 180, "2"),
            ),
        )
        value = replace(value, series=(value.series[0], direction))
        result = sample_station(
            value, Capability.STATION_CURRENT, START + timedelta(minutes=7.5)
        )
        self.assertAlmostEqual(result.values[0], 0)
        self.assertAlmostEqual(result.values[1], 0)

    def test_time_gaps_nulls_and_quality_fail_explicitly(self):
        for value in (dataset(qc="3"), dataset(speed=None)):
            self.assertEqual(
                sample_station(value, Capability.STATION_CURRENT, START).status,
                "missing_samples",
            )
        self.assertEqual(
            sample_station(
                dataset(), Capability.STATION_CURRENT, START - timedelta(seconds=1)
            ).status,
            "outside_time",
        )
        value = dataset()
        series = tuple(
            replace(
                s,
                samples=(
                    s.samples[0],
                    replace(s.samples[-1], time=START + timedelta(hours=2)),
                ),
            )
            for s in value.series
        )
        value = replace(value, requested_end=START + timedelta(hours=2), series=series)
        self.assertEqual(
            sample_station(
                value, Capability.STATION_CURRENT, START + timedelta(minutes=30)
            ).status,
            "missing_samples",
        )

    def test_units_and_paired_current_times_are_checked(self):
        value = dataset()
        invalid = replace(
            value, series=(replace(value.series[0], unit="m/s"), value.series[1])
        )
        self.assertEqual(
            sample_station(invalid, Capability.STATION_CURRENT, START).status,
            "unsupported_unit",
        )
        invalid = replace(
            value, series=(value.series[0], replace(value.series[1], samples=()))
        )
        self.assertEqual(
            sample_station(invalid, Capability.STATION_CURRENT, START).status,
            "unpaired_components",
        )

    def test_water_level_keeps_unknown_datum_and_missing_height(self):
        value = replace(
            dataset(),
            series=(
                Series(
                    "water_level",
                    "m",
                    (
                        Sample(START, 1, "1"),
                        Sample(START + timedelta(minutes=15), 3, "2"),
                    ),
                ),
            ),
        )
        result = sample_station(
            value, Capability.WATER_LEVEL, START + timedelta(minutes=7.5)
        )
        self.assertEqual(result.values, (2,))
        self.assertNotIn(Capability.CURRENT_FIELD, value.capabilities)
        self.assertEqual(value.manifest()["metadata"], {})


class TestMarineManager(unittest.TestCase):
    def test_restart_is_offline_and_new_downloads_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory)
            identifier = manager.add(dataset())
            self.assertFalse(manager.is_enabled(identifier))
            manager.enable(identifier)
            manager.store_stations((STATION,))
            restarted = MarineManager(directory)
            self.assertEqual(restarted.stations, (STATION,))
            selection = restarted.snapshot(Capability.STATION_CURRENT)
            self.assertEqual(selection.sample(STATION.id, START).dataset_id, identifier)
            self.assertEqual(
                selection.sample("other:station", START).status, "no_selected_source"
            )

    def test_fresh_installs_fall_back_but_stored_opt_out_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory)
            self.assertTrue(manager.allow_fallback)
            manager.set_fallback(False)
            restarted = MarineManager(directory)
            self.assertFalse(restarted.allow_fallback)
            self.assertFalse(
                restarted.snapshot(Capability.STATION_CURRENT).allow_fallback
            )

    def test_snapshot_survives_changes_and_cache_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory)
            identifier = manager.add(dataset())
            manager.enable(identifier)
            snapshot = manager.snapshot(Capability.STATION_CURRENT)
            manifest = snapshot.to_json()
            manager.remove(identifier)
            self.assertEqual(snapshot.sample(STATION.id, START).status, "available")
            self.assertEqual(snapshot.to_json(), manifest)
            self.assertEqual(manager.snapshot(Capability.STATION_CURRENT).datasets, ())
            with self.assertRaises(FrozenInstanceError):
                snapshot.datasets[0].provider = "changed"

    def test_priority_fallback_and_per_calculation_override_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory)
            first = manager.add(dataset(qc="3"))
            second = manager.add(dataset(speed=4))
            manager.enable(first)
            manager.enable(second)
            self.assertTrue(manager.allow_fallback)
            selection = manager.snapshot(Capability.STATION_CURRENT)
            result = selection.sample(STATION.id, START)
            self.assertEqual(result.dataset_id, second)
            self.assertTrue(result.fell_back)
            self.assertIn(first, result.detail)
            manager.set_fallback(False)
            selection = manager.snapshot(Capability.STATION_CURRENT)
            self.assertEqual(
                selection.sample(STATION.id, START).status, "missing_samples"
            )
            self.assertFalse(selection.sample(STATION.id, START).fell_back)
            manager.set_fallback(True)
            selection = manager.snapshot(Capability.STATION_CURRENT)
            result = selection.sample(STATION.id, START)
            self.assertEqual(result.dataset_id, second)
            self.assertIn(first, result.detail)
            override = manager.snapshot(Capability.STATION_CURRENT, [second], False)
            self.assertEqual(len(override.datasets), 1)
            self.assertEqual(manager.ordered_datasets()[0].id, first)
            manager.move(second, -1)
            self.assertEqual(manager.ordered_datasets()[0].id, second)

    def test_corrupt_cache_and_selection_remain_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory)
            identifier = manager.add(dataset())
            path = Path(directory) / "datasets" / (identifier + ".json")
            value = json.loads(path.read_text())
            value["attribution"] = "tampered"
            path.write_text(canonical_json(value))
            restarted = MarineManager(directory)
            self.assertTrue(restarted.errors)
            self.assertFalse(restarted.datasets)

    def test_duplicate_or_unknown_source_overrides_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = MarineManager(directory)
            identifier = manager.add(dataset())
            for identifiers in ([identifier, identifier], ["missing"]):
                with self.assertRaises(ValueError):
                    manager.snapshot(Capability.STATION_CURRENT, identifiers)


class TestCHSProvider(unittest.TestCase):
    def test_streamed_response_records_actual_url_and_raw_checksum(self):
        payload = b'[{"code":"08108"}]'
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.url = "https://example.invalid/stations?code=08108"
        response.iter_content.return_value = (payload[:5], payload[5:])
        session = Mock()
        session.get.return_value = response
        rows, source = CHSProvider(session)._get(
            "stations", {"code": "08108"}, threading.Event()
        )
        self.assertEqual(rows, [{"code": "08108"}])
        self.assertEqual(source["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(source["bytes"], len(payload))
        self.assertEqual(source["url"], response.url)
        response.raise_for_status.assert_called_once()

    def test_download_normalizes_series_and_preserves_sources(self):
        provider = CHSProvider()
        station = Station("chs-iwls:08108", "08108", "Seymour", 50, -125, ())
        remote = dict(
            code="08108",
            id="opaque-id",
            officialName="Seymour Narrows",
            latitude=50,
            longitude=-125,
            timeSeries=[
                dict(code="wcsp1", phenomenonId="speed"),
                dict(code="wcdp1", phenomenonId="bearing"),
                dict(code="wcp1-events", phenomenonId="speed"),
            ],
        )
        source = {"url": "https://example.invalid/source", "sha256": "example"}

        def get(endpoint, params, cancel):
            if endpoint == "stations":
                return [remote], source
            if endpoint.endswith("metadata"):
                return dict(floodDirection=180, ebbDirection=0), source
            if endpoint == "phenomena":
                return [
                    dict(id="speed", unit="kn"),
                    dict(id="bearing", unit="deg"),
                ], source
            row = dict(
                eventDate=START.isoformat(),
                value=180 if params["time-series-code"] == "wcdp1" else 2,
                qcFlagCode="2",
            )
            if params["time-series-code"] == "wcp1-events":
                row["qualifier"] = "EXTREMA_FLOOD"
            return [row], source

        with patch.object(provider, "_get", side_effect=get):
            result = provider.download(station, START, START + timedelta(days=1))
        self.assertIn(Capability.STATION_CURRENT, result.capabilities)
        self.assertIn(Capability.PASSAGE_EVENTS, result.capabilities)
        self.assertEqual(result.get_series("current_direction").source_code, "wcdp1")
        self.assertEqual(len(result.manifest()["sources"]), 6)
        self.assertFalse(result.manifest()["metadata"]["routing_qualified"])

    def test_cancelled_download_makes_no_request(self):
        session = Mock()
        provider = CHSProvider(session)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(DownloadCancelledError):
            provider.discover(cancel)
        session.get.assert_not_called()

    def test_station_discovery_keeps_prediction_only_stations(self):
        provider = CHSProvider()
        row = dict(
            code="08108",
            officialName="Seymour",
            latitude=50,
            longitude=-125,
            operating=False,
            timeSeries=[dict(code="wcdp1"), dict(code="wcsp1")],
        )
        with patch.object(provider, "_get", return_value=([row], {})):
            self.assertEqual(len(provider.discover()), 1)

    def test_invalid_intervals_do_not_reach_network(self):
        provider = CHSProvider()
        with patch.object(provider, "_get") as get:
            with self.assertRaises(ValueError):
                provider.download(STATION, START, START + timedelta(days=8))
        get.assert_not_called()


class TestRoutingProvenance(unittest.TestCase):
    def test_saved_routes_preserve_provenance_without_aliasing(self):
        source = dict(
            schema_version=1, selected_sources=[dataset().manifest()], actual_sources=[]
        )
        route = Routing("Example", [], [], provenance=source)
        source["actual_sources"].append("changed after construction")
        saved = route.to_json()
        restored = Routing.from_json(saved)
        saved["provenance"]["actual_sources"].append("changed after restoration")
        self.assertEqual(restored.provenance["actual_sources"], [])

    def test_legacy_route_has_unknown_provenance(self):
        value = Routing("Legacy", [], []).to_json()
        del value["provenance"]
        self.assertIsNone(Routing.from_json(value).provenance)


if __name__ == "__main__":
    unittest.main()
