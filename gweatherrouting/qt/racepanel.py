"""Race review panel for imported YB Tracking races."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.race import (
    Race,
    load_saved_races,
    parse_race,
    save_race,
)

from gweatherrouting.core.geo.track import Track
from gweatherrouting.qt.ybracedialog import YBRaceDialog


class RacesPanel(QWidget):
    changed = Signal()
    race_selected = Signal(object)
    tracks_changed = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.race: Race | None = None
        self._races: list[Race] = []
        self._race_track_names: list[str] = []
        self.race_track_times: dict[str, list[float]] = {}

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

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.race_list = QListWidget()
        self.race_list.currentRowChanged.connect(self._select_race)
        splitter.addWidget(self.race_list)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        splitter.addWidget(self.details)
        splitter.setSizes([120, 240])
        layout.addWidget(splitter, 1)

        self.status = QLabel("No race imported.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._load_saved_races()

    def import_race(self):
        dialog = YBRaceDialog(self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        race = getattr(dialog, "downloaded_race", None)
        if race is not None:
            self._add_race(
                race, status=f"Downloaded {race.name}: {len(race.boats)} boats."
            )
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
        self._add_race(race, status=f"Imported {race.name}: {len(race.boats)} boats.")

    def _add_race(self, race: Race, status: str | None = None):
        self._races.append(race)
        self.race_list.addItem(race.name)
        self.race_list.setCurrentRow(len(self._races) - 1)
        if status:
            self.status.setText(status)

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
            self.details.clear()
            self.review_button.setEnabled(False)
            self.weather_button.setEnabled(False)
            return
        self.race = self._races[index]
        self._show_race(self.race)
        self._show_race_tracks(self.race)
        self.review_button.setEnabled(self.race.start_time is not None)
        self.weather_button.setEnabled(self.race.bounds() is not None)
        self.race_selected.emit(self.race)
        self.changed.emit()

    def _show_race_tracks(self, race: Race):
        track_manager = getattr(
            getattr(self.controller, "core", None), "trackManager", None
        )
        if track_manager is None:
            return
        track_manager.remove_by_names(self._race_track_names)
        self._race_track_names = []
        self.race_track_times = {}
        new_tracks = []
        for boat in race.boats:
            points = [(p.latitude, p.longitude) for p in boat.positions]
            if not points:
                continue
            name = track_manager.get_unique_name(
                f"{race.name} - {boat.name or boat.boat_id}"
            )
            track = Track(name, points, collection=track_manager, race_track=True)
            new_tracks.append(track)
            self._race_track_names.append(name)
            self.race_track_times[name] = [
                p.timestamp.replace(tzinfo=timezone.utc).timestamp()
                for p in boat.positions
            ]
        if new_tracks:
            track_manager.append_many(new_tracks)
            # Make an imported boat visible immediately, so timeline scrubbing
            # can move its marker without requiring the separate Review action.
            track_manager.set_active(new_tracks[0])
        self.tracks_changed.emit()

    def _show_race(self, race: Race):
        start, end = race.time_range()
        lines = [
            f"Race: {race.name}",
            f"ID: {race.race_id}",
            f"Start: {start.isoformat() if start else 'unknown'}",
            f"End: {end.isoformat() if end else 'unknown'}",
            f"Duration: {race.duration_hours:.1f} h",
            f"Boats: {len(race.boats)}",
            f"Course points: {sum(len(c.points) for c in race.course)}",
            "",
        ]
        for boat in race.boats:
            position = (
                boat.position_at(race.start_time or datetime.min)
                if race.start_time
                else None
            )
            suffix = ""
            if position is not None:
                suffix = f" @ {position.latitude:.4f}, {position.longitude:.4f}"
            lines.append(
                f"- {boat.name or boat.boat_id} ({len(boat.positions)} positions){suffix}"
            )
        self.details.setPlainText("\n".join(lines))

    def review_from_start(self):
        if self.race is None or self.race.start_time is None:
            return
        track_manager = getattr(
            getattr(self.controller, "core", None), "trackManager", None
        )
        if track_manager is not None:
            for track in track_manager:
                if track.name in self._race_track_names:
                    track_manager.set_active(track)
                    break
            self.tracks_changed.emit()
        self.controller.time_control.set_time(self.race.start_time)
        self.status.setText(
            f"Reviewing {self.race.name} from {self.race.start_time.isoformat()}"
        )

    def show_weather_request(self):
        if self.race is None:
            return
        request = self.race.weather_request()
        self.details.append("")
        self.details.append("Weather request:")
        self.details.append(
            json.dumps(
                {
                    key: (value.isoformat() if isinstance(value, datetime) else value)
                    for key, value in request.items()
                },
                indent=2,
                default=str,
            )
        )
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
            except Exception as exc:
                self.status.setText(
                    f"Weather request calculated; Data Library unavailable: {exc}"
                )
                return
        self.status.setText(
            "Weather request opened in Data Library; choose models and download there."
        )

    def boats_at_current_time(self):
        if self.race is None:
            return {}
        return self.race.positions_at(self.controller.time_control.time)


# Compatibility aliases.
RacePanel = RacesPanel
RacesView = RacesPanel
