from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from gweatherrouting.qt.analysiscontroller import AnalysisController
from gweatherrouting.qt.analysisdisplay import BoundDisplayForecastSelection
from gweatherrouting.rendering.displayforecast import DisplayForecast
from test.analysis_qt_fixtures import FakeAnalysisState, make_snapshot

RUN = datetime(2025, 1, 1, tzinfo=timezone.utc)


def app():
    return QApplication.instance() or QApplication([])


def wait_for(predicate, application, timeout=2000, detail=None):
    if predicate():
        return
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(0)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    poll.start()
    timer.start(timeout)
    loop.exec()
    poll.stop()
    timer.stop()
    if not predicate():
        diagnostic = detail() if callable(detail) else detail
        pytest.fail(f"display metadata worker was not delivered: {diagnostic or ''}")


def display(path, version, valid_times=None):
    model = path.rsplit("/", 1)[-1].upper()
    epoch = RUN.timestamp()
    process = {"GFS": ("0", "0", "gfs"), "HRRR": ("0", "0", "83")}[model]
    return DisplayForecast(
        path,
        model + ".grib",
        "NOAA",
        (0, 0, 1, 1),
        datetime.fromtimestamp(epoch),
        0,
        model,
        (epoch, (("7", ""), process, (), "")),
        epoch,
        epoch,
        version,
        valid_times=(epoch,) if valid_times is None else tuple(valid_times),
    )


def test_sources_and_choice_follow_the_single_active_model(tmp_path, monkeypatch):
    import gweatherrouting.qt.analysisdisplay as module

    monkeypatch.setattr(
        module,
        "inspect_forecasts",
        lambda entries: ((display(*entries[0]),), ""),
    )
    application = app()
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    controller = AnalysisController(state=state)
    wait_for(lambda: not controller.loading, application)
    selection = BoundDisplayForecastSelection(controller)
    wait_for(
        lambda: not selection.loading,
        application,
        detail=lambda: f"error={selection.error!r}; status={selection.status!r}",
    )
    assert len(selection.choices) == 2, selection.error
    assert len(selection.sources()) == 1
    assert selection.sources()[0].model_name == "GFS"
    assert selection.sources()[0].valid_times == (RUN.timestamp(),)
    old_key = selection.selected_key
    other = next(choice for choice in selection.choices if choice.model_name == "HRRR")
    assert selection.choose(other.key)
    wait_for(
        lambda: controller.active is not None
        and controller.active.entry.model_id == "HRRR",
        application,
    )
    wait_for(
        lambda: not selection.loading
        and bool(selection.sources())
        and selection.sources()[0].model_name == "HRRR",
        application,
    )
    assert old_key != selection.selected_key
    selection.close()
    controller.close()


def test_display_refresh_rescans_current_artifacts_without_downloading(
    tmp_path, monkeypatch
):
    import gweatherrouting.qt.analysisdisplay as module

    monkeypatch.setattr(
        module,
        "inspect_forecasts",
        lambda entries: ((display(*entries[0]),), ""),
    )
    application = app()
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    refresh_calls = 0
    original_refresh = state.refresh

    def count_refresh(*args, **kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        return original_refresh(*args, **kwargs)

    state.refresh = count_refresh
    controller = AnalysisController(state=state)
    wait_for(lambda: not controller.loading, application)
    selection = BoundDisplayForecastSelection(controller)
    wait_for(lambda: not selection.loading, application)
    old_source = selection.sources()[0]
    old_key = selection.selected_key
    assert selection.refresh()
    wait_for(
        lambda: not selection.loading,
        application,
        detail=lambda: f"error={selection.error!r}; status={selection.status!r}",
    )
    assert refresh_calls == 0
    assert selection.selected_key == old_key
    assert selection.sources()[0].analysis_identity == old_source.analysis_identity
    selection.close()
    assert not selection.refresh()
    assert refresh_calls == 0
    controller.close()


def test_identical_artifact_in_new_tranche_rejects_old_scan_result(
    tmp_path, monkeypatch
):
    import gweatherrouting.qt.analysisdisplay as module

    entered, release = Event(), Event()
    calls = 0

    def scan(entries):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("old display scan was not released")
        return (display(*entries[0]),), ""

    monkeypatch.setattr(module, "inspect_forecasts", scan)
    application = app()
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path, "tranche-a"))
    controller = AnalysisController(state=state)
    wait_for(lambda: not controller.loading, application)
    selection = BoundDisplayForecastSelection(controller)
    assert entered.wait(2)
    old_generation = selection._generation
    old_result = Event()
    selection._scanned.connect(
        lambda generation, _result, _binding: (
            old_result.set() if generation == old_generation else None
        )
    )
    new_snapshot = make_snapshot(tmp_path, "tranche-b")
    controller._active = new_snapshot
    controller.changed.emit()
    try:
        wait_for(
            lambda: not selection.loading and selection.sources(),
            application,
            detail=lambda: f"error={selection.error!r}; status={selection.status!r}",
        )
        current_key = selection.sources()[0].key
        assert "tranche-b" in current_key
        release.set()
        wait_for(
            lambda: old_result.is_set(),
            application,
            detail=lambda: f"error={selection.error!r}; status={selection.status!r}",
        )
        assert selection.sources()[0].key == current_key
    finally:
        release.set()
        selection.close()
        controller.close()


def test_multifile_entry_is_unavailable_not_stitched(tmp_path, monkeypatch):
    import gweatherrouting.qt.analysisdisplay as module

    monkeypatch.setattr(module, "inspect_forecasts", lambda entries: ((), ""))
    application = app()
    snapshot = make_snapshot(tmp_path)
    entry = snapshot.entry
    entry.artifacts = entry.artifacts + (
        SimpleNamespace(path="artifacts/extra", sha256="b" * 64, size=1),
    )
    state = FakeAnalysisState(tmp_path, snapshot)
    controller = AnalysisController(state=state)
    wait_for(lambda: not controller.loading, application)
    selection = BoundDisplayForecastSelection(controller)
    wait_for(lambda: not selection.loading, application)
    assert not selection.sources()
    assert "multiple artifacts" in selection.error
    selection.close()
    controller.close()
