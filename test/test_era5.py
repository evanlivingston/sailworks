from datetime import datetime, timezone

import pytest

from sailworks.core.era5 import (
    Era5DownloadService,
    Era5Error,
    plan_request,
    split_request,
    validate_grib,
)
from pathlib import Path
import hashlib
import json
import threading


def plan(start, end, area=(50, -10, 40, 5)):
    return plan_request(
        datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        area,
        now=datetime(2025, 2, 20, tzinfo=timezone.utc),
    )


def test_plan_inclusive_utc_and_contract():
    planned = plan("2025-01-31T23:00", "2025-02-01T01:00")
    pieces = split_request(planned)
    assert [(p.start.hour, p.end.hour) for p in pieces] == [(23, 23), (0, 1)]
    assert pieces[0].request["month"] == ["01"]
    assert pieces[1].request["month"] == ["02"]
    assert planned.request["product_type"] == ["reanalysis"]
    assert planned.request["data_format"] == "grib"
    assert planned.request["download_format"] == "unarchived"


def test_wave_request_and_pending_identity(tmp_path):
    from types import SimpleNamespace
    from sailworks.core.era5 import FIELD_VARIABLES

    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    fields = ("wave_height", "wave_period", "wave_direction")
    wave = plan_request(start, start, (50, -10, 40, 5), fields=fields)
    wind = plan_request(start, start, (50, -10, 40, 5), fields=("wind",))
    assert wave.request["variable"] == [FIELD_VARIABLES[f][0] for f in fields]
    service = Era5DownloadService(SimpleNamespace(root=tmp_path))
    service._remember_pending(wind, "wind")
    service._remember_pending(wave, "wave")
    assert service._pending_entry(service._load_pending(), wave)["request_id"] == "wave"
    assert len(service.pending_requests()) == 2


def test_validate_and_publish_wave_history(tmp_path):
    from types import SimpleNamespace
    from sailworks.core.era5 import publish_history_display
    from sailworks.core.tranchestore import TrancheStore

    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 1, 1, tzinfo=timezone.utc)
    fields = ("wave_height", "wave_period", "wave_direction")
    piece = plan_request(start, end, (50, -10, 40, 5), fields=fields)

    class Band:
        def __init__(self, element, stamp):
            self.element, self.stamp = element, stamp

        def GetMetadata(self):
            return {
                "GRIB_ELEMENT": self.element,
                "GRIB_SHORT_NAME": "0-SFC",
                "GRIB_VALID_TIME": str(self.stamp),
            }

    bands = [
        Band(element, int(stamp.timestamp()))
        for stamp in (start, end)
        for element in ("swh", "mwp", "mwd")
    ]
    dataset = SimpleNamespace(
        RasterCount=len(bands), GetRasterBand=lambda n: bands[n - 1]
    )
    assert (
        validate_grib(tmp_path / "x", piece, dataset_opener=lambda _: dataset)[
            "validated_hours"
        ]
        == 2
    )
    store = TrancheStore(tmp_path)
    artifact = tmp_path / "waves.grib"
    artifact.write_bytes(b"waves")
    entries = [
        {
            "artifact": artifact.name,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "area": piece.area,
            "variables": piece.request["variable"],
        }
    ]
    tranche = publish_history_display(SimpleNamespace(store=store), entries)
    assert tranche.entries[0].fields == fields
    assert all(times == (start, end) for _, times in tranche.entries[0].field_times)


@pytest.mark.parametrize("area", [(10, 170, 0, -170), (91, 0, 0, 10)])
def test_rejects_invalid_and_dateline_area(area):
    with pytest.raises(ValueError):
        plan("2025-01-01T00:00", "2025-01-01T00:00", area)


