from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from gweatherrouting.core.forecasttranche import (
    AnalysisSelection,
    Artifact,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)

RUN = datetime(2025, 1, 1, tzinfo=timezone.utc)
ARTIFACT = Artifact("artifacts/" + "d" * 64, "d" * 64, 1)


def make_config(**changes):
    config = ModelConfiguration(
        "GFS", ("wind",), (0, 1, 2, 3), "native", 6, (0, 3, 6), 3
    )
    return replace(config, **changes)


def make_entry(**changes):
    entry = ModelEntry(
        "run",
        "GFS",
        RUN,
        "control",
        ("wind",),
        ("10m",),
        (-1, 0, 3, 4),
        "0.25deg",
        (RUN, RUN + timedelta(hours=3), RUN + timedelta(hours=6)),
        (ARTIFACT,),
        RUN + timedelta(hours=1),
    )
    return replace(entry, **changes)


def make_tranche(config=None, entry=None, **changes):
    return ForecastTranche(
        "batch",
        RUN + timedelta(hours=2),
        (config or make_config(),),
        (entry or make_entry(),),
        **changes,
    )


@pytest.mark.parametrize(
    "region",
    [
        (0, 180, 1, -180),
        (0, 10, 1, 10),
        (float("nan"), 0, 1, 2),
        (0, 0, float("inf"), 2),
        (True, 0, 1, 2),
        (-91, 0, 1, 2),
        (0, -181, 1, 2),
        (0, 0, 91, 2),
        (0, 0, 1, 181),
    ],
)
def test_region_rejects_invalid_or_reverse_global_longitudes(region):
    with pytest.raises(ValueError):
        make_config(region=region)
    payload = make_config().to_json()
    payload["region"] = list(region)
    with pytest.raises(ValueError):
        ModelConfiguration.from_json(payload)


@pytest.mark.parametrize("region", [(0, -180, 1, 180), (0, 170, 1, -170)])
def test_ordered_global_and_dateline_regions_are_accepted(region):
    config = make_config(region=region, forecast_hours=0, steps=(), interval=0)
    entry_region = region if region[1] == -180 else (0, 160, 1, -160)
    entry = make_entry(region=entry_region, time_steps=(RUN,))
    make_tranche(config, entry)


def test_dateline_region_does_not_contain_non_crossing_longitudes():
    config = make_config(
        region=(0, 170, 1, -170), forecast_hours=0, steps=(), interval=0
    )
    entry = make_entry(region=(0, 160, 1, 170), time_steps=(RUN,))
    with pytest.raises(ValueError):
        make_tranche(config, entry)


@pytest.mark.parametrize("version", [True, 2, 1.0])
def test_version_requires_exact_supported_integer(version):
    with pytest.raises(ValueError):
        make_tranche(version=version)
    payload = make_tranche().to_json()
    payload["version"] = version
    with pytest.raises(ValueError):
        ForecastTranche.from_json(payload)


@pytest.mark.parametrize("interval", [True, 2, 3.0, -1])
def test_interval_rejects_non_contract_values(interval):
    with pytest.raises(ValueError):
        make_config(interval=interval)


@pytest.mark.parametrize("steps", [[0, 3, 9], [0, 3, 3], [3, 0], [0, True]])
def test_config_rejects_invalid_steps(steps):
    with pytest.raises(ValueError):
        make_config(steps=steps)


def test_native_request_accepts_concrete_resolution_and_json_is_defensive():
    tranche = make_tranche()
    payload = tranche.to_json()
    payload["configurations"][0]["fields"].append("pressure")
    payload["entries"][0]["artifacts"].clear()
    assert tranche.configurations[0].fields == ("wind",)
    assert len(tranche.entries[0].artifacts) == 1
    assert tranche.entries[0].acquired_at == RUN + timedelta(hours=1)


@pytest.mark.parametrize(
    "changes",
    [
        {"resolution": "native"},
        {"region": (1, 1, 2, 3)},
        {"time_steps": (RUN, RUN + timedelta(hours=3))},
        {"time_steps": (RUN, RUN + timedelta(hours=3), RUN + timedelta(hours=9))},
        {"acquired_at": RUN + timedelta(hours=3)},
        {"model_id": "HRRR"},
    ],
)
def test_tranche_rejects_metadata_that_does_not_satisfy_request(changes):
    with pytest.raises(ValueError):
        make_tranche(entry=make_entry(**changes))


