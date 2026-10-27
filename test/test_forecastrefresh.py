from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
import json

import pytest
from dataclasses import replace

from gweatherrouting.core.forecastcapabilities import MODEL_CAPABILITIES
from gweatherrouting.core.forecastrefresh import (
    RefreshCoordinator,
    RefreshCancelledError,
    RefreshExecutionError,
    RefreshSupersededError,
)
from gweatherrouting.core.forecastrefreshcontracts import (
    ForecastRefreshPlan,
    PlannedModel,
)
from gweatherrouting.core.forecasttranche import AnalysisSelection, ModelConfiguration
from gweatherrouting.core.forecastvalidation import ValidatedModel, inspect_download
from gweatherrouting.core.tranchestore import TrancheStore

RUN = datetime(2025, 1, 1, tzinfo=timezone.utc)
ACQUIRED = datetime(2025, 1, 2, tzinfo=timezone.utc)


def config(model="GFS", fields=("wind",), hours=3, interval=3, steps=()):
    return ModelConfiguration(
        model, fields, (0, -1, 1, 1), "native", hours, steps, interval
    )


def make_plan(configurations, selected):
    models = []
    for cfg in configurations:
        capability = MODEL_CAPABILITIES[cfg.model_id]
        field_steps = tuple(
            (
                field,
                tuple(
                    hour
                    for hour in capability.available_steps(0, field)
                    if hour <= cfg.forecast_hours
                    and (not cfg.interval or hour % cfg.interval == 0)
                    and (not cfg.steps or hour in cfg.steps)
                ),
            )
            for field in cfg.fields
        )
        union = sorted({hour for _, times in field_steps for hour in times})
        identifier = json.dumps(
            {
                "source": capability.provider_name,
                "date": "20250101",
                "cycle": "00",
                "hours": max(union),
                "steps": union,
                "fields": list(cfg.fields),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        models.append(
            PlannedModel(
                cfg,
                RUN,
                capability.provider_name,
                identifier,
                capability.native_resolution,
                field_steps,
            )
        )
    return ForecastRefreshPlan(tuple(models), selected)


class FakeSource:
    def __init__(self, model, behavior=None):
        self.name = MODEL_CAPABILITIES[model].provider_name
        self.behavior = behavior or {}
        self.bounds = None
        self.identifiers = []

    def download(self, identifier, directory, progress, cancel=None):
        self.identifiers.append(identifier)
        if self.behavior.get("entered"):
            self.behavior["entered"].set()
            if not self.behavior["release"].wait(5):
                raise TimeoutError("test download was not released")
        if self.behavior.get("download_error"):
            raise OSError("synthetic download error")
        path = f"{directory}/{self.name.replace(' ', '_')}.grib"
        with open(path, "wb") as stream:
            stream.write(self.behavior.get("bytes", self.name.encode()))
        return path


def make_factories(behaviors=None, created=None):
    behaviors = behaviors or {}
    created = [] if created is None else created
    result = {}
    for model in MODEL_CAPABILITIES:

        def factory(model=model):
            source = FakeSource(model, behaviors.get(model))
            created.append(source)
            return source

        result[model] = factory
    return result, created


def valid_metadata(planned):
    field_times = tuple(
        (
            field,
            tuple(planned.run_time + timedelta(hours=h) for h in hours),
        )
        for field, hours in planned.field_steps
    )
    times = tuple(sorted({time for _, field in field_times for time in field}))
    levels = tuple(
        sorted(
            "10-HTGL" if field == "wind" else "0-MSL"
            for field in planned.configuration.fields
        )
    )
    return ValidatedModel(
        planned.model_id,
        planned.run_time,
        planned.member,
        planned.configuration.fields,
        levels,
        planned.configuration.region,
        MODEL_CAPABILITIES[planned.model_id].native_resolution,
        times,
        field_times,
    )


def coordinator(store, configs, *, behaviors=None, validator=None, progress=None):
    factories, created = make_factories(behaviors)
    return (
        RefreshCoordinator(
            store,
            factories=factories,
            planner=lambda requested, selected, **kwargs: make_plan(
                requested, selected
            ),
            validator=validator
            or (lambda path, planned, cancel: valid_metadata(planned)),
            clock=lambda: ACQUIRED,
            id_factory=lambda: "tranche-"
            + str(len(list(store.manifests.glob("*.json")))),
        ),
        created,
    )


def test_success_publishes_every_model_and_returns_selected_entry(
    tmp_path, monkeypatch
):
    store = TrancheStore(tmp_path)
    configs = (config("GFS"), config("HRRR"))
    validated_paths = []

    def validator(path, planned, cancel):
        assert path.parent == store.artifacts
        assert (
            path.read_bytes()
            == MODEL_CAPABILITIES[planned.model_id].provider_name.encode()
        )
        validated_paths.append(path)
        return valid_metadata(planned)

    service, created = coordinator(store, configs, validator=validator)
    service.configure(configs, "HRRR")
    original_activate = store.activate
    resolve_calls = []
    original_resolve = store.resolve

    def resolve(*args, **kwargs):
        resolve_calls.append(args[0])
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(store, "resolve", resolve)

    def activate(*args, **kwargs):
        assert not list(tmp_path.glob(".refresh-*"))
        return original_activate(*args, **kwargs)

    monkeypatch.setattr(store, "activate", activate)
    result = service.refresh()
    assert result.selection.entry_id == "HRRR"
    assert {entry.model_id for entry in result.tranche.entries} == {"GFS", "HRRR"}
    assert len(validated_paths) == 2
    assert [json.loads(s.identifiers[0])["cycle"] for s in created] == ["00", "00"]
    assert len(resolve_calls) == 1
    assert all(entry.acquired_at == ACQUIRED for entry in result.tranche.entries)


def test_refresh_reuses_same_run_local_model_and_downloads_only_missing_model(
    tmp_path,
):
    store = TrancheStore(tmp_path)
    original_config = config("GFS", hours=6)
    configs = (config("GFS"), config("HRRR"))
    first, _ = coordinator(store, (original_config,))
    first.configure((original_config,), "GFS")
    original = first.refresh()
    original_gfs = original.tranche.entries[0]

    second, created = coordinator(store, configs)
    second.configure(configs, "HRRR")
    refreshed = second.refresh()

    assert {entry.model_id for entry in refreshed.tranche.entries} == {"GFS", "HRRR"}
    reused_gfs = next(e for e in refreshed.tranche.entries if e.model_id == "GFS")
    assert reused_gfs.artifacts == original_gfs.artifacts
    assert max(reused_gfs.time_steps) == RUN + timedelta(hours=3)
    assert [source.name for source in created] == ["NOAA HRRR"]


def test_local_reuse_requires_the_latest_planned_model_run(tmp_path):
    store = TrancheStore(tmp_path)
    cfg = config("GFS")
    service, _ = coordinator(store, (cfg,))
    service.configure((cfg,), "GFS")
    service.refresh()
    planned = make_plan((cfg,), "GFS").models[0]
    newer_run = RUN + timedelta(hours=6)
    identifier = json.dumps(
        {
            **json.loads(planned.identifier),
            "date": newer_run.strftime("%Y%m%d"),
            "cycle": newer_run.strftime("%H"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        store.reusable_entry(
            replace(planned, run_time=newer_run, identifier=identifier)
        )
        is None
    )


def test_estimate_excludes_reused_models_from_network_total(tmp_path):
    store = TrancheStore(tmp_path)
    gfs = config("GFS")
    initial, _ = coordinator(store, (gfs,))
    initial.configure((gfs,), "GFS")
    initial.refresh()

    configs = (gfs, config("HRRR"))
    service, _ = coordinator(store, configs)
    service.configure(configs, "HRRR")
    total, models, reused = service.estimate_download(configs, "HRRR")

    assert total > 0
    assert [model for model, _size, _approximate in models] == ["HRRR"]
    assert reused == ("GFS",)
    assert models[0][2] is True


@pytest.mark.parametrize("failure", ["download", "validation", "second-model"])
def test_any_model_failure_preserves_old_active_binding(tmp_path, failure):
    store = TrancheStore(tmp_path)
    initial, _ = coordinator(store, (config(),))
    initial.configure((config(),), "GFS")
    prior = initial.refresh()
    pointer = store.active_path.read_bytes()
    configs = (config("GFS"), config("HRRR"))
    behaviors = (
        {"HRRR": {"download_error": True}}
        if failure in ("download", "second-model")
        else None
    )

    def validator(path, planned, cancel):
        if failure == "validation" and planned.model_id == "HRRR":
            raise ValueError("synthetic validation failure")
        return valid_metadata(planned)

    service, _ = coordinator(store, configs, behaviors=behaviors, validator=validator)
    service.configure(configs, "GFS")
    with pytest.raises(RefreshExecutionError):
        service.refresh()
    assert store.active_path.read_bytes() == pointer
    assert store.active().tranche.tranche_id == prior.tranche.tranche_id


def test_configuration_edit_during_download_retires_ticket(tmp_path):
    entered, release = Event(), Event()
    service, _ = coordinator(
        TrancheStore(tmp_path),
        (config(),),
        behaviors={"GFS": {"entered": entered, "release": release}},
    )
    service.configure((config(),), "GFS")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.refresh)
        assert entered.wait(5)
        service.configure((config("HRRR"),), "HRRR")
        release.set()
        with pytest.raises(RefreshSupersededError):
            future.result(5)
    assert service.store.active() is None


def test_external_cancellation_during_download_preserves_existing_active(tmp_path):
    store = TrancheStore(tmp_path)
    initial, _ = coordinator(store, (config(),))
    initial.configure((config(),), "GFS")
    prior = initial.refresh()
    previous = store.active_path.read_bytes()
    entered, release, cancelled = Event(), Event(), Event()
    updated_config = config("GFS", hours=6)
    service, _ = coordinator(
        store,
        (updated_config,),
        behaviors={"GFS": {"entered": entered, "release": release}},
    )
    service.configure((updated_config,), "GFS")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.refresh, cancel=cancelled)
        assert entered.wait(5)
        cancelled.set()
        release.set()
        with pytest.raises(RefreshCancelledError):
            future.result(5)
    assert store.active_path.read_bytes() == previous
    assert store.active().tranche.tranche_id == prior.tranche.tranche_id


def test_newer_refresh_supersedes_old_refresh_even_with_same_configuration(tmp_path):
    entered, release = Event(), Event()
    calls = []

    def factory():
        calls.append(len(calls))
        behavior = {"entered": entered, "release": release} if len(calls) == 1 else {}
        return FakeSource("GFS", behavior)

    store = TrancheStore(tmp_path)
    plan = make_plan((config(),), "GFS")
    service = RefreshCoordinator(
        store,
        factories={"GFS": factory},
        planner=lambda *args, **kwargs: plan,
        validator=lambda path, planned, cancel: valid_metadata(planned),
        clock=lambda: ACQUIRED,
        id_factory=lambda: "refresh-" + str(len(calls)),
    )
    service.configure((config(),), "GFS")
    with ThreadPoolExecutor(max_workers=1) as pool:
        older = pool.submit(service.refresh)
        assert entered.wait(5)
        newer = service.refresh()
        release.set()
        with pytest.raises(RefreshSupersededError):
            older.result(5)
    assert store.active().tranche.tranche_id == newer.tranche.tranche_id


def test_stale_during_active_pointer_staging_is_blocked_at_commit_guard(
    tmp_path, monkeypatch
):
    store = TrancheStore(tmp_path)
    service, _ = coordinator(store, (config(),))
    service.configure((config(),), "GFS")
    service.refresh()
    previous = store.active_path.read_bytes()
    # The second refresh must require a real download so it exercises the commit
    # path where staleness can be injected; an all-current refresh is a no-op.
    service.configure((config("GFS", hours=6),), "GFS")
    entered, release = Event(), Event()
    original = store._fsync_dir

    def blocked(path):
        if path == store.root:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("commit staging was not released")
        return original(path)

    monkeypatch.setattr(store, "_fsync_dir", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.refresh)
        assert entered.wait(5)
        service.invalidate()
        release.set()
        with pytest.raises(RefreshSupersededError):
            future.result(5)
    assert store.active_path.read_bytes() == previous
    assert not list(tmp_path.glob(".active-*"))


def test_replace_failure_preserves_previous_binding(tmp_path, monkeypatch):
    store = TrancheStore(tmp_path)
    service, _ = coordinator(store, (config(),))
    service.configure((config(),), "GFS")
    service.refresh()
    previous = store.active_path.read_bytes()
    # The second refresh must require a real download so the replace failure can
    # be exercised; an all-current refresh is a no-op.
    service.configure((config("GFS", hours=6),), "GFS")
    service.id_factory = lambda: "replacement"
    original_replace = __import__("os").replace

    def fail_replace(source, destination):
        if Path(destination) == store.active_path:
            raise OSError("injected pointer replacement failure")
        return original_replace(source, destination)

    monkeypatch.setattr("gweatherrouting.core.tranchestore.os.replace", fail_replace)
    with pytest.raises(RefreshExecutionError, match="activation failed"):
        service.refresh()
    assert store.active_path.read_bytes() == previous
    assert store.active().tranche.tranche_id != "replacement"
    assert not list(tmp_path.glob(".active-*"))


def test_registered_blob_is_opened_by_production_validator(tmp_path):
    from test.forecastvalidation_fixtures import Dataset, band, planned as fixture_plan

    planned = fixture_plan()
    configs = (planned.configuration,)
    plan = ForecastRefreshPlan((planned,), "GFS")
    factories, _ = make_factories()

    def validator(path, item, cancel):
        bands = [band(element, hour) for hour in (0, 3) for element in ("UGRD", "VGRD")]
        return inspect_download(
            path,
            item,
            cancel,
            opener=lambda _: Dataset(bands),
            vector_reader=lambda _: [True] * len(bands),
        )

    store = TrancheStore(tmp_path)
    service = RefreshCoordinator(
        store,
        factories=factories,
        planner=lambda *args, **kwargs: plan,
        validator=validator,
        clock=lambda: ACQUIRED,
        id_factory=lambda: "integration",
    )
    service.configure(configs, "GFS")
    result = service.refresh()
    assert result.entry.artifacts[0].path.startswith("artifacts/")
    assert result.entry.field_times[0][1] == (RUN, RUN + timedelta(hours=3))


def make_plan_at(configurations, selected, run_by_model=None):
    """Build a plan like make_plan, with a run-time override per model id."""
    run_by_model = run_by_model or {}
    models = []
    for cfg in configurations:
        capability = MODEL_CAPABILITIES[cfg.model_id]
        run = run_by_model.get(cfg.model_id, RUN)
        field_steps = tuple(
            (
                field,
                tuple(
                    hour
                    for hour in capability.available_steps(0, field)
                    if hour <= cfg.forecast_hours
                    and (not cfg.interval or hour % cfg.interval == 0)
                    and (not cfg.steps or hour in cfg.steps)
                ),
            )
            for field in cfg.fields
        )
        union = sorted({hour for _, times in field_steps for hour in times})
        identifier = json.dumps(
            {
                "source": capability.provider_name,
                "date": run.strftime("%Y%m%d"),
                "cycle": run.strftime("%H"),
                "hours": max(union),
                "steps": union,
                "fields": list(cfg.fields),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        models.append(
            PlannedModel(
                cfg,
                run,
                capability.provider_name,
                identifier,
                capability.native_resolution,
                field_steps,
            )
        )
    return ForecastRefreshPlan(tuple(models), selected)


def test_refresh_does_not_transfer_when_all_selected_models_are_current(tmp_path):
    store = TrancheStore(tmp_path)
    cfg = config("GFS")
    service, created = coordinator(store, (cfg,))
    service.configure((cfg,), "GFS")
    first = service.refresh()
    downloads_before = len(created)
    assert downloads_before == 1

    second = service.refresh()

    # No new download source was created and the published selection is unchanged:
    # every selected model is already current for its latest planned run.
    assert len(created) == downloads_before
    assert second.tranche.tranche_id == first.tranche.tranche_id


def test_models_needing_download_empty_when_all_current(tmp_path):
    store = TrancheStore(tmp_path)
    gfs = config("GFS")
    service, _ = coordinator(store, (gfs,))
    service.configure((gfs,), "GFS")
    service.refresh()
    needed = service.models_needing_download((gfs,), "GFS")
    assert needed == ()


def test_models_needing_download_reports_model_with_newer_run(tmp_path):
    store = TrancheStore(tmp_path)
    gfs = config("GFS")
    service, _ = coordinator(store, (gfs,))
    service.configure((gfs,), "GFS")
    service.refresh()
    newer_run = RUN + timedelta(hours=6)

    def newer_planner(requested, selected, **kwargs):
        return make_plan_at(requested, selected, {"GFS": newer_run})

    service.planner = newer_planner
    needed = service.models_needing_download((gfs,), "GFS")
    assert [model.model_id for model in needed] == ["GFS"]


def test_refresh_downloads_only_model_with_newer_run(tmp_path):
    store = TrancheStore(tmp_path)
    gfs = config("GFS")
    hrrr = config("HRRR")
    service, created = coordinator(store, (gfs, hrrr))
    service.configure((gfs, hrrr), "HRRR")
    first = service.refresh()
    assert {entry.model_id for entry in first.tranche.entries} == {"GFS", "HRRR"}
    sources_before = [source.name for source in created]

    newer_run = RUN + timedelta(hours=6)

    def newer_planner(requested, selected, **kwargs):
        return make_plan_at(requested, selected, {"GFS": newer_run})

    service.planner = newer_planner
    second = service.refresh()
    assert {entry.model_id for entry in second.tranche.entries} == {"GFS", "HRRR"}

    # Only GFS had a newer forecast available; HRRR was already current.
    newly_downloaded = [source.name for source in created][len(sources_before) :]
    assert newly_downloaded == ["NOAA GFS"]
