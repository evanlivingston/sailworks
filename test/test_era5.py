from datetime import datetime, timezone

import pytest

from gweatherrouting.core.era5 import (
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
    from gweatherrouting.core.gribfields import is_mean_sea_level_pressure

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
    from gweatherrouting.core.era5 import split_request
    from gweatherrouting.core.tranchestore import Artifact

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
    from gweatherrouting.core.tranchestore import TrancheStore

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

    active_after = store.active_path.read_bytes()
    assert active_after == active_before
    assert active_after == sentinel

    assert list(store.manifests.glob("*.json")) == []


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
