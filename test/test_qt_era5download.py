from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
from PySide6.QtCore import QDate, QDateTime, QTime, Qt
from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QWidget

from sailworks.qt.era5download import Era5DownloadTab
from test.qt_analysis_fakes import app


def make_tab(tmp_path):
    tab = Era5DownloadTab.__new__(Era5DownloadTab)
    QWidget.__init__(tab)
    tab.service = SimpleNamespace(store=SimpleNamespace(root=tmp_path))
    tab.catalog = QTableWidget(1, 5, tab)
    tab.catalog.setItem(0, 4, QTableWidgetItem("history.grib"))
    tab._rows = [
        {
            "kind": "complete",
            "entry": {
                "start": "2020-01-01T00:00:00Z",
                "end": "2020-01-01T01:00:00Z",
                "artifact": "history.grib",
            },
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-01T01:00:00Z",
            "area": [50, -10, 40, 5],
            "variables": ["10m_u_component_of_wind"],
            "artifact": "history.grib",
        }
    ]
    tab.history_status = __import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel()
    tab.sample_start = Era5DownloadTab._date(datetime(2020, 1, 1, tzinfo=timezone.utc))
    tab.sample_end = Era5DownloadTab._date(datetime(2020, 1, 1, tzinfo=timezone.utc))
    from PySide6.QtWidgets import QDoubleSpinBox

    tab.sample_lat = QDoubleSpinBox()
    tab.sample_lon = QDoubleSpinBox()
    tab.sample_lat.setRange(-90, 90)
    tab.sample_lon.setRange(-180, 180)
    tab.sample_lat.setValue(45)
    tab.sample_lon.setValue(-120)
    tab.catalog.selectRow(0)
    tab.nbs_job = None
    return tab


def test_poll_refreshes_queue_rows_while_download_runs(tmp_path):
    app()
    from PySide6.QtWidgets import QLabel, QProgressBar

    tab = make_tab(tmp_path)
    tab.service = SimpleNamespace(
        store=SimpleNamespace(root=tmp_path),
        pending_requests=lambda: [
            {
                "start": "2026-05-16T00:00:00+00:00",
                "end": "2026-05-16T23:00:00+00:00",
                "area": [49.54, -125.97, 45.2, -121.62],
                "request_id": "rid-queued",
                "created": "2026-11-04T02:44:07+00:00",
            }
        ],
        resume_active=lambda: 0,
    )
    tab.job = SimpleNamespace(is_alive=lambda: True)
    tab.plan = None
    tab.progress = QProgressBar()
    tab.status = QLabel()
    tab._poll_count = 3

    tab.catalog.setRowCount(0)
    tab.poll()

    assert tab.catalog.rowCount() == 1
    assert "Queued at CDS" in tab.catalog.cellWidget(0, 3).text()
    label = tab.catalog.cellWidget(0, 3)
    assert "cds.climate.copernicus.eu/requests" in label.text()
    assert label.openExternalLinks()


def test_refresh_catalog_shows_satellite_wind_status(tmp_path):
    app()
    tab = make_tab(tmp_path)
    tab._nbs_status = {
        "start": "2026-05-16T00:00:00+00:00",
        "end": "2026-05-16T18:00:00+00:00",
        "area": [49.54, -125.97, 45.2, -121.62],
        "status": "Downloading NBS 2026-05-16…",
    }

    tab.refresh_catalog()

    labels = [tab.catalog.item(row, 3).text() for row in range(tab.catalog.rowCount())]
    assert "Downloading NBS 2026-05-16…" in labels


def test_history_model_list_dispatches_to_checked_models(tmp_path):
    app()
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    controller = SimpleNamespace(
        analysis=SimpleNamespace(state=SimpleNamespace(store=store))
    )
    tab = Era5DownloadTab(
        controller, capture_region=lambda: (49.5, -126.0, 45.2, -121.6)
    )
    calls = []
    tab.submit = lambda fields=None: calls.append(("era5", fields))
    tab.submit_nbs = lambda: calls.append(("nbs", None))

    tab.model_checks["ERA5"].setChecked(False)
    tab.model_checks["NBS"].setChecked(True)
    assert tab.instructions.isHidden()
    assert tab.show_button.isHidden()
    assert not tab.field_pressure.isEnabled()
    tab.start_download()

    tab.model_checks["NBS"].setChecked(False)
    tab.model_checks["ERA5"].setChecked(True)
    assert not tab.instructions.isHidden()
    assert not tab.show_button.isHidden()
    assert tab.field_pressure.isEnabled()
    tab.start_download()

    assert calls == [("nbs", None), ("era5", ("wind", "pressure"))]