def test_rejects_latency_and_bounds():
    with pytest.raises(ValueError, match="five days"):
        plan_request(
            datetime(2025, 1, 30, tzinfo=timezone.utc),
            datetime(2025, 1, 30, tzinfo=timezone.utc),
            (50, -10, 40, 5),
            now=datetime(2025, 2, 1, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="maximum"):
        plan("2024-12-01T00:00", "2025-01-01T00:00")


def test_rejects_pre_era5_and_grid_times_bound():
    with pytest.raises(ValueError, match="1940-01-01"):
        plan_request(
            datetime(1939, 12, 31, 23, tzinfo=timezone.utc),
            datetime(1939, 12, 31, 23, tzinfo=timezone.utc),
            (1, 1, 0, 2),
            now=datetime(2025, 2, 1, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="grid-size"):
        plan("2025-01-01T00:00", "2025-01-31T23:00", (90, -180, -90, 180))


def test_gdal_validator_requires_all_fields_and_times(tmp_path):
    planned = plan("2025-01-01T00:00", "2025-01-01T00:00")

    class Band:
        def __init__(self, element, short):
            self.element, self.short = element, short

        def GetMetadata(self):
            return {
                "GRIB_ELEMENT": self.element,
                "GRIB_SHORT_NAME": self.short,
                "GRIB_VALID_TIME": "1735689600 sec UTC",
            }

    class Dataset:
        RasterCount = 3

        def GetRasterBand(self, number):
            return [
                Band("UGRD", "10-HTGL"),
                Band("VGRD", "10-HTGL"),
                Band("PRMSL", "0-MSL"),
            ][number - 1]

    assert (
        validate_grib(
            tmp_path / "unused",
            split_request(planned)[0],
            dataset_opener=lambda _path: Dataset(),
        )["validated_hours"]
        == 1
    )

    with pytest.raises(Era5Error, match="missing required"):
        validate_grib(
            tmp_path / "unused",
            split_request(planned)[0],
            dataset_opener=lambda _path: type("D", (), {"RasterCount": 0})(),
        )


def test_gdal_validator_accepts_cds_single_level_aliases_for_multiple_hours(tmp_path):
    planned = plan("2025-01-01T00:00", "2025-01-01T01:00")
    timestamps = (1735689600, 1735693200)

    class Band:
        def __init__(self, element, level, timestamp):
            self.metadata = {
                "GRIB_ELEMENT": element,
                "GRIB_SHORT_NAME": level,
                "GRIB_VALID_TIME": f"{timestamp} sec UTC",
            }

        def GetMetadata(self):
            return self.metadata

    class Dataset:
        bands = [
            Band(element, level, timestamp)
            for timestamp in timestamps
            for element, level in (("10U", "0-SFC"), ("10V", "0-SFC"), ("MSL", "0-SFC"))
        ]
        RasterCount = len(bands)

        def GetRasterBand(self, number):
            return self.bands[number - 1]

    result = validate_grib(
        tmp_path / "unused",
        split_request(planned)[0],
        dataset_opener=lambda _path: Dataset(),
    )
    assert result["fields"] == sorted(
        (
            "10m_u_component_of_wind",
            "10m_v_component_of_wind",
            "mean_sea_level_pressure",
        )
    )
    assert result["validated_hours"] == 2


def test_gdal_validator_accepts_live_msl_surface_alias(tmp_path):
    planned = plan("2025-01-01T00:00", "2025-01-01T00:00")

    class Band:
        def __init__(self, element, level):
            self.metadata = {
                "GRIB_ELEMENT": element,
                "GRIB_SHORT_NAME": level,
                "GRIB_VALID_TIME": "1735689600 sec UTC",
            }

        def GetMetadata(self):
            return self.metadata

    class Dataset:
        bands = [Band("10U", "0-SFC"), Band("10V", "0-SFC"), Band("MSL", "0-SFC")]
        RasterCount = len(bands)

        def GetRasterBand(self, number):
            return self.bands[number - 1]

    result = validate_grib(
        tmp_path / "unused",
        split_request(planned)[0],
        dataset_opener=lambda _path: Dataset(),
    )
    assert result["validated_hours"] == 1


def test_shared_pressure_helper_recognizes_msl_surface_and_msl_aliases():
    from sailworks.core.gribfields import is_mean_sea_level_pressure

    assert is_mean_sea_level_pressure(
        {"GRIB_ELEMENT": "MSL", "GRIB_SHORT_NAME": "0-SFC"}
    )
    assert is_mean_sea_level_pressure(
        {"GRIB_ELEMENT": "MSL", "GRIB_SHORT_NAME": "0-MSL"}
    )
    assert not is_mean_sea_level_pressure(
        {"GRIB_ELEMENT": "MSL", "GRIB_SHORT_NAME": "10-HTGL"}
    )
    assert not is_mean_sea_level_pressure(
        {"GRIB_ELEMENT": "MSL", "GRIB_SHORT_NAME": "0-SFCX"}
    )


def test_gdal_validator_rejects_unsupported_band_with_element_and_level(tmp_path):
    planned = plan("2025-01-01T00:00", "2025-01-01T00:00")

    class Band:
        def GetMetadata(self):
            return {
                "GRIB_ELEMENT": "UGRD",
                "GRIB_SHORT_NAME": "100-HTGL",
                "GRIB_VALID_TIME": "1735689600 sec UTC",
            }

    class Dataset:
        RasterCount = 1

        def GetRasterBand(self, _number):
            return Band()

    with pytest.raises(Era5Error, match="UGRD/100-HTGL"):
        validate_grib(
            tmp_path / "unused",
            split_request(planned)[0],
            dataset_opener=lambda _path: Dataset(),
        )


def test_failed_multichunk_import_preserves_preexisting_content_addressed_artifact(
    tmp_path,
):
    from sailworks.core.era5 import split_request
    from sailworks.core.tranchestore import Artifact

    root = tmp_path
    (root / "artifacts").mkdir()
    existing_bytes = b"preexisting artifact"
    digest = hashlib.sha256(existing_bytes).hexdigest()
    (root / "artifacts" / digest).write_bytes(existing_bytes)

    class Store:
        def __init__(self):
            self.root = root

        def register_file(self, source):
            data = Path(source).read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            target = root / "artifacts" / sha
            target.write_bytes(data)
            return Artifact(f"artifacts/{sha}", sha, len(data))

    class Client:
        def retrieve(self, _dataset, _request, target):
            Path(target).write_bytes(existing_bytes)

    count = 0

    def validate(_path, _piece):
        nonlocal count
        count += 1
        if count == 2:
            raise Era5Error("invalid second chunk")
        return {"reader": "test"}

    service = Era5DownloadService(Store(), client_factory=Client, validator=validate)
    planned = plan("2025-01-01T23:00", "2025-01-02T00:00")
    with pytest.raises(Era5Error, match="second chunk"):
        service._run(planned, threading.Event(), False)
    assert (root / "artifacts" / digest).read_bytes() == existing_bytes
    assert not (root / "era5-history.json").exists()


def test_successful_era5_import_publishes_catalog_and_artifact(tmp_path):
    from sailworks.core.tranchestore import TrancheStore

    root = tmp_path
    store = TrancheStore(root)
    sentinel = b'{"sentinel": true}'
    store.active_path.write_bytes(sentinel)
    active_before = store.active_path.read_bytes()

    payload = b"fake era5 grib bytes"
    digest = hashlib.sha256(payload).hexdigest()
    size = len(payload)
    provenance = {"reader": "test", "validated_hours": 1}

    class Client:
        def retrieve(self, _dataset, _request, target):
            Path(target).write_bytes(payload)

    def validate(_path, _piece):
        return provenance

    service = Era5DownloadService(store, client_factory=Client, validator=validate)
    planned = plan("2025-01-01T00:00", "2025-01-01T00:00")
    service._run(planned, threading.Event(), False)

    progress = service.progress_for(planned)
    assert progress["done"] == progress["total"] == 1
    assert progress["stage"] == "Complete"

    catalog = root / "era5-history.json"
    assert catalog.exists()
    entries = json.loads(catalog.read_text())
    assert len(entries) == 1
    entry = entries[0]
    assert entry["dataset"] == "reanalysis-era5-single-levels"
    assert entry["source"] == "CDS ERA5 hourly single levels reanalysis"
    assert entry["start"] == planned.start.isoformat()
    assert entry["end"] == planned.end.isoformat()
    assert entry["area"] == list(planned.area)
    assert entry["variables"] == list(
        (
            "10m_u_component_of_wind",
            "10m_v_component_of_wind",
            "mean_sea_level_pressure",
        )
    )
    assert entry["sha256"] == digest
    assert entry["size"] == size
    assert entry["provenance"] == provenance
    assert entry["artifact"] == f"artifacts/{digest}"

    artifact_path = root / entry["artifact"]
    assert artifact_path.is_file()
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == digest
    assert artifact_path.stat().st_size == size

    class NoClient:
        def retrieve(self, *_args):
            raise AssertionError("covered ERA5 data must be reused")

    reuse = Era5DownloadService(store, client_factory=NoClient, validator=validate)
    reuse._run(planned, threading.Event(), False)
    assert reuse.progress_for(planned)["stage"] == "Already available"
    assert len(json.loads(catalog.read_text())) == 1

    active_after = store.active_path.read_bytes()
    assert active_after == active_before
    assert active_after == sentinel

    assert list(store.manifests.glob("*.json")) == []


def test_submitted_cds_request_is_persisted_and_reused(tmp_path):
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    payload = b"fake era5 grib bytes"

    class Remote:
        request_id = "rid-1"
        status = "successful"

        def download(self, target):
            Path(target).write_bytes(payload)

    class Inner:
        def __init__(self):
            self.submitted = []

        def submit(self, _dataset, request):
            self.submitted.append(request)
            return Remote()

        def get_remote(self, _request_id):
            return Remote()

    class Client:
        def __init__(self):
            self.client = Inner()

    clients = []

    def factory():
        client = Client()
        clients.append(client)
        return client

    service = Era5DownloadService(
        store, client_factory=factory, validator=lambda _p, _piece: {"reader": "test"}
    )
    planned = plan("2025-01-01T00:00", "2025-01-01T00:00")

    service._run(planned, threading.Event(), False)

    assert sum(len(client.client.submitted) for client in clients) == 1
    assert service._load_pending() == []
    assert (tmp_path / "era5-history.json").exists()

    # A repeated request is served from the catalog: no new CDS submission.
    before = len(clients)
    repeated = Era5DownloadService(
        store, client_factory=factory, validator=lambda _p, _piece: {"reader": "test"}
    )
    repeated._run(planned, threading.Event(), False)
    assert len(clients) == before


def test_pending_cds_request_resumes_after_restart(tmp_path):
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    payload = b"resumed era5 grib bytes"

    class Remote:
        request_id = "rid-resume"
        status = "successful"

        def download(self, target):
            Path(target).write_bytes(payload)

    class Inner:
        def submit(self, _dataset, _request):
            raise AssertionError("a pending request must not be submitted again")

        def get_remote(self, request_id):
            assert request_id == "rid-resume"
            return Remote()

    class Client:
        def __init__(self):
            self.client = Inner()

    service = Era5DownloadService(
        store, client_factory=Client, validator=lambda _p, _piece: {"reader": "test"}
    )
    piece = split_request(plan("2025-01-01T00:00", "2025-01-01T00:00"))[0]
    service._remember_pending(piece, "rid-resume")

    assert service.resume_pending() == 1
    for thread in list(service._resume_threads):
        thread.join(5)

    catalog = tmp_path / "era5-history.json"
    assert catalog.exists()
    assert len(json.loads(catalog.read_text())) == 1
    assert service._load_pending() == []


def test_pending_requests_prune_covered_and_duplicate_entries(tmp_path):
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    service = Era5DownloadService(
        store, client_factory=lambda: None, validator=lambda _p, _piece: {}
    )
    piece = split_request(plan("2025-01-01T00:00", "2025-01-01T00:00"))[0]
    other = split_request(plan("2025-01-02T00:00", "2025-01-02T00:00"))[0]

    artifact = tmp_path / "artifacts" / "existing"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"cached")
    (tmp_path / "era5-history.json").write_text(
        json.dumps(
            [
                {
                    "start": piece.start.isoformat(),
                    "end": piece.end.isoformat(),
                    "area": list(piece.area),
                    "artifact": "artifacts/existing",
                }
            ]
        )
    )
    service._save_pending(
        [
            {
                "start": piece.start.isoformat(),
                "end": piece.end.isoformat(),
                "area": list(piece.area),
                "request_id": "covered",
                "created": "2025-01-01T00:00:00+00:00",
            },
            {
                "start": other.start.isoformat(),
                "end": other.end.isoformat(),
                "area": [round(value, 4) for value in other.area],
                "request_id": "older",
                "created": "2025-01-01T00:00:05+00:00",
            },
            {
                "start": other.start.isoformat(),
                "end": other.end.isoformat(),
                "area": [round(value, 2) for value in other.area],
                "request_id": "newer",
                "created": "2025-01-01T00:00:10+00:00",
            },
        ]
    )

    assert [entry["request_id"] for entry in service.pending_requests()] == ["newer"]
    # The prune is persisted, so a later restart does not resurrect old rows.
    assert [entry["request_id"] for entry in service._load_pending()] == ["newer"]


def test_rounded_area_reuses_pending_request_instead_of_resubmitting(tmp_path):
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)

    class Remote:
        request_id = "rid-rounded"
        status = "successful"

        def download(self, target):
            Path(target).write_bytes(b"rounded area request")

    class Inner:
        def submit(self, _dataset, _request):
            raise AssertionError("a pending request must be reused")

        def get_remote(self, request_id):
            assert request_id == "rid-rounded"
            return Remote()

    class Client:
        def __init__(self):
            self.client = Inner()

    service = Era5DownloadService(
        store, client_factory=Client, validator=lambda _p, _piece: {"reader": "test"}
    )
    piece = split_request(plan("2025-01-01T00:00", "2025-01-01T00:00"))[0]
    service._save_pending(
        [
            {
                "start": piece.start.isoformat(),
                "end": piece.end.isoformat(),
                "area": [round(value, 4) for value in piece.area],
                "request_id": "rid-rounded",
                "created": "2025-01-01T00:00:00+00:00",
            }
        ]
    )

    service._run(plan("2025-01-01T00:00", "2025-01-01T00:00"), threading.Event(), False)

    assert service._load_pending() == []
    assert (tmp_path / "era5-history.json").exists()


