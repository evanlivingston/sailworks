import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from sailworks.core import arco
from sailworks.core.arco import ArcoDownloadService, ArcoError
from sailworks.core.era5 import DATASET, job_key, plan_request

NOW = datetime(2025, 2, 20, tzinfo=timezone.utc)
STEP = arco.GRID_SPACING
# A reduced but still 0.25 degree store: the grid checks only require a
# uniform lattice, so tests do not need the full 721x1440 global field.
LAT = np.arange(60.0, 29.999, -STEP)
LON = np.arange(0.0, 360.0, STEP)
UTC = timezone.utc


def planned(start, end, area=(50, -10, 40, 5), fields=("wind",)):
    return plan_request(
        datetime.fromisoformat(start).replace(tzinfo=UTC),
        datetime.fromisoformat(end).replace(tzinfo=UTC),
        area,
        now=NOW,
        fields=fields,
    )


def availability_attrs(
    start="2025-01-01T00:00:00",
    stable="2025-01-01T02:00:00",
    era5t="2025-01-01T03:00:00",
):
    return {
        "valid_time_start": f"{start}.000000000",
        "valid_time_stop": f"{stable}.000000000",
        "valid_time_stop_era5t": f"{era5t}.000000000",
    }


def hourly(*hours):
    return tuple(datetime(2025, 1, 1, hour, tzinfo=UTC) for hour in hours)


def make_data(times):
    shape = (len(times), LAT.size, LON.size)
    rows, columns, hours = np.indices(shape, dtype=float)
    return {
        arco.U_VARIABLE: 1.0 + 0.5 * hours + 0.01 * rows - 0.001 * columns,
        # Every value is negative so a sign flip is visible in the GRIB.
        arco.V_VARIABLE: -5.0 - 0.02 * rows + 0.001 * columns,
    }


class FakeIndex:
    def __init__(self, times):
        self._lookup = {
            np.datetime64(stamp.replace(tzinfo=None), "ns"): index
            for index, stamp in enumerate(times)
        }

    def get_indexer(self, keys):
        return np.array(
            [self._lookup.get(np.datetime64(key, "ns"), -1) for key in keys],
            dtype=int,
        )


class FakeVariable:
    def __init__(self, dataset, data):
        self._dataset = dataset
        self._data = data
        self._indexers = []

    def isel(self, **indexers):
        clone = FakeVariable(self._dataset, self._data)
        clone._indexers = list(self._indexers) + list(indexers.items())
        return clone

    @property
    def values(self):
        result = self._data
        axes = ["time", "latitude", "longitude"]
        for name, selector in self._indexers:
            axis = axes.index(name)
            if np.ndim(selector) == 0:
                result = np.take(result, int(selector), axis=axis)
                axes.pop(axis)
            else:
                result = np.take(result, np.asarray(selector), axis=axis)
        if self._dataset.on_read is not None:
            self._dataset.on_read()
        return result


class FakeDataset:
    def __init__(self, times, data, attrs=None, on_read=None, lat=LAT, lon=LON):
        self.times = tuple(times)
        self.data = data
        self.attrs = availability_attrs() if attrs is None else attrs
        self.lat = lat
        self.lon = lon
        self.on_read = on_read
        self.indexes = {"time": FakeIndex(self.times)}
        self.closed = False

    def __getitem__(self, name):
        if name == "latitude":
            return SimpleNamespace(values=self.lat)
        if name == "longitude":
            return SimpleNamespace(values=self.lon)
        return FakeVariable(self, self.data[name])

    def __contains__(self, name):
        return name in self.data or name in ("latitude", "longitude", "time")

    def close(self):
        self.closed = True


class FakeEccodes:
    def __init__(self):
        self.messages = []
        self.samples = []

    def codes_grib_new_from_samples(self, name):
        self.samples.append(name)
        return {}

    def codes_set(self, handle, key, value):
        handle[key] = value

    def codes_set_values(self, handle, values):
        handle["values"] = np.array(values, dtype=float)

    def codes_write(self, handle, stream):
        self.messages.append(dict(handle))
        stream.write(b"grib")

    def codes_release(self, handle):
        handle.clear()


