"""Pressure-only validation of externally supplied public GRIB probes."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from gweatherrouting.core.forecastboundrouting import (
    BoundForecastError,
    BoundWorkerGrib,
)
from gweatherrouting.core.forecastcapabilities import MODEL_CAPABILITIES
from gweatherrouting.core.forecasttranche import (
    AnalysisSelection,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)
from gweatherrouting.core.forecastvalidation import inspect_download
from gweatherrouting.core.grib import Grib
from gweatherrouting.core.tranchestore import TrancheStore
from gweatherrouting.rendering.displayforecast import inspect_forecasts

MODELS = {
    "IFS": ("ifs-surface-0h.grib2", "ifs-metadata.json"),
    "AIFS": ("aifs-surface-0h.grib2", "aifs-metadata.json"),
    "AIGFS": ("aigfs-surface-0h.grib2", "aigfs-metadata.json"),
}
RUN = datetime(2026, 10, 24, tzinfo=timezone.utc)


def extract_pressure_messages(source, metadata, destination):
    raw = source.read_bytes()
    offset = 0
    kept = []
    band_metadata = metadata["bands"]
    message_count = 0
    while offset < len(raw):
        if raw[offset : offset + 4] != b"GRIB" or raw[offset + 7] != 2:
            raise AssertionError("probe is not a sequence of GRIB2 messages")
        length = int.from_bytes(raw[offset + 8 : offset + 16], "big")
        if length < 20 or offset + length > len(raw):
            raise AssertionError("probe contains a malformed GRIB2 message")
        message = raw[offset : offset + length]
        if message[-4:] != b"7777":
            raise AssertionError("probe GRIB2 message has no terminator")
        if message_count >= len(band_metadata):
            raise AssertionError("probe has more messages than decoded bands")
        band = band_metadata[message_count]
        if (
            band.get("GRIB_ELEMENT") in ("PRES", "PRMSL", "MSLMA")
            and band.get("GRIB_SHORT_NAME") == "0-MSL"
        ):
            kept.append(message)
        message_count += 1
        offset += length
    if offset != len(raw) or message_count != len(band_metadata) or not kept:
        raise AssertionError("probe message and metadata counts disagree")
    destination.write_bytes(b"".join(kept))


@pytest.mark.parametrize("model", tuple(MODELS))
def test_pressure_only_probe_validates_samples_and_stays_unroutable(model, tmp_path):
    probe_root = os.environ.get("GWR_TEST_PUBLIC_GRIB_ROOT")
    if not probe_root:
        pytest.skip("set GWR_TEST_PUBLIC_GRIB_ROOT to the supplied public GRIB probes")
    grib_name, metadata_name = MODELS[model]
    probe = Path(probe_root) / grib_name
    metadata = json.loads((Path(probe_root) / metadata_name).read_text())
    run = datetime.fromtimestamp(
        float(metadata["bands"][0]["GRIB_REF_TIME"]), timezone.utc
    )
    pressure_path = tmp_path / f"{model}-pressure-only.grib2"
    extract_pressure_messages(probe, metadata, pressure_path)
    configuration = ModelConfiguration(
        model, ("pressure",), (-1, -1, 1, 1), "native", 0, (0,), 0
    )
    capability = MODEL_CAPABILITIES[model]
    identifier = json.dumps(
        {
            "source": capability.provider_name,
            "date": run.strftime("%Y%m%d"),
            "cycle": run.strftime("%H"),
            "hours": 0,
            "steps": [0],
            "fields": ["pressure"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    from gweatherrouting.core.forecastrefreshcontracts import PlannedModel

    planned = PlannedModel(
        configuration,
        run,
        capability.provider_name,
        identifier,
        capability.native_resolution,
        (("pressure", (0,)),),
    )
    validated = inspect_download(pressure_path, planned)
    assert validated.model_id == model
    assert validated.fields == ("pressure",)

    display, errors = inspect_forecasts(
        (
            (
                str(pressure_path),
                (
                    hashlib.sha256(pressure_path.read_bytes()).hexdigest(),
                    pressure_path.stat().st_size,
                ),
            ),
        )
    )
    assert not errors
    assert len(display) == 1 and display[0].model_name == model

    grib = Grib.parse(str(pressure_path))
    try:
        pressure = grib.get_pressure(run, ((-0.25, -0.25), (0.25, 0.25)))
        assert pressure
        dataset = grib.dataset
        band = next(
            dataset.GetRasterBand(index)
            for index in range(1, dataset.RasterCount + 1)
            if dataset.GetRasterBand(index).GetMetadata().get("GRIB_ELEMENT")
            in ("PRES", "PRMSL", "MSLMA")
        )
        expected_hpa = float(band.ReadAsArray()[360, 720]) / 100.0
        closest = min(pressure, key=lambda sample: abs(sample[0]) + abs(sample[1]))
        assert closest[2] == pytest.approx(expected_hpa)
    finally:
        grib.close()

    store = TrancheStore(tmp_path / "state")
    artifact = store.register_file(pressure_path)
    acquired = run + timedelta(hours=1)
    entry = ModelEntry(
        model,
        model,
        run,
        "deterministic",
        validated.fields,
        validated.levels,
        validated.region,
        validated.resolution,
        validated.time_steps,
        (artifact,),
        acquired,
        validated.field_times,
    )
    tranche = ForecastTranche("pressure-only", acquired, (configuration,), (entry,))
    store.publish(tranche)
    binding = store.resolve(AnalysisSelection(tranche.tranche_id, entry.entry_id))
    with pytest.raises(BoundForecastError, match="wind coverage"):
        BoundWorkerGrib(binding)
