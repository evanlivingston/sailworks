"""Display-only wave selection: bindable for display, never for routing."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from sailworks.qt.analysiscontroller import AnalysisController
from sailworks.qt.analysisdisplay import BoundDisplayForecastSelection
from sailworks.qt.weatherpanel import ForecastModelSelector
from sailworks.rendering.displayforecast import DisplayForecast
from test.analysis_qt_fixtures import (
    FakeAnalysisState,
    FakeSnapshot,
    make_snapshot,
)

RUN = datetime(2025, 1, 1, tzinfo=timezone.utc)
PROCESS = {
    "GFS": ("0", "0", "gfs"),
    "HRRR": ("0", "0", "83"),
    "GFSWAVE": ("0", "0", "wave"),
    "GFSRAIN": ("0", "0", "rain"),
}


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


def forecast(path, version):
    model = path.rsplit("/", 1)[-1].upper()
    epoch = RUN.timestamp()
    return DisplayForecast(
        path,
        model + ".grib",
        "NOAA",
        (0, 0, 1, 1),
        datetime.fromtimestamp(epoch),
        0,
        model,
        (epoch, (("7", ""), PROCESS[model], (), "")),
        epoch,
        epoch,
        version,
        valid_times=(epoch,),
    )


def scan(entries):
    return (forecast(entries[0][0], entries[0][1][0]),), ""


def entry(entry_id, model_id, fields):
    return SimpleNamespace(
        entry_id=entry_id,
        model_id=model_id,
        run_time=RUN,
        member="deterministic",
        fields=tuple(fields),
        field_times=tuple((field, (RUN,)) for field in fields),
        time_steps=(RUN,),
        artifacts=(
            SimpleNamespace(
                path=f"artifacts/{entry_id}",
                sha256=(entry_id * 32)[:64],
                size=1,
            ),
        ),
    )


def snapshot_with(root, *extra, tranche_id="tranche-a"):
    snapshot = make_snapshot(root, tranche_id)
    tranche = SimpleNamespace(
        tranche_id=tranche_id,
        entries=snapshot.tranche.entries + tuple(extra),
    )
    return FakeSnapshot(
        snapshot.selection, tranche, snapshot.entry, snapshot.artifact_paths
    )


def wave_entry():
    return entry("gfswave", "GFSWAVE", ("wave_height", "wave_direction"))


def start(tmp_path, monkeypatch, snapshot):
    monkeypatch.setattr(
        "sailworks.qt.analysisdisplay.inspect_forecasts", lambda entries: scan(entries)
    )
    application = app()
    state = FakeAnalysisState(tmp_path, snapshot)
    controller = AnalysisController(state=state)
    wait_for(lambda: not controller.loading, application)
    selection = BoundDisplayForecastSelection(controller)
    wait_for(
        lambda: not selection.loading,
        application,
        detail=lambda: f"error={selection.error!r}; status={selection.status!r}",
    )
    return application, controller, selection


def test_wave_entry_is_offered_and_never_activates_routing(tmp_path, monkeypatch):
    application, controller, selection = start(
        tmp_path, monkeypatch, snapshot_with(tmp_path, wave_entry())
    )
    wave = selection.wave
    assert [choice.model_name for choice in wave.choices] == ["GFSWAVE"]
    assert wave.selected_key == ""
    assert wave.sources()[0].model_name == "GFS"
    assert selection.sources()[0].model_name == "GFS"
    routing = ForecastModelSelector._choices(selection.analysis)
    assert "GFSWAVE" not in [model_id for _label, model_id, _target in routing]

    active = controller.active
    changes = []
    wave.changed.connect(lambda: changes.append(1))
    assert wave.choose(wave.choices[0].key)
    assert controller.active is active
    assert wave.selected_key == wave.choices[0].key
    assert wave.sources()[0].model_name == "GFSWAVE"
    assert selection.sources()[0].model_name == "GFS"
    assert changes
    selection.close()
    controller.close()


def test_entry_without_wind_or_wave_is_not_offered(tmp_path, monkeypatch):
    rain = entry("gfsrain", "GFSRAIN", ("precipitation",))
    application, controller, selection = start(
        tmp_path, monkeypatch, snapshot_with(tmp_path, wave_entry(), rain)
    )
    offered = {choice.model_name for choice in selection.wave.choices}
    assert offered == {"GFSWAVE"}
    routing = ForecastModelSelector._choices(selection.analysis)
    assert "GFSRAIN" not in [model_id for _label, model_id, _target in routing]
    assert not selection.wave.choose("gfsrain")
    selection.close()
    controller.close()


def test_default_display_is_the_routing_entry_without_wave_sources(
    tmp_path, monkeypatch
):
    application, controller, selection = start(
        tmp_path, monkeypatch, make_snapshot(tmp_path)
    )
    wave = selection.wave
    assert wave.choices == ()
    assert wave.selected_key == ""
    assert wave.sources() == selection.sources()
    assert wave.sources()[0].model_name == "GFS"
    selection.close()
    controller.close()


def test_stale_wave_selection_falls_back_to_the_routing_entry(tmp_path, monkeypatch):
    application, controller, selection = start(
        tmp_path, monkeypatch, snapshot_with(tmp_path, wave_entry())
    )
    wave = selection.wave
    assert wave.choose(wave.choices[0].key)
    assert wave.sources()[0].model_name == "GFSWAVE"
    changes = []
    wave.changed.connect(lambda: changes.append(1))

    controller._active = make_snapshot(tmp_path, "tranche-b")
    controller.changed.emit()
    wait_for(
        lambda: not selection.loading and selection.sources(),
        application,
        detail=lambda: f"error={selection.error!r}; status={selection.status!r}",
    )
    assert "tranche-b" in selection.sources()[0].key
    assert wave.choices == ()
    assert wave.selected_key == ""
    assert wave.sources()[0].model_name == "GFS"
    assert wave.sources() == selection.sources()
    assert changes
    selection.close()
    controller.close()
