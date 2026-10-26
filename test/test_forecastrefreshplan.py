import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from gweatherrouting.core.forecastrefreshplan import (
    MODEL_CAPABILITIES,
    ForecastRefreshPlan,
    PlannedModel,
    RefreshCancelledError,
    RefreshPlanningError,
    UnsupportedConfigurationError,
    create_source,
    plan_refresh,
)
from gweatherrouting.core.forecasttranche import ModelConfiguration


def config(
    model="GFS",
    *,
    fields=("wind",),
    hours=6,
    interval=3,
    steps=(),
    resolution="native",
    region=(0, -10, 10, 10),
):
    return ModelConfiguration(model, fields, region, resolution, hours, steps, interval)


def row(provider, date, cycle, hours, steps, fields=("wind",)):
    identifier = json.dumps(
        {
            "source": provider,
            "date": date,
            "cycle": cycle,
            "hours": max(steps),
            "steps": list(steps),
            "fields": list(fields),
        }
    )
    return ["label", provider, "size", "run", identifier]


class FakeSource:
    def __init__(self, name, rows):
        self.name = name
        self.rows = rows
        self.bounds = None
        self.calls = 0

    def get_download_list(self, options):
        self.calls += 1
        self.options = options
        return self.rows


def factory_map(sources):
    calls = []

    def make(model):
        def factory():
            calls.append(model)
            template = sources[model]
            return type(template)(template.name, template.rows)

        return factory

    return {model: make(model) for model in sources}, calls


def test_capabilities_are_read_only_and_create_source_sets_bounds():
    with pytest.raises(TypeError):
        MODEL_CAPABILITIES["NEW"] = object()
    source = FakeSource("NOAA GFS", [])
    factories, _ = factory_map({"GFS": source})
    created = create_source(config(region=(1, -5, 8, 9)), factories)
    assert created.bounds == (1.0, -5.0, 8.0, 9.0)


def test_validates_all_models_before_any_catalogue_call():
    good = FakeSource(
        "NOAA GFS", [row("NOAA GFS", "20250101", "00", 6, range(0, 7, 3))]
    )
    factories, calls = factory_map({"GFS": good})
    invalid = config("HRRR", resolution="12km")
    with pytest.raises(UnsupportedConfigurationError):
        plan_refresh((config(), invalid), "GFS", factories=factories)
    assert calls == [] and good.calls == 0


def test_plan_pins_provider_run_and_default_steps():
    source = FakeSource(
        "NOAA GFS", [row("NOAA GFS", "20250101", "00", 6, range(0, 7, 3))]
    )
    factories, _ = factory_map({"GFS": source})
    request = config(hours=5, interval=3)
    plan = plan_refresh([request], "GFS", factories=factories)
    planned = plan.models[0]
    assert planned.run_time == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert planned.configuration is request
    assert planned.field_steps == (("wind", (0, 3)),)
    assert json.loads(planned.identifier)["steps"] == [0, 3]
    assert plan.configurations == (request,)


def test_zero_hour_and_explicit_steps_are_preserved_or_rejected():
    source = FakeSource("NOAA GFS", [row("NOAA GFS", "20250101", "00", 0, [0])])
    factories, _ = factory_map({"GFS": source})
    zero = plan_refresh((config(hours=0, interval=0),), "GFS", factories=factories)
    assert zero.models[0].field_steps == (("wind", (0,)),)
    source.rows = [row("NOAA GFS", "20250101", "00", 0, [0])]
    with pytest.raises(RefreshPlanningError):
        plan_refresh(
            (config(hours=3, interval=3, steps=(0, 3)),), "GFS", factories=factories
        )


def test_hrrr_skips_newer_short_run_for_older_extended_run():
    source = FakeSource(
        "NOAA HRRR",
        [
            row("NOAA HRRR", "20250102", "07", 18, range(19)),
            row("NOAA HRRR", "20250102", "06", 48, range(49)),
        ],
    )
    factories, _ = factory_map({"HRRR": source})
    result = plan_refresh(
        (config("HRRR", hours=24, interval=1),), "HRRR", factories=factories
    )
    planned = result.models[0]
    assert planned.run_time.hour == 6
    assert max(planned.field_steps[0][1]) == 24