def accepting_validator(path, piece):
    assert Path(path).stat().st_size > 0
    hours = int((piece.end - piece.start).total_seconds() // 3600) + 1
    return {"reader": "test", "validated_hours": hours}


def store_for(tmp_path):
    from sailworks.core.tranchestore import TrancheStore

    return TrancheStore(tmp_path)


def test_arco_reads_every_hour_and_publishes_arco_labels(tmp_path):
    store = store_for(tmp_path)
    times = hourly(0, 1, 2)
    data = make_data(times)
    data[arco.U_VARIABLE][1, LAT.size // 2, 0] = np.nan
    eccodes = FakeEccodes()
    opened = []
    fake = FakeDataset(times, data)

    def opener(url):
        opened.append(url)
        return fake

    service = ArcoDownloadService(
        store, opener=opener, encoder=eccodes, validator=accepting_validator
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T02:00")
    service._run(plan, threading.Event(), False)

    assert opened == [arco.STORE_URL]
    # The handle stays cached for the next plan; an explicit close releases it.
    assert fake.closed is False
    service.close()
    assert fake.closed is True
    assert eccodes.samples == ["regular_ll_sfc_grib2"] * 6
    assert [message["shortName"] for message in eccodes.messages] == [
        "10u",
        "10v",
    ] * 3
    for index, message in enumerate(eccodes.messages):
        assert message["dataDate"] == 20250101
        assert message["dataTime"] == (index // 2) * 100
        assert message["typeOfLevel"] == "heightAboveGround"
        assert message["level"] == 10
        assert message["centre"] == 98
        assert message["forecastTime"] == 0
        assert message["startStep"] == message["endStep"] == 0
        assert message["gridType"] == "regular_ll"
        assert message["iScansNegatively"] == 0
        assert message["jScansPositively"] == 0
        assert message["bitmapPresent"] == 1
        assert message["Ni"] == 61
        assert message["Nj"] == 41
        assert message["latitudeOfFirstGridPointInDegrees"] == 50.0
        assert message["latitudeOfLastGridPointInDegrees"] == 40.0
        assert message["longitudeOfFirstGridPointInDegrees"] == -10.0
        assert message["longitudeOfLastGridPointInDegrees"] == 5.0

    # v keeps its native negative sign and the NaN became a bitmap value.
    assert np.all(eccodes.messages[1]["values"] < 0)
    assert 9999.0 in eccodes.messages[2]["values"]

    lat_index = np.where((LAT <= 50) & (LAT >= 40))[0]
    lon_index = np.concatenate([np.where(LON >= 350)[0], np.where(LON <= 5)[0]])
    expected = data[arco.U_VARIABLE][0][np.ix_(lat_index, lon_index)].ravel()
    expected[~np.isfinite(expected)] = 9999.0
    np.testing.assert_array_equal(eccodes.messages[0]["values"], expected)

    entries = json.loads((tmp_path / "era5-history.json").read_text())
    assert len(entries) == 1
    entry = entries[0]
    assert entry["dataset"] == arco.DATASET_LABEL
    assert entry["dataset"] != DATASET
    assert entry["source"] == arco.SOURCE_LABEL
    assert entry["start"] == plan.start.isoformat()
    assert entry["end"] == plan.end.isoformat()
    assert entry["area"] == list(plan.area)
    assert entry["variables"] == list(arco.WIND_VARIABLES)
    assert entry["provenance"]["store"] == arco.STORE_URL
    assert entry["provenance"]["preliminary_era5t"] is False
    assert (tmp_path / entry["artifact"]).is_file()

    progress = service.progress_for(plan)
    assert progress["done"] == progress["total"] == 1
    assert progress["stage"] == "Complete"


def test_arco_progress_reports_each_completed_hour(tmp_path):
    store = store_for(tmp_path)
    times = hourly(0, 1, 2)
    service = ArcoDownloadService(
        store,
        opener=lambda url: FakeDataset(times, make_data(times)),
        encoder=FakeEccodes(),
        validator=accepting_validator,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T02:00")
    seen = []
    original = service._set_progress

    def record(key, *, done=None, total=None, stage=None, detail=None):
        original(key, done=done, total=total, stage=stage, detail=detail)
        if stage and "ARCO wind hour" in stage:
            entry = service.progress_for(plan)
            seen.append((stage, entry["done"], entry["total"]))

    service._set_progress = record
    service._run(plan, threading.Event(), False)

    assert [stage for stage, _done, _total in seen] == [
        "Chunk 1/1: ARCO wind hour 1/3 · 2025-01-01 00:00 UTC",
        "Chunk 1/1: ARCO wind hour 2/3 · 2025-01-01 01:00 UTC",
        "Chunk 1/1: ARCO wind hour 3/3 · 2025-01-01 02:00 UTC",
    ]
    # Hour updates never invent chunk progress: done/total stay per-chunk.
    assert {(done, total) for _stage, done, total in seen} == {(0, 1)}
    final = service.progress_for(plan)
    assert final["done"] == final["total"] == 1
    # The finer hour tier is reported separately for the second bar.
    assert final["detail"] == {"done": 3, "total": 3}


def test_arco_greenwich_crossing_is_monotonic_and_padded(tmp_path):
    store = store_for(tmp_path)
    times = hourly(0)
    data = make_data(times)
    eccodes = FakeEccodes()
    service = ArcoDownloadService(
        store,
        opener=lambda url: FakeDataset(times, data),
        encoder=eccodes,
        validator=accepting_validator,
    )
    # Bounds between grid nodes: the selection must expand outward, and the
    # box crosses Greenwich in a 0..360 store.
    plan = planned("2025-01-01T00:00", "2025-01-01T00:00", area=(50.1, -9.9, 39.9, 4.9))
    service._run(plan, threading.Event(), False)

    message = eccodes.messages[0]
    assert message["latitudeOfFirstGridPointInDegrees"] == 50.25
    assert message["latitudeOfLastGridPointInDegrees"] == 39.75
    assert message["longitudeOfFirstGridPointInDegrees"] == -10.0
    assert message["longitudeOfLastGridPointInDegrees"] == 5.0
    assert message["Nj"] == 43
    assert message["Ni"] == 61

    lat_index = np.where((LAT <= 50.25) & (LAT >= 39.75))[0]
    lon_index = np.concatenate([np.where(LON >= 350)[0], np.where(LON <= 5)[0]])
    expected = data[arco.U_VARIABLE][0][np.ix_(lat_index, lon_index)].ravel()
    np.testing.assert_array_equal(message["values"], expected)


@pytest.mark.parametrize(
    "area, expected_first, expected_last, expected_ni",
    [
        ((50, -180, 40, -170), -180.0, -170.0, 41),
        ((50, 170, 40, 180), 170.0, 180.0, 41),
    ],
)
def test_arco_longitude_boundaries_stay_contiguous(
    tmp_path, area, expected_first, expected_last, expected_ni
):
    store = store_for(tmp_path)
    times = hourly(0)
    eccodes = FakeEccodes()
    service = ArcoDownloadService(
        store,
        opener=lambda url: FakeDataset(times, make_data(times)),
        encoder=eccodes,
        validator=accepting_validator,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T00:00", area=area)
    service._run(plan, threading.Event(), False)

    message = eccodes.messages[0]
    assert message["longitudeOfFirstGridPointInDegrees"] == expected_first
    assert message["longitudeOfLastGridPointInDegrees"] == expected_last
    assert message["Ni"] == expected_ni
    assert message["iScansNegatively"] == 0


def test_arco_missing_hour_fails_without_publication(tmp_path):
    store = store_for(tmp_path)
    fake = FakeDataset(hourly(0, 2), make_data(hourly(0, 2)))
    eccodes = FakeEccodes()
    service = ArcoDownloadService(
        store, opener=lambda url: fake, encoder=eccodes, validator=accepting_validator
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T02:00")

    with pytest.raises(ArcoError, match="no hourly data"):
        service._run(plan, threading.Event(), False)

    assert eccodes.messages == []
    assert not (tmp_path / "era5-history.json").exists()
    assert fake.closed


def test_arco_submit_reports_error_without_publishing(tmp_path):
    store = store_for(tmp_path)
    fake = FakeDataset(hourly(0, 2), make_data(hourly(0, 2)))
    service = ArcoDownloadService(
        store,
        opener=lambda url: fake,
        encoder=FakeEccodes(),
        validator=accepting_validator,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T02:00")
    thread = service.submit(plan)
    thread.join(10)

    assert not thread.is_alive()
    assert service.key_for(plan) == job_key(plan)
    assert "no hourly data" in service.error_for(job_key(plan))
    assert service.progress_for(plan)["stage"] == "Failed"
    assert not (tmp_path / "era5-history.json").exists()


def test_arco_rejects_unsupported_time_before_reading(tmp_path):
    store = store_for(tmp_path)
    eccodes = FakeEccodes()
    fake = FakeDataset(hourly(0), make_data(hourly(0)))
    service = ArcoDownloadService(
        store,
        opener=lambda url: fake,
        encoder=eccodes,
        validator=accepting_validator,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T04:00")

    with pytest.raises(ArcoError, match="ARCO ERA5 ends at"):
        service._run(plan, threading.Event(), False)

    assert fake.closed
    assert eccodes.messages == []
    assert not (tmp_path / "era5-history.json").exists()


def test_arco_rejects_missing_variable_without_publication(tmp_path):
    store = store_for(tmp_path)
    data = make_data(hourly(0))
    data.pop(arco.V_VARIABLE)
    eccodes = FakeEccodes()
    service = ArcoDownloadService(
        store,
        opener=lambda url: FakeDataset(hourly(0), data),
        encoder=eccodes,
        validator=accepting_validator,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T00:00")

    with pytest.raises(ArcoError, match=arco.V_VARIABLE):
        service._run(plan, threading.Event(), False)

    assert eccodes.messages == []
    assert not (tmp_path / "era5-history.json").exists()


def test_arco_rejects_non_wind_fields_without_opening_store(tmp_path):
    store = store_for(tmp_path)
    opened = []
    service = ArcoDownloadService(
        store,
        opener=lambda url: opened.append(url),
        encoder=FakeEccodes(),
        validator=accepting_validator,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T00:00", fields=("pressure",))

    with pytest.raises(ArcoError, match="10 m wind only"):
        service._run(plan, threading.Event(), False)

    assert opened == []
    assert not (tmp_path / "era5-history.json").exists()


def test_arco_preliminary_era5t_is_recorded(tmp_path):
    store = store_for(tmp_path)
    times = hourly(0, 1, 2, 3)
    data = make_data(times)
    service = ArcoDownloadService(
        store,
        opener=lambda url: FakeDataset(times, data),
        encoder=FakeEccodes(),
        validator=accepting_validator,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T03:00")
    service._run(plan, threading.Event(), False)

    entry = json.loads((tmp_path / "era5-history.json").read_text())[0]
    assert entry["provenance"]["preliminary_era5t"] is True
    assert "preliminary ERA5T" in entry["provenance"]["note"]


def test_arco_cancel_between_hours_never_publishes_or_touches_cds(tmp_path):
    store = store_for(tmp_path)
    cancel = threading.Event()
    times = hourly(0, 1, 2)
    reads = {"count": 0}

    def on_read():
        reads["count"] += 1
        if reads["count"] == 2:
            cancel.set()

    fake = FakeDataset(times, make_data(times), on_read=on_read)
    eccodes = FakeEccodes()
    service = ArcoDownloadService(
        store,
        opener=lambda url: fake,
        encoder=eccodes,
        validator=accepting_validator,
        workers=1,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T02:00")

    with pytest.raises(ArcoError, match="canceled"):
        service._run(plan, cancel, False)

    # The first hour was read and encoded before the cancellation was seen,
    # but no partial chunk was published.
    assert len(eccodes.messages) == 2
    assert not (tmp_path / "era5-history.json").exists()
    assert fake.closed
    with pytest.raises(ArcoError, match="never submits CDS"):
        service.client_factory()


def test_arco_cancel_before_start_does_not_open_store(tmp_path):
    opened = []
    cancel = threading.Event()
    cancel.set()
    service = ArcoDownloadService(
        store_for(tmp_path),
        opener=lambda url: opened.append(url),
        encoder=FakeEccodes(),
        validator=accepting_validator,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T01:00")

    with pytest.raises(ArcoError, match="canceled"):
        service._run(plan, cancel, False)

    assert opened == []


def test_arco_reuses_existing_catalogued_wind(tmp_path):
    store = store_for(tmp_path)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    artifact = tmp_path / "artifacts" / "existing"
    artifact.write_bytes(b"existing")
    plan = planned("2025-01-01T00:00", "2025-01-01T01:00")
    catalog = tmp_path / "era5-history.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "dataset": DATASET,
                    "source": "CDS ERA5 hourly single levels reanalysis",
                    "artifact": "artifacts/existing",
                    "start": plan.start.isoformat(),
                    "end": plan.end.isoformat(),
                    "area": list(plan.area),
                    "variables": list(arco.WIND_VARIABLES),
                }
            ]
        )
    )

    def opener(url):
        raise AssertionError("cached wind must not be downloaded again")

    service = ArcoDownloadService(
        store,
        opener=opener,
        encoder=FakeEccodes(),
        validator=accepting_validator,
    )
    service._run(plan, threading.Event(), False)

    assert service.progress_for(plan)["stage"] == "Already available"
    assert json.loads(catalog.read_text()) == [
        {
            "dataset": DATASET,
            "source": "CDS ERA5 hourly single levels reanalysis",
            "artifact": "artifacts/existing",
            "start": plan.start.isoformat(),
            "end": plan.end.isoformat(),
            "area": list(plan.area),
            "variables": list(arco.WIND_VARIABLES),
        }
    ]


def test_arco_reports_mixed_cached_source_when_store_fails(tmp_path):
    store = store_for(tmp_path)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    (tmp_path / "artifacts" / "existing").write_bytes(b"existing")
    first = planned("2025-01-01T00:00", "2025-01-01T23:00")
    plan = planned("2025-01-01T00:00", "2025-01-02T00:00")
    (tmp_path / "era5-history.json").write_text(
        json.dumps(
            [
                {
                    "dataset": DATASET,
                    "source": "CDS ERA5 hourly single levels reanalysis",
                    "artifact": "artifacts/existing",
                    "start": first.start.isoformat(),
                    "end": first.end.isoformat(),
                    "area": list(first.area),
                    "variables": list(arco.WIND_VARIABLES),
                }
            ]
        )
    )

    def opener(url):
        raise OSError("network down")

    service = ArcoDownloadService(
        store, opener=opener, encoder=FakeEccodes(), validator=accepting_validator
    )

    with pytest.raises(ArcoError, match="cannot open"):
        service._run(plan, threading.Event(), False)

    assert "cached source may differ" in service.progress_for(plan)["stage"]


def test_arco_never_reads_cds_pending_state(tmp_path):
    (tmp_path / "era5-pending.json").write_text(
        json.dumps(
            [
                {
                    "dataset": DATASET,
                    "start": "2025-01-01T00:00:00+00:00",
                    "end": "2025-01-01T01:00:00+00:00",
                    "area": [50, -10, 40, 5],
                    "variables": list(arco.WIND_VARIABLES),
                    "request_id": "queued",
                }
            ]
        )
    )
    service = ArcoDownloadService(SimpleNamespace(root=tmp_path))

    assert service._load_pending() == []
    assert service.pending_requests() == []
    assert service.pending_count() == 0
    assert service.resume_pending() == 0
    service._submit_with_backoff(None, threading.Event())
    with pytest.raises(ArcoError, match="never submits CDS"):
        service.client_factory()


def test_arco_open_store_uses_anonymous_gcs(monkeypatch):
    calls = {}

    class FakeXarray:
        @staticmethod
        def open_zarr(url, **kwargs):
            calls["url"] = url
            calls.update(kwargs)
            return object()

    monkeypatch.setitem(sys.modules, "xarray", FakeXarray)
    arco.open_store()

    assert calls["url"] == arco.STORE_URL
    assert calls["url"].startswith("gs://")
    assert calls["storage_options"] == {"token": "anon"}
    assert calls["consolidated"] is True


def test_arco_rejects_non_uniform_grid(tmp_path):
    store = store_for(tmp_path)
    lat = LAT.copy()
    lat[10] += 0.01
    eccodes = FakeEccodes()
    service = ArcoDownloadService(
        store,
        opener=lambda url: FakeDataset(hourly(0), make_data(hourly(0)), lat=lat),
        encoder=eccodes,
        validator=accepting_validator,
    )
    plan = planned("2025-01-01T00:00", "2025-01-01T00:00")

    with pytest.raises(ArcoError, match="uniform 0.25 degree grid"):
        service._run(plan, threading.Event(), False)

    assert eccodes.messages == []
    assert not (tmp_path / "era5-history.json").exists()


class ExplodingClose(FakeDataset):
    def close(self):
        raise RuntimeError("close failed")


def test_arco_reuses_one_store_open_across_plans(tmp_path):
    times = hourly(0, 1)
    dataset = FakeDataset(times, make_data(times))
    opened = []

    def opener(url):
        opened.append(url)
        return dataset

    service = ArcoDownloadService(
        store_for(tmp_path),
        opener=opener,
        encoder=FakeEccodes(),
        validator=accepting_validator,
    )
    service._run(
        planned("2025-01-01T00:00", "2025-01-01T00:00"), threading.Event(), False
    )
    service._run(
        planned("2025-01-01T01:00", "2025-01-01T01:00"), threading.Event(), False
    )

    assert opened == [service.store_url]
    assert dataset.closed is False
    service.close()


def test_arco_close_is_idempotent_and_reopens_on_next_plan(tmp_path):
    times = hourly(0, 1)
    datasets = []

    def opener(url):
        dataset = FakeDataset(times, make_data(times))
        datasets.append(dataset)
        return dataset

    service = ArcoDownloadService(
        store_for(tmp_path),
        opener=opener,
        encoder=FakeEccodes(),
        validator=accepting_validator,
    )
    service._run(
        planned("2025-01-01T00:00", "2025-01-01T00:00"), threading.Event(), False
    )
    service.close()
    service.close()
    assert datasets[0].closed is True

    service._run(
        planned("2025-01-01T01:00", "2025-01-01T01:00"), threading.Event(), False
    )
    assert len(datasets) == 2
    assert datasets[1].closed is False
    service.close()


def test_arco_close_failure_does_not_mask_acquisition_error(tmp_path):
    times = hourly(0, 1)
    data = make_data(times)
    del data[arco.V_VARIABLE]
    fake = ExplodingClose(times, data)
    service = ArcoDownloadService(
        store_for(tmp_path),
        opener=lambda url: fake,
        encoder=FakeEccodes(),
        validator=accepting_validator,
    )
    with pytest.raises(ArcoError, match="missing the 10m_v_component_of_wind"):
        service._run(
            planned("2025-01-01T00:00", "2025-01-01T00:00"),
            threading.Event(),
            False,
        )


def test_direct_chunk_reader_decodes_a_blosc_chunk(monkeypatch):
    import json

    import numcodecs
    import numpy as np

    import sailworks.core.arco as arco

    compressor = {
        "id": "blosc",
        "cname": "lz4",
        "clevel": 5,
        "shuffle": 1,
        "blocksize": 0,
    }
    data = np.arange(1 * 4 * 5, dtype="<f4").reshape(1, 4, 5)
    raw = numcodecs.get_codec(compressor).encode(data)
    meta = {
        "chunks": [1, 4, 5],
        "shape": [10, 4, 5],
        "dtype": "<f4",
        "filters": None,
        "compressor": compressor,
    }
    reader = arco._ChunkReader("gs://bucket/store")
    paths = []

    def fake_get(path):
        paths.append(path)
        if path.endswith(".zarray"):
            return json.dumps(meta).encode()
        return raw

    monkeypatch.setattr(reader, "_get", fake_get)

    out = reader.read("u10", 7, np.array([1, 3]), np.array([0, 4]))

    assert reader.base == "https://storage.googleapis.com/bucket/store"
    assert out.shape == (2, 2)
    assert (out[0, 0], out[1, 1]) == (data[0, 1, 0], data[0, 3, 4])
    assert "u10/7.0.0" in paths


def test_direct_chunk_reader_rejects_a_changed_layout(monkeypatch):
    import json

    import numpy as np
    import pytest

    import sailworks.core.arco as arco

    reader = arco._ChunkReader("gs://bucket/store")
    monkeypatch.setattr(
        reader,
        "_get",
        lambda path: json.dumps(
            {
                "chunks": [2, 4, 5],
                "shape": [10, 4, 5],
                "dtype": "<f4",
                "filters": None,
                "compressor": None,
            }
        ).encode(),
    )

    with pytest.raises(arco.ArcoError, match="layout changed"):
        reader.read("u10", 0, np.array([0, 1]), np.array([0, 1]))
