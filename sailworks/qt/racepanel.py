"""Race review panel for imported YB Tracking races."""

from __future__ import annotations

import random
from datetime import timezone

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sailworks.core.race import (
    Race,
    RaceTrack,
    load_saved_races,
    parse_race,
    race_file_path,
)
from sailworks.core.timecontrol import utc_to_wall, wall_to_utc

from sailworks.qt.ybracedialog import YBRaceDialog


def _hex_colour(value) -> tuple[float, float, float] | None:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return (
            int(text[0:2], 16) / 255.0,
            int(text[2:4], 16) / 255.0,
            int(text[4:6], 16) / 255.0,
        )
    except ValueError:
        return None


def _boat_colour(boat) -> tuple[float, float, float]:
    """Use the YB team colour when available, else a random colour."""
    return _hex_colour(getattr(boat, "colour", "")) or (
        random.random(),
        random.random(),
        random.random(),
    )


class RacesPanel(QWidget):
    changed = Signal()
    race_selected = Signal(object)
    tracks_changed = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.race: Race | None = None
        self._races: list[Race] = []
        self._displayed_race_id: str | None = None
        self._explicit_race_ids: set[str] = set()
        self._race_tracks: dict[str, list[RaceTrack]] = {}
        self.active_race_track: RaceTrack | None = None
        self._syncing_boats = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        buttons = QHBoxLayout()
        self.import_button = QPushButton("Import race…")
        self.import_button.clicked.connect(self.import_race)
        buttons.addWidget(self.import_button)
        self.review_button = QPushButton("Review from start")
        self.review_button.setEnabled(False)
        self.review_button.clicked.connect(self.review_from_start)
        buttons.addWidget(self.review_button)
        self.weather_button = QPushButton("Weather request")
        self.weather_button.setEnabled(False)
        self.weather_button.clicked.connect(self.show_weather_request)
        buttons.addWidget(self.weather_button)
        layout.addLayout(buttons)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.race_list = QListWidget()
        self.race_list.currentRowChanged.connect(self._select_race)
        self.race_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.race_list.customContextMenuRequested.connect(self._race_context_menu)
        self.tabs.addTab(self.race_list, "Races")

        boats_page = QWidget()
        boats_layout = QVBoxLayout(boats_page)
        boats_layout.setContentsMargins(0, 0, 0, 0)
        self.boat_table = QTableWidget(0, 9)
        self.boat_table.setHorizontalHeaderLabels(
            [
                "Boat",
                "Sail",
                "Model",
                "Class",
                "Country",
                "Flag",
                "Colour",
                "TCF",
                "Status",
            ]
        )
        self.boat_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.boat_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.boat_table.setAlternatingRowColors(True)
        self.boat_table.setShowGrid(False)
        self.boat_table.verticalHeader().hide()
        self.boat_table.verticalHeader().setDefaultSectionSize(24)
        self.boat_table.setMinimumHeight(80)
        self.boat_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.boat_table.setSortingEnabled(True)
        self.boat_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.boat_table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((150, 60, 110, 80, 110, 50, 80, 60, 80)):
            self.boat_table.setColumnWidth(column, width)
        self.boat_table.itemChanged.connect(self._boat_visibility_changed)
        self.boat_table.currentCellChanged.connect(self._boat_selected)
        boats_layout.addWidget(self.boat_table, 1)
        self.tabs.addTab(boats_page, "Boats")
        layout.addWidget(self.tabs, 1)

        self._defer_heavy = True
        self._purge_legacy_race_tracks()
        self._load_saved_races()
        self.refresh_boats()

    def import_race(self):
        dialog = YBRaceDialog(self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        race = getattr(dialog, "downloaded_race", None)
        if race is not None:
            self._add_race(race)
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import local race JSON", "", "Race data (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            race = parse_race(path)
        except Exception as exc:
            QMessageBox.warning(self, "Could not import race", str(exc))
            return
        self._add_race(race)

    def _add_race(self, race: Race):
        self._races.append(race)
        self.race_list.addItem(race.name)
        self.race_list.setCurrentRow(len(self._races) - 1)

    def _load_saved_races(self):
        try:
            races = load_saved_races()
        except Exception:
            races = []
        for race in races:
            # Avoid duplicates by race_id.
            if any(existing.race_id == race.race_id for existing in self._races):
                continue
            self._races.append(race)
            self.race_list.addItem(race.name)
        if self._races and self.race_list.currentRow() < 0:
            self.race_list.setCurrentRow(0)

    def _select_race(self, index):
        if index < 0 or index >= len(self._races):
            self.race = None
            self.review_button.setEnabled(False)
            self.weather_button.setEnabled(False)
            self.refresh_boats()
            self.race_selected.emit(None)
            return
        self.race = self._races[index]
        self._show_race_tracks(self.race)
        self.refresh_boats()
        self.review_button.setEnabled(self.race.start_time is not None)
        self.weather_button.setEnabled(self.race.bounds() is not None)
        self.race_selected.emit(self.race)
        self.changed.emit()

    def _show_race_tracks(self, race: Race):
        if getattr(self, "_defer_heavy", False):
            self._defer_heavy = False
            selected = self.race is race
            QTimer.singleShot(
                0,
                lambda r=race: (
                    self._show_race_tracks(r)
                    if not selected or self.race is r
                    else None
                ),
            )
            return
        if self._displayed_race_id and self._displayed_race_id != race.race_id:
            previous = next(
                (r for r in self._races if r.race_id == self._displayed_race_id),
                None,
            )
            # Tracks the user loaded explicitly from the context menu stay
            # loaded when they select another race; only the transient tracks
            # belonging to the previously displayed race are replaced.
            if previous is not None and previous.race_id not in self._explicit_race_ids:
                self._unload_race_tracks(previous, emit=False)
        self._displayed_race_id = race.race_id
        self._load_race_tracks(race, select_first=True, emit=False)
        self.refresh_boats()
        self.tracks_changed.emit()

    def race_tracks(self, race: Race | None) -> list[RaceTrack]:
        if race is None:
            return []
        return list(self._race_tracks.get(race.race_id, ()))

    def loaded_race_tracks(self) -> dict[str, list[RaceTrack]]:
        return self._race_tracks

    def track_for_boat(self, race: Race, boat_id) -> RaceTrack | None:
        return next(
            (track for track in self.race_tracks(race) if track.boat_id == boat_id),
            None,
        )

    def _make_race_track(self, race: Race, boat) -> RaceTrack | None:
        points = [(p.latitude, p.longitude) for p in boat.positions]
        if not points:
            return None
        return RaceTrack(
            boat_id=boat.boat_id,
            name=str(boat.name or boat.boat_id),
            race_id=race.race_id,
            points=points,
            timestamps=[
                p.timestamp.replace(tzinfo=timezone.utc).timestamp()
                for p in boat.positions
            ],
            color=_boat_colour(boat),
        )

    def _load_race_tracks(self, race: Race, *, select_first=False, emit=True) -> int:
        if self._race_tracks.get(race.race_id):
            return 0
        tracks = [
            track
            for boat in race.boats
            for track in [self._make_race_track(race, boat)]
            if track is not None
        ]
        if not tracks:
            return 0
        self._race_tracks[race.race_id] = tracks
        if select_first:
            self.active_race_track = tracks[0]
        if emit:
            self.tracks_changed.emit()
        return len(tracks)

    def _unload_race_tracks(self, race: Race, *, emit=True) -> int:
        tracks = self._race_tracks.pop(race.race_id, None)
        if not tracks:
            return 0
        if self.active_race_track in tracks:
            self.active_race_track = None
        if emit:
            self.tracks_changed.emit()
        return len(tracks)

    def load_race_tracks(self, race: Race) -> int:
        added = self._load_race_tracks(race, select_first=True)
        if added:
            self._explicit_race_ids.add(race.race_id)
        self.refresh_boats()
        return added

    def unload_race_tracks(self, race: Race) -> int:
        self._explicit_race_ids.discard(race.race_id)
        removed = self._unload_race_tracks(race)
        self.refresh_boats()
        return removed

    def set_race_track_visible(self, race: Race, boat_id, visible: bool) -> bool:
        track = self.track_for_boat(race, boat_id)
        if track is None:
            if not visible:
                return False
            return self._ensure_boat_track(race, boat_id) is not None
        track.visible = bool(visible)
        if not track.visible and self.active_race_track is track:
            self.active_race_track = None
        self.tracks_changed.emit()
        return True

    def _ensure_boat_track(self, race: Race, boat_id) -> RaceTrack | None:
        for track in self.race_tracks(race):
            if track.boat_id == boat_id:
                track.visible = True
                self.tracks_changed.emit()
                return track
        boat = race.boat(boat_id)
        if boat is None:
            return None
        track = self._make_race_track(race, boat)
        if track is None:
            return None
        self._race_tracks.setdefault(race.race_id, []).append(track)
        self.tracks_changed.emit()
        return track

    def refresh_boats(self):
        self._syncing_boats = True
        self.boat_table.setSortingEnabled(False)
        self.boat_table.setRowCount(0)
        race = self.race
        tracks = {track.boat_id: track for track in self.race_tracks(race)}
        active_boat_id = None
        if race is not None:
            for boat in race.boats:
                row = self.boat_table.rowCount()
                self.boat_table.insertRow(row)
                track = tracks.get(boat.boat_id)
                name_item = QTableWidgetItem(str(boat.name or boat.boat_id))
                name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                name_item.setCheckState(
                    Qt.CheckState.Checked
                    if track is not None and track.visible
                    else Qt.CheckState.Unchecked
                )
                name_item.setData(Qt.ItemDataRole.UserRole, boat.boat_id)
                self.boat_table.setItem(row, 0, name_item)
                values = (
                    getattr(boat, "sail", ""),
                    getattr(boat, "model", ""),
                    getattr(boat, "boat_type", ""),
                    getattr(boat, "country", ""),
                    getattr(boat, "flag", ""),
                )
                for column, value in enumerate(values, start=1):
                    self.boat_table.setItem(
                        row, column, QTableWidgetItem(str(value or ""))
                    )
                colour = str(getattr(boat, "colour", "") or "")
                colour_item = QTableWidgetItem(colour)
                rgb = _hex_colour(colour)
                if rgb is not None:
                    colour_item.setBackground(QBrush(QColor.fromRgbF(*rgb)))
                    luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
                    colour_item.setForeground(
                        QBrush(QColor("black" if luminance > 0.6 else "white"))
                    )
                self.boat_table.setItem(row, 6, colour_item)
                tcf_item = QTableWidgetItem(str(getattr(boat, "tcf1", "") or ""))
                tcf_item.setToolTip(
                    "TCF1 {0}  TCF2 {1}  TCF3 {2}".format(
                        getattr(boat, "tcf1", ""),
                        getattr(boat, "tcf2", ""),
                        getattr(boat, "tcf3", ""),
                    )
                )
                self.boat_table.setItem(row, 7, tcf_item)
                self.boat_table.setItem(
                    row, 8, QTableWidgetItem(str(getattr(boat, "status", "") or ""))
                )
                if track is not None and track is self.active_race_track:
                    active_boat_id = boat.boat_id
        self.boat_table.setSortingEnabled(True)
        self._syncing_boats = False
        if active_boat_id is not None:
            for row in range(self.boat_table.rowCount()):
                item = self.boat_table.item(row, 0)
                if (
                    item is not None
                    and item.data(Qt.ItemDataRole.UserRole) == active_boat_id
                ):
                    self.boat_table.selectRow(row)
                    break

    def _boat_visibility_changed(self, item):
        if self._syncing_boats or self.race is None or item.column() != 0:
            return
        boat_id = item.data(Qt.ItemDataRole.UserRole)
        self.set_race_track_visible(
            self.race, boat_id, item.checkState() == Qt.CheckState.Checked
        )

    def _boat_selected(self, row, column, previous_row, previous_column):
        if self._syncing_boats or self.race is None or row < 0:
            return
        item = self.boat_table.item(row, 0)
        if item is None:
            return
        track = self.track_for_boat(self.race, item.data(Qt.ItemDataRole.UserRole))
        if track is None or not track.visible:
            return
        self.active_race_track = track
        self.tracks_changed.emit()

    def _race_context_menu(self, position):
        item = self.race_list.itemAt(position)
        if item is None:
            return
        row = self.race_list.row(item)
        if not 0 <= row < len(self._races):
            return
        race = self._races[row]
        loaded = bool(self.race_tracks(race))
        menu = QMenu(self.race_list)
        load = menu.addAction("Load tracks")
        load.setEnabled(not loaded)
        load.triggered.connect(lambda: self.load_race_tracks(race))
        unload = menu.addAction("Unload tracks")
        unload.setEnabled(loaded)
        unload.triggered.connect(lambda: self.unload_race_tracks(race))
        menu.addSeparator()
        delete = menu.addAction("Delete race")
        delete.triggered.connect(lambda: self._confirm_delete_race(race))
        menu.exec(self.race_list.viewport().mapToGlobal(position))

    def _confirm_delete_race(self, race: Race):
        answer = QMessageBox.question(
            self,
            "Delete race",
            f"Delete '{race.name}'?\n\nIts saved data and loaded tracks are removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_race(race)

    def delete_race(self, race: Race) -> bool:
        """Remove a race from the list, unload its tracks and delete its file."""
        index = next(
            (i for i, candidate in enumerate(self._races) if candidate is race), None
        )
        if index is None:
            return False
        was_displayed = self._displayed_race_id == race.race_id
        self._unload_race_tracks(race, emit=False)
        self._explicit_race_ids.discard(race.race_id)
        if was_displayed:
            self._displayed_race_id = None
        self._races.pop(index)
        self.race_list.takeItem(index)
        try:
            path = race_file_path(race.race_id)
            if path.exists():
                path.unlink()
        except OSError:
            pass
        self.refresh_boats()
        self.tracks_changed.emit()
        self.changed.emit()
        return True

    def _purge_legacy_race_tracks(self):
        """Drop race tracks persisted by older versions from the user collection."""
        manager = getattr(getattr(self.controller, "core", None), "trackManager", None)
        if manager is None:
            return
        legacy = [
            track for track in manager if getattr(track, "_legacy_race_track", False)
        ]
        if not legacy:
            return
        remove = getattr(manager, "remove_by_names", None)
        if not callable(remove):
            return
        remove([track.name for track in legacy], save=False)
        save = getattr(manager, "save", None)
        if callable(save):
            save()

    def review_from_start(self):
        if self.race is None or self.race.start_time is None:
            return
        # Re-apply the race's timeline range before moving the clock. The
        # scrubber may still show the forecast range (for example after
        # "Now"), which would clamp the race start out of view.
        self.race_selected.emit(self.race)
        if not self.race_tracks(self.race):
            self._load_race_tracks(self.race, select_first=True)
        else:
            track = next((t for t in self.race_tracks(self.race) if t.visible), None)
            if track is not None:
                self.active_race_track = track
        self.refresh_boats()
        self.tracks_changed.emit()
        self.controller.time_control.set_time(utc_to_wall(self.race.start_time))

    def show_weather_request(self):
        if self.race is None:
            return
        request = self.race.weather_request()
        window = self.window()
        if window is not None and hasattr(window, "_ensure_library"):
            try:
                window._ensure_library()
                dialog = getattr(window, "library_dialog", None)
                options = (
                    getattr(dialog, "forecast_options", None)
                    if dialog is not None
                    else None
                )
                if options is not None and request.get("region") is not None:
                    options.region = request["region"]
                    if hasattr(options, "region_status"):
                        options.region_status.setText("Using race bounds")
                    if hasattr(options, "_schedule_size_estimate"):
                        options._schedule_size_estimate()
                if dialog is not None:
                    dialog.show()
                    dialog.raise_()
                    dialog.activateWindow()
            except Exception:
                return

    def boats_at_current_time(self):
        if self.race is None:
            return {}
        return self.race.positions_at(wall_to_utc(self.controller.time_control.time))


# Compatibility aliases.
RacePanel = RacesPanel
RacesView = RacesPanel