def test_hrrr_short_only_catalogue_cannot_satisfy_full_duration():
    source = FakeSource(
        "NOAA HRRR", [row("NOAA HRRR", "20250102", "07", 18, range(19))]
    )
    factories, _ = factory_map({"HRRR": source})
    with pytest.raises(RefreshPlanningError, match="complete requested forecast"):
        plan_refresh(
            (config("HRRR", hours=24, interval=1, steps=(0, 3)),),
            "HRRR",
            factories=factories,
        )


def test_hrdps_pressure_only_has_three_hour_schedule():
    source = FakeSource(
        "CMC HRDPS", [row("CMC HRDPS", "20250101", "00", 6, [0, 3, 6], ("pressure",))]
    )
    factories, _ = factory_map({"HRDPS": source})
    result = plan_refresh(
        (config("HRDPS", fields=("pressure",), hours=6, interval=1),),
        "HRDPS",
        factories=factories,
    )
    assert result.models[0].field_steps == (("pressure", (0, 3, 6)),)


def test_hrdps_explicit_union_keeps_pressure_on_native_three_hour_steps():
    source = FakeSource(
        "CMC HRDPS",
        [row("CMC HRDPS", "20250101", "00", 3, (0, 1, 3), ("wind", "pressure"))],
    )
    factories, _ = factory_map({"HRDPS": source})
    result = plan_refresh(
        (
            config(
                "HRDPS",
                fields=("wind", "pressure"),
                hours=3,
                interval=1,
                steps=(0, 1, 3),
            ),
        ),
        "HRDPS",
        factories=factories,
    )
    assert result.models[0].field_steps == (
        ("wind", (0, 1, 3)),
        ("pressure", (0, 3)),
    )


@pytest.mark.parametrize("model,provider", [("GFS", "NOAA GFS"), ("HRRR", "NOAA HRRR")])
def test_gfs_and_hrrr_pressure_follow_hourly_provider_schedule(model, provider):
    source = FakeSource(
        provider,
        [row(provider, "20250101", "00", 3, [0, 1, 2, 3], ("wind", "pressure"))],
    )
    factories, _ = factory_map({model: source})
    result = plan_refresh(
        (config(model, fields=("wind", "pressure"), hours=3, interval=1),),
        model,
        factories=factories,
    )
    assert result.models[0].field_steps == (
        ("wind", (0, 1, 2, 3)),
        ("pressure", (0, 1, 2, 3)),
    )


def test_unsupported_capabilities_and_factories_rejected_before_catalogues():
    source = FakeSource(
        "NOAA GFS", [row("NOAA GFS", "20250101", "00", 6, range(0, 7, 3))]
    )
    factories, calls = factory_map({"GFS": source})
    with pytest.raises(UnsupportedConfigurationError):
        plan_refresh(
            (config("GFS"), config("HRDPS", hours=49)), "GFS", factories=factories
        )
    with pytest.raises(UnsupportedConfigurationError):
        plan_refresh(
            (config("HRDPS", fields=("pressure",), interval=1, steps=(1,)),),
            "HRDPS",
            factories={"HRDPS": lambda: pytest.fail("factory must not be called")},
        )
    with pytest.raises(UnsupportedConfigurationError):
        plan_refresh(
            (config("GFS", hours=121, interval=1, steps=(121,)),),
            "GFS",
            factories=factories,
        )
    with pytest.raises(UnsupportedConfigurationError):
        plan_refresh((config("GFS"), config("HRRR")), "GFS", factories=factories)
    assert calls == [] and source.calls == 0


def test_native_resolution_and_wrapped_region_rules():
    source = FakeSource("NOAA GFS", [row("NOAA GFS", "20250101", "00", 0, [0])])
    factories, _ = factory_map({"GFS": source})
    planned = plan_refresh(
        (config(hours=0, interval=0),), "GFS", factories=factories
    ).models[0]
    assert planned.native_resolution == "0.25deg"
    with pytest.raises(UnsupportedConfigurationError):
        plan_refresh((config(region=(0, 170, 10, -170)),), "GFS", factories=factories)


