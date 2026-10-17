"""Geographic coverage, masked current interpolation and provider selection."""

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
import requests
from osgeo import gdal

from gweatherrouting.core.marine.coverage import CoveragePlanner
from gweatherrouting.core.marine.fields import (
    Area,
    CurrentField,
    FieldStore,
    FieldSelection,
)
from gweatherrouting.core.marine.models import canonical_json
from gweatherrouting.core.marine.spatialproviders import (
    CopernicusProvider,
    CIOPSProvider,
    DATAMART_URL,
    DownloadClient,
    read_component,
)
from test.test_marine import START


def field(resolution=500, masked=False, start=START):
    east = np.ones((2, 2, 2))
    if masked:
        east[:, 0, 0] = np.nan
    metadata = dict(
        schema_version=1,
        provider="example",
        model="Test currents",
        composition="total",
        vector_reference="true_east_north",
        sources=[],
        resolution_m=resolution,
        run=start.isoformat(),
        routing_qualified=False,
    )
    return CurrentField(
        canonical_json(metadata),
        np.array([start.timestamp(), (start + timedelta(hours=1)).timestamp()]),
        np.array([49, 50]),
        np.array([-126, -125]),
        east,
        east * 2,
    )


def netcdf(path):
    dataset = gdal.GetDriverByName("netCDF").CreateMultiDimensional(str(path))
    root = dataset.GetRootGroup()
    t = root.CreateDimension("time", "TEMPORAL", "", 2)
    y = root.CreateDimension("latitude", "HORIZONTAL_Y", "NORTH", 2)
    x = root.CreateDimension("longitude", "HORIZONTAL_X", "EAST", 2)
    entries = [
        ("time", [t], [0, 1], "hours since 2026-10-11 00:00:00"),
        ("latitude", [y], [50, 49], "degrees_north"),
        ("longitude", [x], [234, 235], "degrees_east"),
    ]
    for name, value in (("uo", 1), ("vo", 2), ("utide", 3), ("vtide", 4)):
        entries.append((name, [t, y, x], np.full((2, 2, 2), value), "m s-1"))
    for name, dimensions, values, unit in entries:
        array = root.CreateMDArray(
            name, dimensions, gdal.ExtendedDataType.Create(gdal.GDT_Float64)
        )
        array.SetUnit(unit)
        array.Write(np.asarray(values, dtype=float))
    array = root = dataset = None