def test_stale_accepted_request_is_deleted_and_resubmitted(tmp_path):
    from datetime import timedelta

    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    created = datetime.now(timezone.utc) - timedelta(hours=8)

    class Stale:
        request_id = "rid-stale"
        status = "accepted"
        created_at = created
        updated_at = created

        def download(self, target):
            raise AssertionError("a stuck request must not be downloaded")

    class Fresh:
        request_id = "rid-fresh"
        status = "successful"

        def download(self, target):
            Path(target).write_bytes(b"resubmitted")

    class Inner:
        def __init__(self):
            self.deleted = []
            self.submitted = 0

        def submit(self, _dataset, _request):
            self.submitted += 1
            return Fresh()

        def get_remote(self, request_id):
            return Stale() if request_id == "rid-stale" else Fresh()

        def delete(self, request_id):
            self.deleted.append(request_id)

    class Client:
        def __init__(self):
            self.client = Inner()

    client = Client()
    service = Era5DownloadService(
        store,
        client_factory=lambda: client,
        validator=lambda _p, _piece: {"reader": "test"},
    )
    piece = split_request(plan("2025-01-01T00:00", "2025-01-01T00:00"))[0]
    service._remember_pending(piece, "rid-stale")

    service._run(plan("2025-01-01T00:00", "2025-01-01T00:00"), threading.Event(), False)

    assert client.client.deleted == ["rid-stale"]
    assert client.client.submitted == 1
    assert service._load_pending() == []


