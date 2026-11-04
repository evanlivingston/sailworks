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
    tab.catalog.selectRow(0)
    tab.nbs_job = None
    return tab


def test_wave_only_selection(tmp_path):
    app()
    from sailworks.core.tranchestore import TrancheStore

    controller = SimpleNamespace(
        analysis=SimpleNamespace(state=SimpleNamespace(store=TrancheStore(tmp_path)))
    )
    tab = Era5DownloadTab(controller)
    assert tab._selected_fields() == ("wind", "pressure")
    assert not tab.field_wave_height.isChecked()
    assert not tab.field_wave_period.isChecked()
    assert not tab.field_wave_direction.isChecked()
    tab.model_checks["NBS"].setChecked(False)
    tab.field_wind.setChecked(False)
    tab.field_pressure.setChecked(False)
    for check in (
        tab.field_wave_height,
        tab.field_wave_period,
        tab.field_wave_direction,
    ):
        check.setChecked(True)
    assert tab._selected_fields() == ("wave_height", "wave_period", "wave_direction")


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
    tab._range = (
        datetime(2026, 5, 14, tzinfo=timezone.utc),
        datetime(2026, 5, 15, tzinfo=timezone.utc),
    )

    tab.model_checks["ERA5"].setChecked(False)
    tab.model_checks["NBS"].setChecked(True)
    assert not tab.field_pressure.isEnabled()
    tab.start_download()

    tab.model_checks["NBS"].setChecked(False)
    tab.model_checks["ERA5"].setChecked(True)
    assert tab.field_pressure.isEnabled()
    tab.start_download()

    assert calls == [("nbs", None), ("era5", ("wind", "pressure"))]


def test_failed_download_completion_consumes_job_error(tmp_path, monkeypatch):
    app()
    from sailworks.core.era5 import job_key
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    controller = SimpleNamespace(
        analysis=SimpleNamespace(state=SimpleNamespace(store=store))
    )
    tab = Era5DownloadTab(
        controller, capture_region=lambda: (49.5, -126.0, 45.2, -121.6)
    )
    tab.model_checks["NBS"].setChecked(False)
    tab._range = (
        datetime(2026, 5, 14, tzinfo=timezone.utc),
        datetime(2026, 5, 15, tzinfo=timezone.utc),
    )

    def fail_download(*args, **kwargs):
        raise RuntimeError("CDS download failed")

    monkeypatch.setattr(tab.service, "_run", fail_download)
    tab.start_download()
    assert tab.job is not None
    tab.job.join(timeout=5)
    assert not tab.job.is_alive()
    key = job_key(tab.plan)
    error = tab.service.error_for(key)
    assert "ERA5 download failed (RuntimeError)" in error

    tab.poll()
    assert tab.status.text() == error
    assert "request complete" not in tab.status.text()
    assert tab.service.error_for(key) is None
    assert tab.job is None

    tab.status.setText("After completion")
    tab.poll()
    assert tab.status.text() == "After completion"


def test_attached_job_with_different_key_reports_and_consumes_failure(
    tmp_path, monkeypatch
):
    import threading

    from sailworks.core.era5 import job_key, plan_request
    from sailworks.core.tranchestore import TrancheStore

    app()
    store = TrancheStore(tmp_path)
    controller = SimpleNamespace(
        analysis=SimpleNamespace(state=SimpleNamespace(store=store))
    )
    tab = Era5DownloadTab(controller, capture_region=lambda: (50, -10, 40, 5))
    start = datetime(2026, 5, 14, tzinfo=timezone.utc)
    original = plan_request(start, start, (50.004999, -10, 40, 5))
    attached = plan_request(start, start, (50.005001, -10, 40, 5))
    assert job_key(original) != job_key(attached)
    release = threading.Event()
    started = threading.Event()

    def fail_download(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        raise RuntimeError("attached CDS download failed")

    monkeypatch.setattr(tab.service, "_run", fail_download)
    job = tab.service.submit(original)
    try:
        assert started.wait(timeout=5)
        monkeypatch.setattr(tab, "_make_plan", lambda fields: attached)
        tab.submit()
        assert tab.job is job
    finally:
        release.set()
        job.join(timeout=5)
    assert not job.is_alive()
    error = tab.service.error_for(job.era5_key)
    assert "ERA5 download failed (RuntimeError)" in error

    tab.poll()
    assert tab.status.text() == error
    assert tab.service.error_for(job.era5_key) is None
    assert tab.job is None
    tab.status.setText("After completion")
    tab.poll()
    assert tab.status.text() == "After completion"


def test_failed_resume_completion_consumes_job_error(tmp_path):
    app()
    from sailworks.core.era5 import Era5DownloadService, job_key, plan_request
    from PySide6.QtWidgets import QLabel, QProgressBar

    tab = make_tab(tmp_path)
    tab.service = Era5DownloadService(SimpleNamespace(root=tmp_path))
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    request = plan_request(start, start, (50, -10, 40, 5))
    key = job_key(request)
    tab.service.errors[key] = "resume failed"
    tab.service._completed_resumes.append(key)
    tab.job = None
    tab.progress = QProgressBar()
    tab.status = QLabel()
    tab._poll_count = 0
    tab._resume_was_active = True
    tab.refresh_catalog = lambda: None
    tab._publish_history_display = lambda: (_ for _ in ()).throw(AssertionError())
    tab.poll()
    assert "resume failed" in tab.status.text()
    assert tab.service.error_for(key) is None
    tab.status.setText("After completion")
    tab.poll()
    assert tab.status.text() == "After completion"


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
    tab._range = (
        datetime(2026, 5, 14, tzinfo=timezone.utc),
        datetime(2026, 5, 15, tzinfo=timezone.utc),
    )

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
    assert "Using visible region" in tab.region_status.text()


def test_unset_region_without_capture_explains_what_to_do(tmp_path):
    app()
    from sailworks.core.tranchestore import TrancheStore

    store = TrancheStore(tmp_path)
    controller = SimpleNamespace(
        analysis=SimpleNamespace(state=SimpleNamespace(store=store))
    )
    tab = Era5DownloadTab(controller, capture_region=None)

    assert not tab._ensure_area()
    assert "Update region from current map" in tab.status.text()
