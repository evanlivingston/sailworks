"""Display-only wave selection: bindable for display, never for routing."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from sailworks.core.forecastcapabilities import display_model_name
from sailworks.qt.analysiscontroller import AnalysisController
from sailworks.qt.analysisdisplay import BoundDisplayForecastSelection
from sailworks.qt.weatherpanel import ForecastModelSelector, WeatherPanel
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


PANEL_SETTINGS = {
    "gribWindStyle": "barbs",
    "gribBarbDensity": "standard",
    "gribWindGradient": True,
    "gribWindGradientOpacity": 0.12,
    "gribWindGradientSmooth": True,
    "gribWindGradientLevels": 15,
    "gribArrowOpacity": 0.4,
    "gribWaveDensity": "standard",
    "gribWaveOpacity": 0.85,
    "gribWaveSmooth": True,
    "gribWaveLevels": 9,
    "gribIsobarsVisible": True,
    "gribIsobarInterval": 4.0,
    "gribIsobarOpacity": 0.85,
    "gribIsobarLabels": True,
    "gribWaveSource": "",
}


class SettingsDouble(dict):
    """Settings double supporting the attribute and item access used in Qt."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


class SignalStub:
    def connect(self, *_args, **_kwargs):
        pass

    def disconnect(self, *_args, **_kwargs):
        pass


class MapDouble:
    def __init__(self):
        self.wind_visible = False
        self.wave_visible = False
        self._weather_presenter = None
        self.ship_observations = SimpleNamespace(
            visible=False,
            loading=False,
            status="",
            changed=SignalStub(),
            refresh=lambda: None,
            set_visible=lambda _visible: None,
        )

    def queue_draw(self):
        pass


def make_panel(controller, selection, stored=""):
    settings = SettingsDouble(PANEL_SETTINGS)
    settings["gribWaveSource"] = stored
    controller.settings = settings
    controller.display_forecast = selection
    controller.data = SimpleNamespace(redraw=lambda: None)
    active_map = MapDouble()
    panel = WeatherPanel(controller, lambda: active_map, lambda: None)
    return panel, settings


def test_choosing_a_wave_source_persists_and_clears_the_setting(tmp_path, monkeypatch):
    application, controller, selection = start(
        tmp_path, monkeypatch, snapshot_with(tmp_path, wave_entry())
    )
    panel, settings = make_panel(controller, selection)
    key = selection.wave.choices[0].key
    assert panel.wave_forecast.findData(key) == 1

    panel.wave_forecast.setCurrentIndex(1)
    assert settings["gribWaveSource"] == key
    assert selection.wave.selected_key == key

    panel.wave_forecast.setCurrentIndex(0)
    assert settings["gribWaveSource"] == ""
    assert selection.wave.selected_key == ""
    panel.dispose()
    selection.close()
    controller.close()


def test_stored_wave_source_restores_and_survives_a_rescan(tmp_path, monkeypatch):
    application, controller, selection = start(
        tmp_path, monkeypatch, snapshot_with(tmp_path, wave_entry())
    )
    key = selection.wave.choices[0].key
    panel, settings = make_panel(controller, selection, key)

    assert selection.wave.selected_key == key
    assert panel.wave_forecast.currentData() == key
    assert selection.wave.sources()[0].model_name == "GFSWAVE"

    # The display scan republishes its choices; the stored key must be applied
    # once they are known again.
    selection.refresh()
    wait_for(
        lambda: not selection.loading and selection.wave.choices,
        application,
        detail=lambda: f"error={selection.error!r}; status={selection.status!r}",
    )
    assert selection.wave.selected_key == key
    assert panel.wave_forecast.currentData() == key
    assert selection.wave.sources()[0].model_name == "GFSWAVE"
    assert settings["gribWaveSource"] == key
    panel.dispose()
    selection.close()
    controller.close()


def test_unknown_stored_wave_source_falls_back_to_routing_and_clears(
    tmp_path, monkeypatch
):
    application, controller, selection = start(
        tmp_path, monkeypatch, snapshot_with(tmp_path, wave_entry())
    )
    panel, settings = make_panel(controller, selection, "tranche:gone:stale")

    assert panel.wave_forecast.currentData() == ""
    assert selection.wave.selected_key == ""
    assert selection.wave.sources()[0].model_name == "GFS"
    assert settings["gribWaveSource"] == ""
    panel.dispose()
    selection.close()
    controller.close()


def test_wave_selector_sits_in_the_waves_section_with_a_short_label(
    tmp_path, monkeypatch
):
    application, controller, selection = start(
        tmp_path, monkeypatch, snapshot_with(tmp_path, wave_entry())
    )
    panel, _settings = make_panel(controller, selection)

    assert panel.waves_section.isAncestorOf(panel.wave_forecast)
    assert not panel.waves_section.isAncestorOf(panel.forecast_model)
    assert panel.wave_forecast_row.parent() is panel.waves_section
    layout = panel.waves_section.layout()
    assert layout.indexOf(panel.wave) < layout.indexOf(panel.wave_forecast_row)

    expected = f"{display_model_name('GFSWAVE')} · 1 Jan 00:00 UTC"
    assert panel.wave_forecast.itemText(1) == expected
    assert "member deterministic" not in panel.wave_forecast.itemText(1)
    assert "run " not in panel.wave_forecast.itemText(1)
    panel.dispose()
    selection.close()
    controller.close()