def test_queue_limit_is_waited_out_instead_of_failing(tmp_path, monkeypatch):
    import sailworks.core.era5 as era5
    from sailworks.core.tranchestore import TrancheStore

    monkeypatch.setattr(era5, "QUEUE_RETRY_SECONDS", 0)
    store = TrancheStore(tmp_path)

    class Remote:
        request_id = "rid-queued"
        status = "successful"

        def download(self, target):
            Path(target).write_bytes(b"queued then accepted")

    class Inner:
        def __init__(self):
            self.submitted = 0

        def submit(self, _dataset, _request):
            self.submitted += 1
            if self.submitted == 1:
                raise RuntimeError(
                    "Maximum number queued requests for this dataset has been "
                    "reached. Please configure your scripts to request data "
                    "sequentially"
                )
            return Remote()

        def get_remote(self, _request_id):
            return Remote()

    class Client:
        def __init__(self):
            self.client = Inner()

    client = Client()
    service = Era5DownloadService(
        store,
        client_factory=lambda: client,
        validator=lambda _p, _piece: {"reader": "test"},
    )

    service._run(plan("2025-01-01T00:00", "2025-01-01T00:00"), threading.Event(), False)

    assert client.client.submitted == 2
    assert (tmp_path / "era5-history.json").exists()