def test_catalogue_errors_and_malformed_or_mismatched_rows_fail_whole_plan():
    good = FakeSource(
        "NOAA GFS", [row("NOAA GFS", "20250101", "00", 6, range(0, 7, 3))]
    )
    bad = FakeSource("NOAA HRRR", [["bad"]])
    factories, _ = factory_map({"GFS": good, "HRRR": bad})
    with pytest.raises(RefreshPlanningError, match="HRRR"):
        plan_refresh((config(), config("HRRR")), "GFS", factories=factories)
    bad.rows = [row("NOAA GFS", "20250101", "00", 6, range(0, 7, 3))]
    with pytest.raises(RefreshPlanningError, match="provider mismatch"):
        plan_refresh((config("HRRR"),), "HRRR", factories=factories)


def test_no_partial_plan_when_catalogue_raises_and_unknown_models_rejected():
    good = FakeSource(
        "NOAA GFS", [row("NOAA GFS", "20250101", "00", 6, range(0, 7, 3))]
    )

    class OfflineSource(FakeSource):
        def get_download_list(self, options):
            raise OSError("offline")

    failing = OfflineSource("NOAA HRRR", [])
    factories, _ = factory_map({"GFS": good, "HRRR": failing})
    with pytest.raises(RefreshPlanningError, match="HRRR: offline"):
        plan_refresh((config(), config("HRRR")), "GFS", factories=factories)
    with pytest.raises(UnsupportedConfigurationError):
        plan_refresh((config("Imported"),), "Imported", factories=factories)


def test_cancellation_and_immutable_pinned_identifiers():
    class Cancel:
        def is_set(self):
            return True

    source = FakeSource(
        "NOAA GFS", [row("NOAA GFS", "20250101", "00", 6, range(0, 7, 3))]
    )
    factories, _ = factory_map({"GFS": source})
    with pytest.raises(RefreshCancelledError):
        plan_refresh((config(),), "GFS", factories=factories, cancel=Cancel())
    result = plan_refresh((config(),), "GFS", factories=factories)
    with pytest.raises((AttributeError, TypeError)):
        result.models[0].field_steps[0][1][0] = 99
    with pytest.raises((AttributeError, TypeError)):
        result.models = ()
    with pytest.raises(ValueError):
        ForecastRefreshPlan(result.models, "HRRR")


def test_plan_snapshots_catalogue_payload_and_factories_are_fresh():
    rows = [row("NOAA GFS", "20250101", "00", 6, range(0, 7, 3))]
    source = FakeSource("NOAA GFS", rows)
    factories, calls = factory_map({"GFS": source})
    first = plan_refresh((config(),), "GFS", factories=factories)
    pinned = first.models[0].identifier
    second = plan_refresh((config(),), "GFS", factories=factories)
    assert first.models[0].identifier == pinned == second.models[0].identifier
    assert calls == ["GFS", "GFS"]
    rows[0][4] = "{}"
    assert first.models[0].identifier == pinned


def test_planned_model_public_boundary_checks_pinned_identity_and_schedules():
    source = FakeSource(
        "NOAA GFS", [row("NOAA GFS", "20250101", "00", 6, range(0, 7, 3))]
    )
    factories, _ = factory_map({"GFS": source})
    planned = plan_refresh((config(),), "GFS", factories=factories).models[0]
    with pytest.raises(ValueError, match="exact UTC cycle hour"):
        replace(planned, run_time=planned.run_time.replace(minute=30))
    payload = json.loads(planned.identifier)
    payload["date"] = "20250102"
    with pytest.raises(ValueError):
        replace(planned, identifier=json.dumps(payload))
    with pytest.raises(ValueError):
        replace(planned, field_steps=(("wind", (0, 2, 6)),))
    with pytest.raises(ValueError):
        replace(planned, native_resolution="3km")
    with pytest.raises(ValueError):
        replace(planned, member="control")

    field_hours = [0, 3, 6]
    fields = [["wind", field_hours]]
    copied = PlannedModel(
        planned.configuration,
        planned.run_time,
        planned.provider_name,
        planned.identifier,
        planned.native_resolution,
        fields,
    )
    field_hours[1] = 2
    fields.clear()
    assert copied.field_steps == (("wind", (0, 3, 6)),)
    with pytest.raises(ValueError):
        replace(planned, field_steps={"wind": (0, 3, 6)})
