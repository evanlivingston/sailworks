from datetime import datetime, timezone
import json

import pytest

from gweatherrouting.core.forecastcapabilities import MODEL_CAPABILITIES
from gweatherrouting.core.forecastrefreshcontracts import (
    ForecastRefreshPlan,
    PlannedModel,
)
from gweatherrouting.core.forecastrefreshplan import (
    UnsupportedConfigurationError,
    plan_refresh,
)
from gweatherrouting.core.forecaststate import (
    ForecastAnalysisState,
    ForecastConfiguration,
)
from gweatherrouting.core.forecasttranche import (
    AnalysisSelection,
    Artifact,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)
from gweatherrouting.core.tranchestore import TrancheStore

RUN = datetime(2025, 1, 1, tzinfo=timezone.utc)
ACQUIRED = datetime(2025, 1, 2, tzinfo=timezone.utc)
REGION = (-1.0, -1.0, 1.0, 1.0)


def config(model, region=REGION):
    return ModelConfiguration(model, ("wind",), region, "native", 0, (0,), 0)


def planned(configuration):
    capability = MODEL_CAPABILITIES[configuration.model_id]
    identifier = json.dumps(
        {
            "source": capability.provider_name,
            "date": "20250101",
            "cycle": "00",
            "hours": 0,
            "steps": [0],
            "fields": ["wind"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return PlannedModel(
        configuration,
        RUN,
        capability.provider_name,
        identifier,
        capability.native_resolution,
        (("wind", (0,)),),
    )


def install_common_tranche(store, configurations):
    entries = []
    for index, configuration in enumerate(configurations):
        source = store.root / f"{configuration.model_id}.grib"
        source.write_bytes(configuration.model_id.encode())
        artifact = store.register_file(source)
        entries.append(
            ModelEntry(
                configuration.model_id,
                configuration.model_id,
                RUN,
                "deterministic",
                ("wind",),
                ("10-HTGL",),
                ((-2.0, -2.0, 2.0, 2.0) if index == 0 else (-5.0, -5.0, 5.0, 5.0)),
                MODEL_CAPABILITIES[configuration.model_id].native_resolution,
                (RUN,),
                (artifact,),
                ACQUIRED,
                (("wind", (RUN,)),),
            )
        )
    tranche = ForecastTranche(
        "shared-region", ACQUIRED, tuple(configurations), tuple(entries)
    )
    store.publish(tranche)
    store.activate(AnalysisSelection(tranche.tranche_id, entries[0].entry_id))
    return tranche


@pytest.mark.parametrize("contract", ["configuration", "plan", "tranche"])
def test_mixed_requested_regions_rejected_by_each_contract(contract):
    first, second = config("GFS"), config("HRRR", (-2, -2, 2, 2))
    with pytest.raises(ValueError, match="share the same requested region"):
        if contract == "configuration":
            ForecastConfiguration((first, second), "GFS")
        elif contract == "plan":
            ForecastRefreshPlan((planned(first), planned(second)), "GFS")
        else:
            artifact = Artifact("artifacts/" + "a" * 64, "a" * 64, 1)
            entries = (
                ModelEntry(
                    "GFS",
                    "GFS",
                    RUN,
                    "deterministic",
                    ("wind",),
                    ("10-HTGL",),
                    (-2, -2, 2, 2),
                    "0.25deg",
                    (RUN,),
                    (artifact,),
                    ACQUIRED,
                    (("wind", (RUN,)),),
                ),
                ModelEntry(
                    "HRRR",
                    "HRRR",
                    RUN,
                    "deterministic",
                    ("wind",),
                    ("10-HTGL",),
                    (-2, -2, 2, 2),
                    "3km",
                    (RUN,),
                    (artifact,),
                    ACQUIRED,
                    (("wind", (RUN,)),),
                ),
            )
            ForecastTranche("mixed", ACQUIRED, (first, second), entries)


def test_planner_rejects_region_mismatch_before_provider_factories():
    calls = []

    def factory():
        calls.append("factory")
        raise AssertionError("provider must not be constructed")

    with pytest.raises(
        UnsupportedConfigurationError, match="share the same requested region"
    ):
        plan_refresh(
            (config("GFS"), config("HRRR", (-2, -2, 2, 2))),
            "GFS",
            factories={"GFS": factory, "HRRR": factory},
        )
    assert calls == []


def test_shared_region_roundtrip_actual_coverage_can_differ(tmp_path):
    configurations = (config("GFS"), config("HRRR"))
    intent = ForecastConfiguration(configurations, "GFS")
    assert intent.region == REGION
    assert ForecastConfiguration.from_json(intent.to_json()).region == REGION
    plan = ForecastRefreshPlan(tuple(planned(item) for item in configurations), "HRRR")
    assert plan.region == REGION

    store = TrancheStore(tmp_path)
    tranche = install_common_tranche(store, configurations)
    assert tranche.region == REGION
    assert tranche.entries[0].region != tranche.entries[1].region
    assert ForecastTranche.from_json(tranche.to_json()).region == REGION


def test_region_edit_changes_intent_not_active_immutable_tranche(tmp_path):
    store = TrancheStore(tmp_path)
    original_configurations = (config("GFS"), config("HRRR"))
    tranche = install_common_tranche(store, original_configurations)
    state = ForecastAnalysisState(tmp_path)
    state.configure(original_configurations, "GFS")
    changed = (-4.0, -3.0, 4.0, 3.0)
    state.configure((config("GFS", changed), config("HRRR", changed)), "HRRR")
    assert state.configuration.region == changed
    assert store.active().tranche.tranche_id == tranche.tranche_id
    assert store.active().tranche.region == REGION
