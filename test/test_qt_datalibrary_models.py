from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QGroupBox, QTabWidget, QWidget

from gweatherrouting.core.forecasttranche import ModelConfiguration
from gweatherrouting.qt.datalibrary import DataLibrary
from gweatherrouting.qt.forecastdownload import AnalysisForecastDownloadOptions
from test.analysis_qt_fixtures import make_published_snapshot
from test.qt_analysis_fakes import FakeAnalysis, FakeSession, app


def forecast_page(tmp_path, *, capture_region):
    configuration = SimpleNamespace(
        configurations=(
            ModelConfiguration(
                "GFS", ("wind",), (-1, -1, 1, 1), "native", 120, (0, 3), 3
            ),
        ),
        refresh_model_id="GFS",
    )
    analysis = FakeAnalysis(tmp_path, configuration, make_published_snapshot(tmp_path))
    session = FakeSession(analysis)
    session.data = SimpleNamespace()
    library = DataLibrary.__new__(DataLibrary)
    QDialog.__init__(library)
    library.controller = session
    library.data = session.data
    library.capture_region = capture_region
    library.mutations = []
    library.tabs = QTabWidget(library)
    library._forecasts()
    return library, session, analysis


def test_forecasts_page_reuses_existing_model_parameters_and_hours_ui(tmp_path):
    app()
    capture = lambda: (-2, -2, 2, 2)
    library, session, analysis = forecast_page(tmp_path, capture_region=capture)

    assert library.tabs.tabText(0) == "Forecasts"
    options = library.forecast_options
    assert isinstance(options, AnalysisForecastDownloadOptions)
    assert options.parentWidget() is library.forecast_page
    assert not hasattr(library, "modelspanel")
    assert {
        options.active_model.itemData(index)
        for index in range(options.active_model.count())
    } == {"GFS", "HRRR"}
    assert options.active_model.currentData() == "GFS"
    assert {"ECMWF IFS", "ECMWF AIFS Single", "NOAA AI-GFS"} <= set(options.models)
    assert options.fields["wind"].text() == "Wind at 10 m"
    assert options.fields["gust"].text() == "Wind gust"
    assert options._parameter_model_id == "GFS"
    assert options.model_rows["NOAA GFS"].property("selected")
    assert not options.fields["gust"].isHidden()
    assert not options.fields["gust"].isChecked()
    assert options.duration.count() >= 8
    assert {box.title() for box in options.findChildren(QGroupBox)} >= {
        "Models",
        "Parameters",
        "Forecast hours",
    }
    assert not any(
        "legacy forecast download options" in child.text().lower()
        for child in library.forecast_page.findChildren(QWidget)
        if callable(getattr(child, "text", None))
    )
    assert library.forecast_download is options.refresh_button

    options.region_button.click()
    assert options.region == capture()
    assert analysis.configuration.configurations[0].region == capture()
    options.models["NOAA HRRR"].setChecked(True)
    configured_hours = {
        item.model_id: item.forecast_hours
        for item in analysis.configuration.configurations
    }
    assert configured_hours == {"GFS": 120, "HRRR": 48}
    options.region = None
    options._update_refresh_availability()
    assert not options.refresh_button.isEnabled()
    assert "capture" in options.refresh_button.toolTip().lower()
    options.region = capture()
    options._save_intent()
    assert options.refresh_button.isEnabled()
    options.models["NOAA HRRR"].setChecked(True)
    configured_hours = {
        config.model_id: config.forecast_hours
        for config in analysis.configuration.configurations
    }
    assert configured_hours == {"GFS": 120, "HRRR": 48}
    assert not hasattr(library, "files_dialog")
    assert not hasattr(library, "import_forecast_button")

    library._forecast_tab_changed()
    assert session.prepare_calls == 1
    other_page = QWidget()
    library.tabs.addTab(other_page, "Charts")
    library.tabs.setCurrentWidget(other_page)
    library._forecast_tab_changed()
    assert session.prepare_calls == 1
    library.tabs.setCurrentWidget(library.forecast_page)
    library._forecast_tab_changed()
    assert session.prepare_calls == 1
    library.close()


