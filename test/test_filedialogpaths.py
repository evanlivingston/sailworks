"""File-dialog folder memory without opening a real dialog."""

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QDialog

from sailworks.qt import navigationio, racepanel
from sailworks.qt.filedialogpaths import remembered_dir, remember_dir


class Store(dict):
    """Settings stand-in with attribute access like SettingsManager."""

    def __getattr__(self, name):
        return self.get(name)

    def __setattr__(self, name, value):
        self[name] = value


class UnwritableStore(Store):
    def __setattr__(self, name, value):
        raise OSError("read-only settings")


def test_empty_settings_has_no_directory():
    assert remembered_dir(Store(), "logs") == ""
    assert remembered_dir({}, "logs") == ""
    assert remembered_dir(None, "logs") == ""


def test_directory_round_trip(tmp_path):
    store = Store()
    remember_dir(store, "logs", str(tmp_path))
    assert remembered_dir(store, "logs") == str(tmp_path)


def test_file_path_stores_its_parent(tmp_path):
    source = tmp_path / "track.gpx"
    source.write_text("<gpx></gpx>", encoding="utf-8")
    store = Store()
    remember_dir(store, "gpx", source)
    assert store.lastUsedDirectories == {"gpx": str(tmp_path)}


def test_plain_dict_settings_are_supported(tmp_path):
    settings = {}
    remember_dir(settings, "polars", str(tmp_path))
    assert settings["lastUsedDirectories"] == {"polars": str(tmp_path)}
    assert remembered_dir(settings, "polars") == str(tmp_path)


def test_nonexistent_stored_directory_is_ignored(tmp_path):
    store = Store(lastUsedDirectories={"logs": str(tmp_path / "gone")})
    assert remembered_dir(store, "logs") == ""


def test_save_failure_is_swallowed(tmp_path):
    remember_dir(UnwritableStore(), "logs", str(tmp_path))


def test_unchanged_keys_are_preserved(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = Store(lastUsedDirectories={"polars": str(first)})
    remember_dir(store, "logs", second)
    assert store.lastUsedDirectories == {
        "polars": str(first),
        "logs": str(second),
    }


def test_import_gpx_uses_and_updates_remembered_directory(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    source = second / "track.gpx"
    source.write_text("<gpx></gpx>", encoding="utf-8")
    store = Store(lastUsedDirectories={"gpx": str(first)})
    window = type("Window", (), {"controller": type("C", (), {"settings": store})()})()
    seen = []

    def picker(*args):
        seen.append(args[2])
        return str(source), ""

    monkeypatch.setattr(navigationio.QFileDialog, "getOpenFileName", picker)
    monkeypatch.setattr(navigationio, "import_path", lambda window, path: path)

    assert navigationio.import_gpx(window) == str(source)
    assert seen == [str(first)]
    assert store.lastUsedDirectories["gpx"] == str(second)


def test_save_gpx_keeps_name_and_remembers_directory(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = Store(lastUsedDirectories={"gpx": str(first)})
    window = type("Window", (), {"controller": type("C", (), {"settings": store})()})()
    seen = []

    def picker(*args):
        seen.append(args[2])
        return str(second / "route.gpx"), ""

    monkeypatch.setattr(navigationio.QFileDialog, "getSaveFileName", picker)

    navigationio._save(window, "Export GPX", "route.gpx", "data", "GPX", ".gpx")
    assert seen == [str(first / "route.gpx")]
    assert (second / "route.gpx").read_text(encoding="utf-8") == "data"
    assert store.lastUsedDirectories["gpx"] == str(second)


def test_race_panel_passes_and_updates_remembered_directory(
    qtbot, monkeypatch, tmp_path
):
    race_folder = tmp_path / "races"
    race_folder.mkdir()
    sample = tmp_path / "race.json"
    sample.write_text(
        '{"race_id":"sample","name":"Sample race",'
        '"teams":[{"id":1,"name":"Boat"}],'
        '"positions":[{"id":1,"moments":['
        '{"at":1700000000,"lat":33.0,"lon":-117.0}]}]}',
        encoding="utf-8",
    )
    store = Store(lastUsedDirectories={"races": str(race_folder)})
    controller = type("C", (), {"settings": store, "time_control": object()})()
    panel = racepanel.RacesPanel(controller)
    qtbot.addWidget(panel)
    seen = []

    monkeypatch.setattr(
        racepanel.YBRaceDialog, "exec", lambda self: QDialog.DialogCode.Accepted
    )

    def picker(*args):
        seen.append(args[2])
        return str(sample), ""

    monkeypatch.setattr(racepanel.QFileDialog, "getOpenFileName", picker)

    panel.import_race()
    assert seen == [str(race_folder)]
    assert store.lastUsedDirectories["races"] == str(tmp_path)
