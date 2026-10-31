from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QComboBox, QDialog, QLabel

from gweatherrouting.core.race import YBRaceError, parse_race
from gweatherrouting.qt.ybracedialog import YBRaceDialog
from gweatherrouting.qt import racepanel


def test_dialog_simplified_local_only_controls(qtbot):
    dialog = YBRaceDialog()
    qtbot.addWidget(dialog)
    assert dialog.isModal()
    assert not dialog.download_button.isEnabled()
    assert not hasattr(dialog, "airmail_button")
    assert not hasattr(dialog, "email_button")
    assert not hasattr(dialog, "format_combo")
    texts = [widget.text() for widget in dialog.findChildren(QAbstractButton)]
    texts += [widget.text() for widget in dialog.findChildren(QLabel)]
    assert not {"Archive", "Format", "Airmail", "Email"} & set(texts)
    assert not dialog.findChildren(QComboBox)
    assert not dialog.update_boat_ids.isEnabled()
    assert not dialog.update_handicaps.isEnabled()
    assert not dialog.latest.isEnabled()
    assert not dialog.since_enabled.isEnabled()
    assert not dialog.since_date.isEnabled()
    assert all(
        "direct YB download" in control.toolTip()
        for control in (
            dialog.update_boat_ids,
            dialog.update_handicaps,
            dialog.latest,
            dialog.since_enabled,
            dialog.since_date,
        )
    )
    assert (
        "Download" in dialog.unsupported_status.text()
        or "race" in dialog.unsupported_status.text().lower()
    )


def test_race_slug_and_yb_url_create_safe_link(qtbot):
    dialog = YBRaceDialog()
    qtbot.addWidget(dialog)
    dialog.race_entry.setText("https://yb.tl/abc-123")
    assert "https://yb.tl/links/abc-123" in dialog.race_link.text()
    dialog.race_entry.setText("ftp://yb.tl/abc-123")
    assert dialog.race_link.text() == "Race link: —"
    dialog.race_entry.setText("https://yb.tl.evil.test/abc")
    assert dialog.race_link.text() == "Race link: —"
    dialog.race_entry.setText("<script>")
    assert dialog.race_link.text() == "Race link: —"


def test_import_button_opens_dialog_and_cancel_stops(qtbot, monkeypatch):
    panel = racepanel.RacesPanel(SimpleNamespace(time_control=SimpleNamespace()))
    qtbot.addWidget(panel)
    calls = []

    def canceled(self):
        calls.append(self)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(racepanel.YBRaceDialog, "exec", canceled)
    monkeypatch.setattr(
        racepanel.QFileDialog,
        "getOpenFileName",
        lambda *args: (_ for _ in ()).throw(AssertionError("file picker opened")),
    )
    qtbot.mouseClick(panel.import_button, Qt.MouseButton.LeftButton)
    assert len(calls) == 1


def test_local_json_selection_and_invalid_file_warning(qtbot, monkeypatch, tmp_path):
    panel = racepanel.RacesPanel(SimpleNamespace(time_control=SimpleNamespace()))
    qtbot.addWidget(panel)
    sample = tmp_path / "race.json"
    sample.write_text(
        '{"race_id":"sample","name":"Sample race",'
        '"teams":[{"id":1,"name":"Boat"}],'
        '"positions":[{"id":1,"moments":['
        '{"at":1700000000,"lat":33.0,"lon":-117.0}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        racepanel.YBRaceDialog, "exec", lambda self: QDialog.DialogCode.Accepted
    )
    monkeypatch.setattr(
        racepanel.QFileDialog, "getOpenFileName", lambda *args: (str(sample), "")
    )
    initial_count = panel.race_list.count()
    panel.import_button.click()
    assert panel.race_list.count() == initial_count + 1
    assert panel.race_list.item(initial_count).text() == "Sample race"
    assert panel.race is not None
    assert panel.race.boat(1).positions[0].latitude == 33.0

    monkeypatch.setattr(
        racepanel,
        "parse_race",
        lambda path: (_ for _ in ()).throw(ValueError("bad JSON")),
    )
    warnings = []
    monkeypatch.setattr(
        racepanel.QMessageBox, "warning", lambda *args: warnings.append(args)
    )
    panel.import_button.click()
    assert warnings and "bad JSON" in warnings[0][2]


