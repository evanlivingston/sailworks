from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from gweatherrouting.core.forecastcapabilities import MODEL_CAPABILITIES
from gweatherrouting.core.forecastrefresh import RefreshSupersededError
from gweatherrouting.core.forecaststate import (
    ForecastAnalysisState,
    ForecastConfiguration,
    ForecastUnavailableError,
    NoActiveForecastError,
)
from gweatherrouting.core.forecasttranche import (
    AnalysisSelection,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)
from gweatherrouting.core.tranchestore import TrancheStore
from test.test_forecastrefresh import (
    FakeSource,
    make_plan as make_refresh_plan,
    valid_metadata,
)

RUN = datetime(2025, 1, 1, tzinfo=timezone.utc)
ACQUIRED = datetime(2025, 1, 2, tzinfo=timezone.utc)


def config(model="GFS", fields=("wind",)):
    return ModelConfiguration(model, fields, (-1, -1, 1, 1), "native", 0, (0,), 0)


def install_tranche(
    store, models=("GFS", "HRRR"), *, fields_by_model=None, artifacts_per_model=1
):
    configurations = tuple(
        config(model, (fields_by_model or {}).get(model, ("wind",))) for model in models
    )
    entries = []
    for model, configuration in zip(models, configurations):
        content = f"forecast-{model}".encode()
        artifact = store.register_file(_write(store.root / f"{model}.src", content))
        artifacts = [artifact]
        artifact_count = (
            artifacts_per_model.get(model, 1)
            if isinstance(artifacts_per_model, dict)
            else artifacts_per_model
        )
        for index in range(1, artifact_count):
            extra = store.register_file(
                _write(
                    store.root / f"{model}-{index}.src", content + str(index).encode()
                )
            )
            artifacts.append(extra)
        field_times = tuple((field, (RUN,)) for field in configuration.fields)
        entries.append(
            ModelEntry(
                model,
                model,
                RUN,
                "deterministic",
                configuration.fields,
                tuple(
                    "10-HTGL" if field == "wind" else "0-MSL"
                    for field in configuration.fields
                ),
                configuration.region,
                MODEL_CAPABILITIES[model].native_resolution,
                (RUN,),
                tuple(artifacts),
                ACQUIRED,
                field_times,
            )
        )
    tranche = ForecastTranche("initial", ACQUIRED, configurations, tuple(entries))
    store.publish(tranche)
    store.activate(AnalysisSelection(tranche.tranche_id, entries[0].entry_id))
    return tranche


def _write(path, data):
    path.write_bytes(data)
    return path


def test_configuration_roundtrip_save_and_restart(tmp_path):
    state = ForecastAnalysisState(tmp_path)
    assert state.configuration is None and not state.configuration_error
    original = [config("GFS"), config("HRRR")]
    saved = state.configure(original, "HRRR")
    original.clear()
    assert saved == ForecastConfiguration((config("GFS"), config("HRRR")), "HRRR")
    assert ForecastConfiguration.from_json(saved.to_json()) == saved
    restarted = ForecastAnalysisState(tmp_path)
    assert restarted.configuration == saved
    assert restarted.configuration_error == ""
    with pytest.raises(ValueError):
        ForecastConfiguration.from_json({**saved.to_json(), "version": True})


def test_bad_saved_configuration_is_visible_but_active_snapshot_remains_usable(
    tmp_path,
):
    store = TrancheStore(tmp_path)
    install_tranche(store, ("GFS",))
    (tmp_path / "configuration.json").write_text("{broken", encoding="utf8")
    state = ForecastAnalysisState(tmp_path)
    assert state.configuration is None
    assert state.configuration_error
    assert state.snapshot(required_fields=("wind",)).entry.model_id == "GFS"
    with pytest.raises(ForecastUnavailableError, match="invalid"):
        state.refresh()


def test_configuration_replace_failure_preserves_configuration_and_active(
    tmp_path, monkeypatch
):
    state = ForecastAnalysisState(tmp_path)
    old = state.configure((config("GFS"),), "GFS")
    store = state.store
    install_tranche(store, ("GFS",))
    active_bytes = store.active_path.read_bytes()
    saved_bytes = state.configuration_path.read_bytes()
    real_replace = __import__("os").replace

    def fail_configuration(source, destination):
        if destination == state.configuration_path:
            raise OSError("injected config replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(
        "gweatherrouting.core.forecaststate.os.replace", fail_configuration
    )
    with pytest.raises(OSError):
        state.configure((config("HRRR"),), "HRRR")
    assert state.configuration == old
    assert state.configuration_path.read_bytes() == saved_bytes
    assert store.active_path.read_bytes() == active_bytes
    assert not list(tmp_path.glob(".configuration-*"))