def test_fixed_resolution_mismatch_is_not_hidden_by_other_metadata():
    config = make_config(resolution="0.25deg")
    assert make_tranche(config).entries[0].resolution == "0.25deg"
    with pytest.raises(ValueError, match="resolution"):
        make_tranche(config, make_entry(resolution="1deg"))


@pytest.mark.parametrize(
    "changes",
    [
        {"forecast_hours": True},
        {"forecast_hours": 6.0},
        {"forecast_hours": -1},
        {"forecast_hours": 385},
        {"fields": {"wind": []}},
        {"fields": ("wind", "wind")},
        {"fields": (["wind"],)},
        {"region": {"south": 0}},
        {"steps": "036"},
        {"resolution": {}},
    ],
)
def test_configuration_rejects_invalid_scalars_and_mutable_elements(changes):
    with pytest.raises(ValueError):
        make_config(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"fields": ({"wind": []},)},
        {"levels": ()},
        {"levels": ("10m", "10m")},
        {"levels": ({"level": []},)},
        {"artifacts": ({"path": ARTIFACT.path},)},
        {"artifacts": (ARTIFACT, replace(ARTIFACT, size=2))},
        {"run_time": RUN.replace(tzinfo=None)},
        {"acquired_at": RUN - timedelta(seconds=1)},
        {"time_steps": (RUN, RUN)},
        {"time_steps": (RUN + timedelta(hours=3), RUN)},
    ],
)
def test_entry_rejects_invalid_nested_values_and_time_coverage(changes):
    with pytest.raises(ValueError):
        make_entry(**changes)


def test_tranche_requires_complete_typed_model_membership_and_requested_fields():
    tranche = make_tranche()
    for changes in (
        {"configurations": ()},
        {"configurations": ({"model_id": "GFS"},)},
        {"configurations": tranche.configurations * 2},
        {"entries": ({"model_id": "GFS"},)},
        {"entries": tranche.entries * 2},
    ):
        with pytest.raises(ValueError):
            replace(tranche, **changes)
    with pytest.raises(ValueError, match="fields"):
        make_tranche(make_config(fields=("wind", "pressure")))


def test_acquisition_json_is_required_and_timestamps_normalize_to_utc():
    entry = make_entry()
    payload = entry.to_json()
    del payload["acquired_at"]
    with pytest.raises(KeyError):
        ModelEntry.from_json(payload)
    offset = timezone(timedelta(hours=5))
    normalized = replace(
        entry,
        run_time=entry.run_time.astimezone(offset),
        acquired_at=entry.acquired_at.astimezone(offset),
        time_steps=[t.astimezone(offset) for t in entry.time_steps],
    )
    assert normalized == entry
    assert normalized.run_time.tzinfo is timezone.utc


def test_nested_input_lists_and_serialized_payloads_cannot_mutate_tranche():
    config_fields, config_region, config_steps = ["wind"], [0, 1, 2, 3], [0, 3, 6]
    config = make_config(fields=config_fields, region=config_region, steps=config_steps)
    entry_fields, levels, artifacts = ["wind"], ["10m"], [ARTIFACT]
    times = list(make_entry().time_steps)
    entry = make_entry(
        fields=entry_fields, levels=levels, artifacts=artifacts, time_steps=times
    )
    configs, entries = [config], [entry]
    tranche = ForecastTranche("batch", RUN + timedelta(hours=2), configs, entries)
    expected = tranche.to_json()
    for value in (
        config_fields,
        config_region,
        config_steps,
        entry_fields,
        levels,
        artifacts,
        times,
        configs,
        entries,
    ):
        value.clear()
    assert tranche.to_json() == expected
    with pytest.raises(FrozenInstanceError):
        tranche.tranche_id = "other"
    selection = AnalysisSelection("batch", "run")
    assert AnalysisSelection.from_json(selection.to_json()) == selection
    with pytest.raises(FrozenInstanceError):
        selection.entry_id = "other"


@pytest.mark.parametrize("size", [True, -1, 1.0])
def test_artifact_requires_exact_nonnegative_size(size):
    with pytest.raises(ValueError):
        replace(ARTIFACT, size=size)