def test_history_requires_at_least_one_model_and_wind(tmp_path):
    app()
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    controller = SimpleNamespace(
        analysis=SimpleNamespace(state=SimpleNamespace(store=store))
    )
    tab = Era5DownloadTab(
        controller, capture_region=lambda: (49.5, -126.0, 45.2, -121.6)
    )
    calls = []
    tab.submit = lambda fields=None: calls.append("era5")
    tab.submit_nbs = lambda: calls.append("nbs")

    tab.model_checks["ERA5"].setChecked(False)
    tab.model_checks["NBS"].setChecked(False)
    tab.start_download()
    assert "at least one model" in tab.status.text()

    tab.model_checks["NBS"].setChecked(True)
    tab.field_wind.setChecked(False)
    tab.start_download()
    assert "Wind at 10 m is required" in tab.status.text()

    assert calls == []


def test_unset_region_is_filled_from_the_visible_map(tmp_path):
    app()
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    controller = SimpleNamespace(
        analysis=SimpleNamespace(state=SimpleNamespace(store=store))
    )
    tab = Era5DownloadTab(
        controller, capture_region=lambda: (49.54, -125.97, 45.2, -121.62)
    )

    assert not tab._valid_area()
    assert tab._ensure_area()
    assert tab._area() == (49.54, -125.97, 45.2, -121.62)
    assert "Using visible bounds" in tab.status.text()


def test_unset_region_without_capture_explains_what_to_do(tmp_path):
    app()
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    controller = SimpleNamespace(
        analysis=SimpleNamespace(state=SimpleNamespace(store=store))
    )
    tab = Era5DownloadTab(controller, capture_region=None)

    assert not tab._ensure_area()
    assert "Use visible map region" in tab.status.text()


def test_sample_selected_samples_explicit_hours_without_forecast_activation(
    tmp_path, monkeypatch
):
    app()
    from sailworks.core.grib import Grib

    calls = []
    parsed = []

    class Sampler:
        def sample(self, variable, hour, lat, lon):
            calls.append((variable, hour, lat, lon))
            if variable == "wind":
                return np.array([[3.0, 4.0]])
            return np.array([[1012.0]])

    class FakeGrib:
        def sampler(self):
            return Sampler()

        def close(self):
            pass

    monkeypatch.setattr(
        Grib, "parse", classmethod(lambda cls, path: parsed.append(path) or FakeGrib())
    )
    tab = make_tab(tmp_path)
    activated = []
    tab.controller = SimpleNamespace(
        analysis=SimpleNamespace(select_model=activated.append)
    )

    for hour in (0, 1):
        selected = datetime(2020, 1, 1, hour, tzinfo=timezone.utc)
        utc_datetime = QDateTime(
            QDate(selected.year, selected.month, selected.day),
            QTime(selected.hour, 0),
            Qt.TimeSpec.UTC,
        )
        tab.sample_start.setDateTime(utc_datetime)
        tab.sample_end.setDateTime(utc_datetime)
        tab.sample_selected()
        assert f"2020-01-01 {hour:02}:00 UTC" in tab.history_status.text()
        assert (
            "wind u=3.00, v=4.00 m/s; pressure=1012.0 hPa" in tab.history_status.text()
        )

    assert parsed == [str(tmp_path / "history.grib")] * 2
    assert [call[1] for call in calls[::2]] == [
        datetime(2020, 1, 1, hour, tzinfo=timezone.utc) for hour in (0, 1)
    ]
    assert [call[0] for call in calls] == ["wind", "pressure"] * 2
    assert all(call[2:] == (45, -120) for call in calls)
    assert activated == []
    tab.close()


def test_sample_selected_rejects_hour_outside_history_coverage(tmp_path, monkeypatch):
    app()
    from sailworks.core.grib import Grib

    parsed = []
    monkeypatch.setattr(
        Grib, "parse", classmethod(lambda cls, path: parsed.append(path))
    )
    tab = make_tab(tmp_path)
    outside = datetime(2020, 1, 1, 2, tzinfo=timezone.utc)
    tab.sample_start.setDateTime(QDateTime(outside))
    tab.sample_end.setDateTime(QDateTime(outside))

    tab.sample_selected()

    assert "outside this history artifact's UTC coverage" in tab.history_status.text()
    assert parsed == [str(tmp_path / "history.grib")]
    tab.close()