def test_snapshot_and_selection_stay_inside_current_tranche(tmp_path):
    store = TrancheStore(tmp_path)
    tranche = install_tranche(store)
    state = ForecastAnalysisState(tmp_path)
    before = state.snapshot(required_fields=("wind",))
    state.configure((config("HRRR"), config("GFS")), "GFS")
    after = state.select_model("HRRR")
    assert after.tranche.tranche_id == tranche.tranche_id
    assert after.entry.model_id == "HRRR"
    assert before.selection.entry_id == "GFS"
    assert before.artifact_paths[0].is_file()
    assert after.selection.tranche_id == before.selection.tranche_id
    with pytest.raises(ForecastUnavailableError, match="not present"):
        state.select_model("HRDPS")
    assert store.active_selection().entry_id == "HRRR"


@pytest.mark.parametrize(
    "fields,artifact_count,required,error",
    [
        (("pressure",), 1, ("wind",), "no validated wind"),
        (("wind",), 2, (), "exactly one artifact"),
    ],
)
def test_snapshot_fails_closed_for_missing_field_or_multifile_analysis(
    tmp_path, fields, artifact_count, required, error
):
    store = TrancheStore(tmp_path)
    install_tranche(
        store,
        ("GFS",),
        fields_by_model={"GFS": fields},
        artifacts_per_model=artifact_count,
    )
    state = ForecastAnalysisState(tmp_path)
    with pytest.raises(ForecastUnavailableError, match=error):
        state.snapshot(required_fields=required)


def test_explicit_selection_can_leave_unsupported_current_analysis(tmp_path):
    store = TrancheStore(tmp_path)
    tranche = install_tranche(store, artifacts_per_model={"GFS": 2, "HRRR": 1})
    state = ForecastAnalysisState(tmp_path)
    with pytest.raises(ForecastUnavailableError, match="exactly one artifact"):
        state.snapshot()
    selected = state.select_model("HRRR")
    assert selected.tranche.tranche_id == tranche.tranche_id
    assert selected.entry.model_id == "HRRR"


def test_clear_selection_removes_only_pointer_and_missing_snapshot_is_explicit(
    tmp_path,
):
    store = TrancheStore(tmp_path)
    tranche = install_tranche(store, ("GFS",))
    state = ForecastAnalysisState(tmp_path)
    artifact_path = store.root / tranche.entries[0].artifacts[0].path
    state.clear_selection()
    assert not store.active_path.exists()
    assert artifact_path.is_file()
    assert list(store.manifests.glob("*.json"))
    with pytest.raises(NoActiveForecastError):
        state.snapshot()
    assert state.clear_selection() is None


def test_refresh_prefers_active_model_over_saved_refresh_model(tmp_path, monkeypatch):
    store = TrancheStore(tmp_path)
    install_tranche(store, ("HRRR",))
    state = ForecastAnalysisState(tmp_path)
    state.configure((config("GFS"), config("HRRR")), "GFS")
    observed = []

    def execute(capture, *, cancel=None, progress=None, action_guard=None):
        observed.append(capture[1])
        return capture

    monkeypatch.setattr(state.coordinator, "_execute_refresh", execute)
    assert state.refresh()[1] == "HRRR"
    assert state.configuration.refresh_model_id == "GFS"


def test_newer_selection_wins_while_older_selection_hashes_outside_guard(
    tmp_path, monkeypatch
):
    store = TrancheStore(tmp_path)
    install_tranche(store)
    state = ForecastAnalysisState(tmp_path)
    entered, release = Event(), Event()
    original_resolve = state.store.resolve
    calls = []

    def delayed_resolve(selection):
        calls.append(selection)
        if len(calls) == 1:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("selection hash was not released")
        return original_resolve(selection)

    monkeypatch.setattr(state.store, "resolve", delayed_resolve)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(state.select_model, "HRRR")
        assert entered.wait(5)
        newest = state.select_model("GFS")
        release.set()
        with pytest.raises(ForecastUnavailableError, match="superseded"):
            old.result(5)
    assert newest.entry.model_id == "GFS"
    assert store.active_selection().entry_id == "GFS"