def test_changing_forecast_hours_emits_changed(tmp_path):
    app()
    library, _, _ = forecast_page(tmp_path, capture_region=lambda: (-2, -2, 2, 2))
    options = library.forecast_options
    emitted = []
    options.changed.connect(lambda: emitted.append(True))

    item = options.hours.item(0, 0)
    item.setCheckState(Qt.CheckState.Unchecked)

    assert emitted == [True]
    library.close()


def test_gust_selection_is_limited_to_models_with_supported_products(tmp_path):
    app()
    library, _, _ = forecast_page(tmp_path, capture_region=lambda: (-2, -2, 2, 2))
    options = library.forecast_options
    options.models["NOAA HRRR"].setChecked(True)
    options.fields["gust"].setChecked(True)

    configurations = {c.model_id: c for c in options._configurations()}
    assert "gust" in configurations["GFS"].fields
    assert "gust" not in configurations["HRRR"].fields
    checked_models = options.names()
    assert options._parameter_model_id == "GFS"

    options.model_rows["NOAA HRRR"].selected.emit("HRRR")
    assert options.fields["gust"].isChecked() is False
    options.fields["gust"].setChecked(True)
    configurations = {c.model_id: c for c in options._configurations()}
    assert "gust" in configurations["GFS"].fields
    assert "gust" in configurations["HRRR"].fields

    options.models["ECMWF IFS"].setChecked(True)
    assert options._parameter_model_id == "HRRR"
    options.model_rows["ECMWF IFS"].selected.emit("IFS")
    assert options.fields["gust"].isHidden()
    configurations = {c.model_id: c for c in options._configurations()}
    assert "gust" in configurations["GFS"].fields
    assert "gust" in configurations["HRRR"].fields
    assert "gust" not in configurations["IFS"].fields
    assert options.names() == checked_models + ("ECMWF IFS",)
    assert options._parameter_model_id == "IFS"
    assert options.active_model.currentData() == "GFS"
    library.close()


def test_gfswave_is_a_separate_wave_only_download_model(tmp_path):
    app()
    library, _, _ = forecast_page(tmp_path, capture_region=lambda: (-2, -2, 2, 2))
    options = library.forecast_options
    options.region = (-2, -2, 2, 2)

    assert options.models["NOAA GFS-Wave"].isChecked() is False
    assert options.fields["wave_height"].isHidden()
    options.models["NOAA GFS-Wave"].setChecked(True)
    options.model_rows["NOAA GFS-Wave"].selected.emit("GFSWAVE")

    assert not options.fields["wave_height"].isHidden()
    assert not options.fields["wave_period"].isHidden()
    assert not options.fields["wave_direction"].isHidden()
    configurations = {item.model_id: item for item in options._configurations()}
    assert configurations["GFS"].fields == ("wind",)
    assert configurations["GFSWAVE"].fields == (
        "wave_height",
        "wave_period",
        "wave_direction",
    )
    assert options.active_model.currentData() == "GFS"

    options.models["NOAA GFS"].setChecked(False)
    with pytest.raises(ValueError, match="wind forecast model"):
        options._configurations()
    library.close()


def test_refresh_failure_is_visible_while_previous_model_remains_active(tmp_path):
    app()
    library, _, analysis = forecast_page(
        tmp_path, capture_region=lambda: (-2, -2, 2, 2)
    )
    options = library.forecast_options
    analysis.error = "HRRR validation failed: missing wind coverage"

    options._update_active_status()

    assert options.size_estimate.text() == "Download estimate unavailable."
    assert "missing wind coverage" in options.size_estimate.toolTip()
    assert options.active_model.currentData() == "GFS"
    library.close()


def test_data_library_without_map_callback_has_no_forecast_download_controls(tmp_path):
    app()
    library, _, _ = forecast_page(tmp_path, capture_region=None)
    assert library.forecast_options is None
    assert library.forecast_download is None
    assert not hasattr(library, "files_dialog")
    library.close()