def test_review_from_start_sets_race_time_and_selects_race_track(qtbot):
    from datetime import datetime

    from gweatherrouting.core.timecontrol import TimeControl

    class TrackManager(list):
        active = None

        def get_unique_name(self, name):
            return name

        def set_active(self, track):
            self.active = track

    manager = TrackManager()
    controller = SimpleNamespace(
        time_control=TimeControl(), core=SimpleNamespace(trackManager=manager)
    )
    panel = racepanel.RacesPanel(controller)
    qtbot.addWidget(panel)
    race = parse_race(
        {
            "race_id": "review-test",
            "name": "Review test",
            "start_time": "2026-05-16T03:00:00Z",
            "end_time": "2026-05-16T04:00:00Z",
            "teams": [{"id": 1, "name": "Boat"}],
            "positions": [
                {
                    "id": 1,
                    "moments": [
                        {"gpsAt": "2026-05-16T03:00:00Z", "lat": 48, "lon": -123},
                        {"gpsAt": "2026-05-16T04:00:00Z", "lat": 48.1, "lon": -123.1},
                    ],
                }
            ],
        }
    )
    panel._add_race(race)

    assert manager.active is not None
    assert manager.active.name in panel._race_track_names
    assert len(manager.active.points) == 2

    panel.review_button.click()

    assert controller.time_control.time == datetime(2026, 5, 16, 3)
    assert manager.active is not None
    assert manager.active.name in panel._race_track_names
    assert len(manager.active.points) == 2


def _sample_race():
    return parse_race(
        {
            "race": "abc",
            "name": "Downloaded Race",
            "teams": [{"id": 1, "name": "Boat"}],
            "positions": [
                {
                    "id": 1,
                    "moments": [{"gpsAt": 1700000000, "lat": 33.0, "lon": -117.0}],
                }
            ],
        }
    )


def test_download_success(qtbot, monkeypatch):
    dialog = YBRaceDialog()
    qtbot.addWidget(dialog)
    race = _sample_race()

    def fake_download(slug, *args, **kwargs):
        return race

    monkeypatch.setattr("gweatherrouting.core.race.download_yb_race", fake_download)
    dialog.race_entry.setText("abc")
    with qtbot.waitSignal(dialog.download_finished, timeout=5000):
        dialog.start_download()
    assert dialog.downloaded_race is race
    assert not dialog._busy
    assert not dialog._threads


def test_download_failure(qtbot, monkeypatch):
    dialog = YBRaceDialog()
    qtbot.addWidget(dialog)

    def fake_download(slug, *args, **kwargs):
        raise YBRaceError("boom")

    monkeypatch.setattr("gweatherrouting.core.race.download_yb_race", fake_download)
    dialog.race_entry.setText("abc")
    with qtbot.waitSignal(dialog.download_failed, timeout=5000):
        dialog.start_download()
    assert dialog.downloaded_race is None
    assert "boom" in dialog.status.text()
    assert not dialog._busy
    assert not dialog._threads


def test_close_ignored_while_download_running(qtbot, monkeypatch):
    import threading

    dialog = YBRaceDialog()
    qtbot.addWidget(dialog)
    race = _sample_race()
    started = threading.Event()
    finished = threading.Event()

    def fake_download(slug, *args, **kwargs):
        started.set()
        finished.wait(5)
        return race

    monkeypatch.setattr("gweatherrouting.core.race.download_yb_race", fake_download)
    dialog.race_entry.setText("abc")
    dialog.start_download()
    assert started.wait(5)
    # Closing while busy must not destroy the running worker.
    dialog.close()
    assert dialog.isVisible()
    assert dialog._busy
    assert dialog._threads
    finished.set()
    qtbot.waitUntil(lambda: not dialog._threads, timeout=5000)
    assert not dialog._busy


def test_reject_ignored_while_download_running(qtbot, monkeypatch):
    import threading

    dialog = YBRaceDialog()
    qtbot.addWidget(dialog)
    race = _sample_race()
    started = threading.Event()
    finished = threading.Event()

    def fake_download(slug, *args, **kwargs):
        started.set()
        finished.wait(5)
        return race

    monkeypatch.setattr("gweatherrouting.core.race.download_yb_race", fake_download)
    dialog.race_entry.setText("abc")
    dialog.start_download()
    assert started.wait(5)
    dialog.reject()
    assert dialog.isVisible()
    assert dialog._busy
    finished.set()
    qtbot.waitUntil(lambda: not dialog._threads, timeout=5000)