def test_refresh_capture_uses_shared_lock_and_active_binding_never_migrates(tmp_path):
    store = TrancheStore(tmp_path)
    install_tranche(store, ("GFS",))
    state = ForecastAnalysisState(tmp_path)
    desired = state.configure((config("HRRR"),), "HRRR")
    active = state.snapshot()
    assert desired.refresh_model_id == "HRRR"
    assert active.entry.model_id == "GFS"
    state.invalidate()
    assert state.snapshot().entry.model_id == "GFS"


def test_configuration_edit_supersedes_captured_refresh_without_switching_active(
    tmp_path,
):
    store = TrancheStore(tmp_path)
    install_tranche(store, ("GFS",))
    entered, release = Event(), Event()

    def planner(configurations, selected, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("planner was not released")
        return make_refresh_plan(configurations, selected)

    state = ForecastAnalysisState(
        tmp_path,
        factories={},
        planner=planner,
        validator=lambda *args: None,
    )
    state.configure((config("GFS"),), "GFS")
    previous_pointer = store.active_path.read_bytes()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(state.refresh)
        assert entered.wait(5)
        state.configure((config("HRRR"),), "HRRR")
        release.set()
        with pytest.raises(RefreshSupersededError):
            future.result(5)
    assert state.configuration.configurations == (config("HRRR"),)
    assert store.active_path.read_bytes() == previous_pointer
    assert store.active().entry.model_id == "GFS"


def test_reserved_newer_selection_defeats_late_queued_selection_and_clear(tmp_path):
    store = TrancheStore(tmp_path)
    install_tranche(store)
    state = ForecastAnalysisState(tmp_path)
    older = state.begin_action()
    newer = state.begin_action()
    state.select_model("HRRR", action_token=newer)
    active = store.active_path.read_bytes()
    ticket = state.coordinator._ticket
    with pytest.raises(ForecastUnavailableError, match="token is stale"):
        state.select_model("GFS", action_token=older)
    with pytest.raises(ForecastUnavailableError, match="token is stale"):
        state.clear_selection(action_token=older)
    assert state.coordinator._ticket == ticket
    assert store.active_path.read_bytes() == active
    assert store.active_selection().entry_id == "HRRR"


def test_stale_queued_refresh_cannot_retire_newer_running_refresh(tmp_path):
    entered, release = Event(), Event()

    def planner(configurations, selected, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("refresh planner was not released")
        return make_refresh_plan(configurations, selected)

    service = ForecastAnalysisState(
        tmp_path,
        factories={"GFS": lambda: FakeSource("GFS")},
        planner=planner,
        validator=lambda path, planned, cancel: valid_metadata(planned),
        clock=lambda: ACQUIRED,
        id_factory=lambda: "queued-refresh",
    )
    service.configure((config("GFS"),), "GFS")
    older = service.begin_action()
    newer = service.begin_action()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(service.refresh, action_token=newer)
        assert entered.wait(5)
        ticket = service.coordinator._ticket
        cancelled = Event()
        cancelled.set()
        with pytest.raises(ForecastUnavailableError, match="token is stale"):
            service.refresh(action_token=older, cancel=cancelled)
        assert service.coordinator._ticket == ticket
        release.set()
        resolved = running.result(5)
    assert resolved.entry.model_id == "GFS"
    assert service.store.active_selection().entry_id == "GFS"


def test_invalidate_retires_unstarted_select_clear_and_refresh_actions(tmp_path):
    store = TrancheStore(tmp_path)
    install_tranche(store, ("GFS",))
    state = ForecastAnalysisState(tmp_path)
    state.configure((config("GFS"),), "GFS")
    token = state.begin_action()
    state.invalidate()
    for action in (
        lambda: state.select_model("GFS", action_token=token),
        lambda: state.clear_selection(action_token=token),
        lambda: state.refresh(action_token=token),
    ):
        with pytest.raises(ForecastUnavailableError, match="token is stale"):
            action()
    assert store.active_selection().entry_id == "GFS"