def test_resume_resubmits_dead_request_with_full_payload(tmp_path):
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    submitted = []

    class Remote:
        request_id = "rid-new"
        status = "successful"

        def download(self, target):
            Path(target).write_bytes(b"resubmitted era5 grib bytes")

    class Inner:
        def submit(self, _dataset, request):
            submitted.append(request)
            return Remote()

        def get_remote(self, request_id):
            if request_id == "rid-dead":
                raise RuntimeError("404 not found")
            return Remote()

    class Client:
        def __init__(self):
            self.client = Inner()

    service = Era5DownloadService(
        store, client_factory=Client, validator=lambda _p, _piece: {"reader": "test"}
    )
    piece = split_request(plan("2025-01-01T00:00", "2025-01-01T00:00"))[0]
    service._remember_pending(piece, "rid-dead")

    assert service.resume_pending() == 1
    for thread in list(service._resume_threads):
        thread.join(5)

    assert len(submitted) == 1
    assert submitted[0]["year"] == ["2025"]
    assert submitted[0]["month"] == ["01"]
    assert submitted[0]["day"] == ["01"]
    assert submitted[0]["time"] == ["00:00"]
    assert submitted[0]["area"] == list(piece.area)
    assert submitted[0]["variable"]
    assert (tmp_path / "era5-history.json").exists()