class TestSpatialCurrents(unittest.TestCase):
    def test_display_coverage_with_retained_order_matches_fresh_selection(self):
        from gweatherrouting.core.marine.presentation import current_coverage

        with tempfile.TemporaryDirectory() as directory:
            store = FieldStore(directory)
            store.add(field(masked=True))
            store.add(field(resolution=9000))
            manager = SimpleNamespace(
                fields=store,
                coverage_planner=SimpleNamespace(
                    worldwide=SimpleNamespace(credentials=False)
                ),
            )
            ordered = store.snapshot().fields
            for latitude, longitude, instant in (
                (49.5, -125.5, START),
                (50, -125, START + timedelta(minutes=30)),
                (49.5, -125.5, START + timedelta(hours=2)),
                (0, 0, START),
            ):
                expected = current_coverage(manager, latitude, longitude, instant)
                with patch.object(
                    store, "snapshot", side_effect=AssertionError("Re-sorted")
                ):
                    actual = current_coverage(
                        manager,
                        latitude,
                        longitude,
                        instant,
                        selection=FieldSelection(ordered),
                    )
                self.assertEqual(expected, actual)

    def test_snapshot_priority_orders_finest_or_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FieldStore(directory)
            fine_old = field(resolution=500, start=START)
            coarse_new = field(resolution=9000, start=START + timedelta(hours=6))
            store.add(fine_old)
            store.add(coarse_new)
            default = store.snapshot()
            self.assertEqual(
                [f.id for f in default.fields], [fine_old.id, coarse_new.id]
            )
            self.assertEqual(default.priority, "finest")
            newest = store.snapshot("newest")
            self.assertEqual(
                [f.id for f in newest.fields], [coarse_new.id, fine_old.id]
            )
            self.assertEqual(newest.priority, "newest")
            self.assertIn(
                "newest run, finest resolution",
                newest.to_json()["selection_policy"],
            )
            self.assertEqual(store.snapshot("bogus").priority, "finest")

    def test_repeat_retrieval_keeps_original_provenance_and_disabled_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FieldStore(directory)
            original = field()
            store.add(original)
            store.enable(original.id, False)
            metadata = json.loads(original.metadata_json)
            metadata["retrieved_at"] = (START + timedelta(hours=1)).isoformat()
            repeated = replace(original, metadata_json=canonical_json(metadata))
            self.assertNotEqual(original.id, repeated.id)
            self.assertEqual(store.add(repeated), original.id)
            restored = FieldStore(directory)
            self.assertEqual(list(restored.fields), [original.id])
            self.assertEqual(restored.snapshot().fields, ())
            changed = replace(repeated, east=repeated.east + 0.01)
            self.assertEqual(store.add(changed), changed.id)
            self.assertEqual(len(store.fields), 2)

    def download_session(self, payload=b"forecast"):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value = Mock()
        response.url = "https://example.test/forecast.nc"
        response.status_code = 200
        response.headers = {"ETag": '"forecast-v1"'}
        response.iter_content.side_effect = lambda *_: iter([payload])
        return session

    def test_immutable_source_cache_survives_clients_and_checks_integrity(self):
        url = "https://example.test/20261011T18Z_MSC_CIOPS-West_PT009H.nc"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.download_session()
            a = DownloadClient(root / "a", threading.Event(), session, root / "cache")
            path = a.get(url)
            b = DownloadClient(root / "b", threading.Event(), session, root / "cache")
            response = session.get.return_value.__enter__.return_value
            response.status_code = 304
            self.assertEqual(b.get(url).read_bytes(), b"forecast")
            self.assertEqual(b.sources, a.sources)
            self.assertEqual(session.get.call_count, 2)
            self.assertEqual(
                session.get.call_args.kwargs["headers"],
                {"If-None-Match": '"forecast-v1"'},
            )
            b.get(url)
            self.assertEqual(session.get.call_count, 2)
            self.assertEqual(b.total, 0)
            (root / "cache" / path.name).write_bytes(b"damaged!")
            response.status_code = 200
            c = DownloadClient(root / "c", threading.Event(), session, root / "cache")
            self.assertEqual(c.get(url).read_bytes(), b"forecast")
            self.assertEqual(session.get.call_count, 3)
            self.assertEqual(session.get.call_args.kwargs["headers"], {})
            b.cancel.set()
            from gweatherrouting.core.marine.chs import DownloadCancelledError

            with self.assertRaises(DownloadCancelledError):
                b.get(url)

    def test_cached_run_is_replaced_when_provider_corrects_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = "https://example.test/20261011T18Z_MSC_CIOPS-West_PT009H.nc"
            session = self.download_session()
            a = DownloadClient(root / "a", threading.Event(), session, root / "cache")
            a.get(url)
            response = session.get.return_value.__enter__.return_value
            response.headers = {"ETag": '"forecast-v2"'}
            response.iter_content.side_effect = lambda *_: iter([b"corrected"])
            b = DownloadClient(root / "b", threading.Event(), session, root / "cache")
            self.assertEqual(b.get(url).read_bytes(), b"corrected")
            self.assertNotEqual(a.sources[0]["sha256"], b.sources[0]["sha256"])
            self.assertEqual(a.get(url).read_bytes(), b"forecast")

    def test_discovery_refreshes_and_new_forecast_run_is_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.download_session()
            client = DownloadClient(directory, threading.Event(), session)
            client.get(DATAMART_URL)
            client.get(DATAMART_URL)
            first = "https://example.test/20261011T18Z_MSC_CIOPS-West_PT009H.nc"
            second = first.replace("11T18Z", "12T00Z")
            client.get(first)
            client.get(second)
            self.assertEqual(session.get.call_count, 4)

    def test_source_cache_evicts_old_files_within_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            client = DownloadClient(
                directory, threading.Event(), self.download_session()
            )
            client.CACHE_LIMIT = 8
            first = "https://example.test/20261011T18Z_MSC_CIOPS-West_PT009H.nc"
            old = client.get(first)
            new = client.get(first.replace("009H", "010H"))
            self.assertFalse(old.exists())
            self.assertEqual(new.read_bytes(), b"forecast")

    def test_cache_without_http_validators_fetches_fresh_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = "https://example.test/20261011T18Z_MSC_CIOPS-West_PT009H.nc"
            session = self.download_session()
            response = session.get.return_value.__enter__.return_value
            response.headers = {}
            DownloadClient(root / "a", threading.Event(), session, root / "cache").get(
                url
            )
            response.iter_content.side_effect = lambda *_: iter([b"updated"])
            client = DownloadClient(
                root / "b", threading.Event(), session, root / "cache"
            )
            self.assertEqual(client.get(url).read_bytes(), b"updated")
            self.assertEqual(session.get.call_args.kwargs["headers"], {})

    def test_ciops_rollover_uses_retained_date_and_skips_unpublished_cycle(self):
        base = DATAMART_URL + "20261011/WXO-DD/model_ciops/salish-sea/500m/"
        name = (
            "20261011T12Z_MSC_CIOPS-SalishSea_SeaWaterVelocityX_"
            "DBS-0.5m_LatLon0.008x0.005_PT000H.nc"
        )
        pages = {
            DATAMART_URL: '<a href="20261011/">old</a><a href="20261012/">new</a>',
            base: '<a href="18/">18</a><a href="12/">12</a>',
            base
            + "12/000/": '<a href="'
            + name
            + '">X</a><a href="'
            + name.replace("VelocityX", "VelocityY")
            + '">Y</a>',
        }

        def get(url):
            if url in pages:
                return SimpleNamespace(read_text=lambda: pages[url])
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError(response=response)

        client = Mock()
        client.get.side_effect = get
        found = CIOPSProvider()._discover("salish-sea", client)
        self.assertEqual(found, (base, "12/", name))

    def test_ciops_selects_run_covering_requested_times(self):
        provider = CIOPSProvider()
        base = "https://example.test/"

        def name(hour):
            return (
                f"{START:%Y%m%d}T{hour:02d}Z_MSC_CIOPS-SalishSea_SeaWaterVelocityX_"
                "DBS-0.5m_LatLon0.008x0.005_PT000H.nc"
            )

        def listing(client, url):
            if url == base:
                return '<a href="12/">12</a><a href="00/">00</a>'
            value = name(12 if "12/000/" in url else 0)
            return (
                '<a href="'
                + value
                + '">X</a>'
                + value.replace("VelocityX", "VelocityY")
            )

        start = START.replace(hour=7)
        with patch.object(provider, "_listing", side_effect=listing):
            self.assertEqual(
                provider._run_at(
                    base,
                    "SalishSea",
                    "0.008x0.005",
                    Mock(),
                    start,
                    start + timedelta(hours=12),
                ),
                ("00/", name(0)),
            )
            self.assertIsNone(
                provider._run_at(
                    base,
                    "SalishSea",
                    "0.008x0.005",
                    Mock(),
                    start,
                    start + timedelta(hours=48),
                )
            )

    def test_ciops_does_not_treat_server_errors_as_missing_dates(self):
        response = requests.Response()
        response.status_code = 503
        client = Mock()
        client.get.side_effect = requests.HTTPError(response=response)
        with self.assertRaises(requests.HTTPError):
            CIOPSProvider()._discover("west", client)

    def test_copernicus_aborted_sdk_reports_account_action(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock()
            process.poll.return_value = 0
            process.returncode = 0

            def write_report(payload):
                report = json.loads(payload)["_result_file"]
                Path(report).write_text('{"error_type":"DownloadAborted"}')

            process.stdin.write.side_effect = write_report
            with patch(
                "gweatherrouting.core.marine.spatialproviders.subprocess.Popen",
                return_value=process,
            ):
                with self.assertRaisesRegex(ValueError, "Worldwide account"):
                    CopernicusProvider._subset(
                        dict(output_directory=directory), threading.Event()
                    )

    def test_copernicus_uses_reported_output_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "returned-name.nc"
            netcdf(path)
            client = Mock()
            client.subset.return_value = SimpleNamespace(file_path=path)
            value = CopernicusProvider().download(
                Area(-126, 49, -125, 50),
                START,
                START + timedelta(hours=1),
                directory,
                threading.Event(),
                client=client,
            )
            self.assertEqual(value.sample(49.5, -125.5, START).values, (4, 6))
            self.assertEqual(client.subset.call_args.kwargs["file_format"], "netcdf")

    def test_copernicus_missing_output_is_not_reported_as_size_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "no NetCDF file"):
                CopernicusProvider._check_subset_path(
                    Path(directory) / "missing.nc", directory
                )
            process = Mock()
            process.poll.return_value = 0
            process.returncode = 0
            stale = Path(directory) / "subset-result.json"
            stale.write_text('{"file_path":"stale.nc"}')
            with patch(
                "gweatherrouting.core.marine.spatialproviders.subprocess.Popen",
                return_value=process,
            ):
                with self.assertRaisesRegex(ValueError, "did not complete"):
                    CopernicusProvider._subset(
                        dict(output_directory=directory), threading.Event()
                    )
            self.assertFalse(stale.exists())

    def test_interpolation_keeps_east_north_components(self):
        value = field()
        result = value.sample(49.5, -125.5, START + timedelta(minutes=30))
        self.assertEqual(result.values, (1, 2))
        self.assertEqual(result.dataset_id, value.id)
        self.assertEqual(value.sample(49, 234, START).values, (1, 2))
        self.assertEqual(value.sample(48, -125, START).status, "outside_grid")
        self.assertEqual(
            value.sample(49, -125, START - timedelta(seconds=1)).status, "outside_time"
        )
        with self.assertRaises(ValueError):
            value.east.setflags(write=True)

    def test_mask_falls_back_to_available_selected_source(self):
        fine, coarse = field(masked=True), field(9000)
        for batch in (False, True):
            selection = FieldSelection((fine, coarse))
            results = (
                selection.sample_many([(49.5, -125.5)], [START])
                if batch
                else [selection.sample(49.5, -125.5, START)]
            )
            self.assertEqual(results[0].dataset_id, coarse.id)
            self.assertEqual(results[0].values, (1, 2))
            self.assertEqual(selection.used, {coarse.id})
            self.assertEqual(selection.missing, {})

    def test_unresolved_mask_retains_original_gap(self):
        fine, coarse = field(masked=True), field(9000, masked=True)
        for batch in (False, True):
            selection = FieldSelection((fine, coarse))
            result = (
                selection.sample_many([(49.5, -125.5)], [START])[0]
                if batch
                else selection.sample(49.5, -125.5, START)
            )
            self.assertEqual(result.status, "masked_or_missing")
            self.assertEqual(result.dataset_id, fine.id)
            self.assertFalse(selection.used)
            self.assertEqual(selection.missing, {"masked_or_missing": 1})

    def test_fallback_respects_disabled_fields(self):
        fine, coarse = field(masked=True), field(9000)
        with tempfile.TemporaryDirectory() as directory:
            store = FieldStore(directory)
            store.add(fine)
            store.add(coarse)
            store.enable(coarse.id, False)
            selection = store.snapshot()
            self.assertEqual(
                selection.sample(49.5, -125.5, START).status, "masked_or_missing"
            )
            self.assertFalse(selection.used)

    def test_batch_fallback_preserves_valid_fine_samples(self):
        fine, coarse = field(masked=True), field(9000)
        positions = [(49.5, -125.5), (50, -125), (48, -125)]
        scalar, batch = FieldSelection((fine, coarse)), FieldSelection((fine, coarse))
        expected = [scalar.sample(*p, START) for p in positions]
        actual = batch.sample_many(positions, [START] * len(positions))
        self.assertEqual(actual, expected)
        self.assertEqual(actual[1].dataset_id, fine.id)
        self.assertEqual(batch.used, scalar.used)
        self.assertEqual(batch.missing, scalar.missing)

    def test_bounded_nearest_corner_retains_time_interpolation_and_provenance(self):
        original = field(masked=True)
        east, north = original.east.copy(), original.north.copy()
        east[1] *= 3
        north[1] *= 3
        value = replace(
            original,
            latitudes=np.array([49, 49.004]),
            longitudes=np.array([-126, -125.996]),
            east=east,
            north=north,
        )
        point = (49.003, -125.997)
        instant = START + timedelta(minutes=30)
        self.assertEqual(value.sample(*point, instant).status, "masked_or_missing")
        calls = []

        def valid(start, end):
            calls.append((start, end))
            return True

        scalar = FieldSelection([value], valid)
        result = scalar.sample(*point, instant)
        self.assertEqual(result.values, (2, 4))
        self.assertEqual(result.qc_flags, ("nearest_wet_cell",))
        location = json.loads(result.detail)
        self.assertLess(location["distance_m"], 150)
        self.assertEqual(
            (location["latitude"], location["longitude"]), (49.004, -125.996)
        )
        batch = FieldSelection([value], valid)
        self.assertEqual(batch.sample_many([point], [instant]), [result])
        self.assertEqual(batch.used, {value.id})
        self.assertFalse(batch.missing)
        self.assertTrue(calls)

    def test_nearest_rejects_land_crossings_distance_and_time_gaps(self):
        original = field(masked=True)
        value = replace(
            original,
            latitudes=np.array([49, 49.004]),
            longitudes=np.array([-126, -125.996]),
        )
        point = (49.003, -125.997)
        for candidate, validator, instant in (
            (value, lambda a, b: False, START),
            (original, lambda a, b: True, START),  # Corners beyond 500 metres.
            (
                replace(
                    value,
                    times=np.array(
                        [START.timestamp(), (START + timedelta(hours=2)).timestamp()]
                    ),
                ),
                lambda a, b: True,
                START + timedelta(minutes=30),
            ),
        ):
            scalar = FieldSelection([candidate], validator)
            batch = FieldSelection([candidate], validator)
            expected = scalar.sample(*point, instant)
            self.assertNotEqual(expected.status, "available")
            self.assertEqual(batch.sample_many([point], [instant]), [expected])

    def test_automatic_priority_latest_run_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FieldStore(directory)
            coarse, fine = field(9000), field()
            for value in (coarse, fine):
                store.add(value)
            restored = FieldStore(directory)
            self.assertFalse(restored.errors)
            selection = restored.snapshot()
            self.assertEqual(selection.sample(49.5, -125.5, START).dataset_id, fine.id)
            self.assertEqual(selection.to_json()["actual_sources"], [fine.id])
            restored.enable(fine.id, False)
            self.assertEqual(
                restored.snapshot().sample(49.5, -125.5, START).dataset_id, coarse.id
            )
            self.assertEqual(selection.sample(49.5, -125.5, START).dataset_id, fine.id)

    def test_bad_cache_checksum_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FieldStore(directory)
            value = field()
            store.add(value)
            path = Path(directory) / value.id / "manifest.json"
            saved = json.loads(path.read_text())
            metadata = json.loads(saved["metadata_json"])
            metadata["provider"] = "tampered"
            saved["metadata_json"] = canonical_json(metadata)
            path.write_text(json.dumps(saved))
            restored = FieldStore(directory)
            self.assertFalse(restored.fields)
            self.assertIn("checksum", restored.errors[0])

    def test_route_bounds_cross_date_line_without_covering_globe(self):
        area = Area.from_points([(10, 179), (11, -179)])
        self.assertGreater(area.west, area.east)
        self.assertEqual(len(area.parts()), 2)
        tasks = CoveragePlanner().plan(area)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t.provider == "copernicus-marine" for t in tasks))
        canada = CoveragePlanner().plan(Area(-126, 48, -123, 50))
        self.assertEqual(
            [t.domain for t in canada], ["salish-sea", "west", "wcofs", "global"]
        )

    def test_regional_failure_does_not_hide_worldwide_data(self):
        regional, worldwide, coastal = Mock(), Mock(), Mock()
        regional.download.side_effect = ValueError("No forecast")
        worldwide.download.return_value = field(9000)
        coastal.download.return_value = field(4000)
        fields, issues = CoveragePlanner(regional, worldwide, coastal=coastal).download(
            Area(-126, 48, -123, 50), START, START + timedelta(hours=1)
        )
        self.assertEqual(len(fields), 2)
        self.assertEqual(len(issues), 2)
        worldwide.download.assert_called_once()
        coastal.download.assert_called_once()

    def test_netcdf_coordinate_reordering_and_worldwide_composition(self):
        gdal.UseExceptions()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smoc.nc"
            netcdf(path)
            area = Area(-126, 49, -125, 50)
            component = read_component(path, "uo", area)
            np.testing.assert_array_equal(component[1], [49, 50])
            np.testing.assert_array_equal(component[2], [-126, -125])
            client = Mock()
            provider = CopernicusProvider()
            provider.configure("example-user", "test-secret")
            value = provider.download(
                area,
                START,
                START + timedelta(hours=1),
                directory,
                threading.Event(),
                client=client,
            )
            self.assertEqual(value.sample(49.5, -125.5, START).values, (4, 6))
            self.assertEqual(
                client.subset.call_args.kwargs["variables"],
                ["uo", "vo", "utide", "vtide"],
            )
            self.assertIsNone(value.manifest()["run"])
            self.assertNotIn("output_directory", value.manifest()["request"])
            self.assertNotIn("test-secret", value.metadata_json)
            self.assertNotIn("example-user", value.metadata_json)
            self.assertEqual(client.subset.call_args.kwargs["password"], "test-secret")

    def test_cancellation_and_large_subsets_do_not_request_data(self):
        client = Mock()
        cancel = threading.Event()
        cancel.set()
        from gweatherrouting.core.marine.chs import DownloadCancelledError

        with self.assertRaises(DownloadCancelledError):
            CopernicusProvider().download(
                Area(-180, -80, 180, 80),
                START,
                START + timedelta(days=7),
                "unused",
                cancel,
                client=client,
            )
        cancel.clear()
        with self.assertRaisesRegex(ValueError, "smaller"):
            CopernicusProvider().download(
                Area(-180, -80, 180, 80),
                START,
                START + timedelta(days=7),
                "unused",
                cancel,
                client=client,
            )
        client.subset.assert_not_called()