def test_wind_only_plan_and_validation(tmp_path):
    from sailworks.core.era5 import FIELD_VARIABLES

    planned = plan_request(
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        (50, -10, 40, 5),
        fields=("wind",),
        now=datetime(2025, 2, 20, tzinfo=timezone.utc),
    )
    piece = split_request(planned)[0]
    assert piece.request["variable"] == list(FIELD_VARIABLES["wind"])

    class Band:
        def __init__(self, element, short):
            self.element, self.short = element, short

        def GetMetadata(self):
            return {
                "GRIB_ELEMENT": self.element,
                "GRIB_SHORT_NAME": self.short,
                "GRIB_VALID_TIME": "1735689600 sec UTC",
            }

    class Dataset:
        RasterCount = 2

        def GetRasterBand(self, number):
            return [Band("UGRD", "10-HTGL"), Band("VGRD", "10-HTGL")][number - 1]

    result = validate_grib(
        tmp_path / "unused", piece, dataset_opener=lambda _path: Dataset()
    )
    assert result["validated_hours"] == 1


def test_publish_history_display_keeps_requested_fields(tmp_path):
    from types import SimpleNamespace

    from sailworks.core.era5 import FIELD_VARIABLES, publish_history_display
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    source = tmp_path / "chunk.grib"
    source.write_bytes(b"fake era5 grib bytes")
    artifact = store.register_file(source)
    entries = [
        {
            "artifact": artifact.path,
            "start": "2025-01-01T00:00:00+00:00",
            "end": "2025-01-01T01:00:00+00:00",
            "area": [50, -10, 40, 5],
            "variables": list(FIELD_VARIABLES["wind"]),
        }
    ]

    tranche = publish_history_display(SimpleNamespace(store=store), entries)

    entry = tranche.entries[0]
    assert entry.fields == ("wind",)
    assert entry.levels == ("10-HTGL",)
    assert dict(entry.field_times)["wind"] == entry.time_steps


def test_canceled_era5_download_leaves_no_catalog_or_artifacts(tmp_path):
    root = tmp_path
    (root / "artifacts").mkdir()

    class Store:
        def __init__(self):
            self.root = root

        def register_file(self, source):
            raise AssertionError("register_file must not be called on cancel")

    started = threading.Event()
    cancel = threading.Event()

    class Client:
        def retrieve(self, _dataset, _request, target):
            started.set()
            cancel.wait(timeout=5)
            Path(target).write_bytes(b"fake era5 grib bytes")

    service = Era5DownloadService(
        Store(),
        client_factory=Client,
        validator=lambda _path, _piece: {"reader": "test"},
    )
    planned = plan("2025-01-01T00:00", "2025-01-01T00:00")

    errors = []

    def run():
        try:
            service._run(planned, cancel, False)
        except Exception as exc:  # noqa: BLE001 - test observes any failure
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    cancel.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], Era5Error)
    assert "cancel" in str(errors[0]).lower()
    assert not (root / "era5-history.json").exists()
    assert list((root / "artifacts").iterdir()) == []
