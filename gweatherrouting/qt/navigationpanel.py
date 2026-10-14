"""GTK-style navigation tabs over the shared POI, track, route and AIS models."""

import math
from pathlib import Path

import dateutil.parser
import gpxpy
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.aismanager import SHIP_TYPE_COLORS
from .backgroundtask import BackgroundTask
from gweatherrouting.rendering.navigation import routing_summary


def table(headers, editable=False):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    widget.setAlternatingRowColors(True)
    widget.setShowGrid(False)
    widget.verticalHeader().hide()
    widget.verticalHeader().setDefaultSectionSize(24)
    widget.setMinimumHeight(80)
    if not editable:
        widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


def cells(widget, rows, check_column=None):
    widget.blockSignals(True)
    widget.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            if column == check_column:
                item.setText("")
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(
                    Qt.CheckState.Checked if value else Qt.CheckState.Unchecked
                )
            elif column:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            widget.setItem(row, column, item)
    widget.blockSignals(False)


def gpx_document(tracks=(), routes=(), waypoints=()):
    document = gpxpy.gpx.GPX()
    document.tracks = list(tracks)
    document.routes = list(routes)
    document.waypoints = list(waypoints)
    return document


def distance_nm(lat1, lon1, lat2, lon2):
    # Match GTK ChartStackAIS's radius and haversine, in nautical miles.
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0, 1 - a)))


class NavigationPanel(QWidget):
    changed = Signal()
    route_selected = Signal(object)
    move_requested = Signal(object)
    ais_updated = Signal(object)

    def __init__(self, controller, canvas, parent=None):
        super().__init__(parent)
        self.controller, self.core, self.canvas = controller, controller.core, canvas
        self.setMinimumWidth(280)
        self.syncing = False
        self.poi = None
        self.route = None
        self.point_index = -1
        self.ais_targets = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs)
        self.track_split = QSplitter(Qt.Orientation.Vertical)
        self.tracks = table(["Name", "POI", "Distance", "Visible"], True)
        for column, width in enumerate((140, 45, 90, 65)):
            self.tracks.setColumnWidth(column, width)
        self._compact_columns(self.tracks)
        self.track_points = table(["#", "Latitude", "Longitude"])
        self.track_split.addWidget(self.tracks)
        self.track_split.addWidget(self.track_points)
        self.track_split.setChildrenCollapsible(False)
        tracks_page = QWidget()
        box = QVBoxLayout(tracks_page)
        box.setContentsMargins(0, 4, 0, 0)
        box.addWidget(self.track_split)
        self.track_buttons = self._buttons(
            box,
            [
                ("New", self.new_track),
                ("Add Point", self.add_point_dialog),
                ("Fit", self.fit_track),
            ],
        )
        self.tabs.addTab(tracks_page, "Tracks")
        self.route_split = QSplitter(Qt.Orientation.Vertical)
        self.routes = table(["Name", "Summary", "Visible"], True)
        self.route_points = table(["#", "Time", "TWA", "TWS", "Speed", "Brg"])
        self.route_split.addWidget(self.routes)
        self.route_split.addWidget(self.route_points)
        self.route_split.setChildrenCollapsible(False)
        self.tabs.addTab(self.route_split, "Routings")
        self.pois = table(["Name", "Latitude", "Longitude", "Visible"], True)
        for column, width in enumerate((100, 80, 90, 65)):
            self.pois.setColumnWidth(column, width)
        self._compact_columns(self.pois)
        pois_page = QWidget()
        box = QVBoxLayout(pois_page)
        box.setContentsMargins(0, 4, 0, 0)
        box.addWidget(self.pois)
        self._buttons(box, [("Add POI", self.add_poi_dialog), ("Fit", self.fit_poi)])
        self.tabs.addTab(pois_page, "POIs")
        self.ais = table(["MMSI", "Name", "Type", "SOG", "COG", "Dist"])
        self.tabs.addTab(self.ais, "AIS")
        self.tabs.setTabVisible(3, False)
        self.status = QLabel("Right-click the map to add a POI or track point.")
        self.status.setWordWrap(True)
        self.status.setProperty("secondary", True)
        layout.addWidget(self.status)
        self.task = BackgroundTask(self)
        self.task.finished.connect(self._import_finished)
        self.task.changed.connect(self._task_changed)
        self.tracks.currentCellChanged.connect(self._select_track)
        self.track_points.currentCellChanged.connect(self._select_point)
        self.pois.currentCellChanged.connect(self._select_poi)
        self.routes.currentCellChanged.connect(self._select_route)
        self.route_points.currentCellChanged.connect(
            lambda row, *_: self.canvas.select_route_point(row)
        )
        self.tracks.itemChanged.connect(
            lambda item: self._edit(item, self.tracks, self.core.trackManager, 3)
        )
        self.pois.itemChanged.connect(
            lambda item: self._edit(item, self.pois, self.core.poiManager, 3)
        )
        self.routes.itemChanged.connect(
            lambda item: self._edit(item, self.routes, self.core.routingManager, 2)
        )
        self.ais.cellDoubleClicked.connect(self.fit_ais)
        self.tracks.cellDoubleClicked.connect(lambda *_: self.fit_track())
        self.pois.cellDoubleClicked.connect(lambda *_: self.fit_poi())
        self.routes.cellDoubleClicked.connect(lambda *_: self.fit_route())
        self._menu(
            self.tracks, [("Remove", self.remove_track), ("Export…", self.export_track)]
        )
        self._menu(
            self.track_points,
            [
                ("Move", self.move_point),
                ("Move Up", lambda: self.edit_point("move_up")),
                ("Move Down", lambda: self.edit_point("move_down")),
                ("Remove", lambda: self.edit_point("remove")),
                ("Duplicate", lambda: self.edit_point("duplicate")),
            ],
        )
        self._menu(self.pois, [("Move", self.move_poi), ("Remove", self.remove_poi)])
        self._menu(
            self.routes,
            [
                ("Fit", self.fit_route),
                ("Remove", self.remove_route),
                ("Export…", self.export_route),
            ],
        )
        self.refresh()
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh_ais)
        self.timer.start()
        self.refresh_ais()

    @staticmethod
    def _compact_columns(widget):
        header = widget.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, widget.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

    @staticmethod
    def _buttons(layout, actions):
        row = QHBoxLayout()
        buttons = []
        for name, callback in actions:
            button = QPushButton(name)
            button.clicked.connect(callback)
            row.addWidget(button)
            buttons.append(button)
        layout.addLayout(row)
        return buttons

    def _menu(self, widget, actions):
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        def show(position):
            item = widget.itemAt(position)
            if item is None:
                return
            widget.setCurrentItem(item)
            menu = QMenu(widget)
            for name, callback in actions:
                menu.addAction(name, callback)
            menu.exec(widget.viewport().mapToGlobal(position))

        widget.customContextMenuRequested.connect(show)

    def refresh(self):
        self.syncing = True
        active = self.core.trackManager.get_active()
        cells(
            self.tracks,
            [
                (t.name, len(t), f"{t.length():.2f} nm", t.visible)
                for t in self.core.trackManager
            ],
            3,
        )
        row = next((i for i, t in enumerate(self.core.trackManager) if t is active), -1)
        self.tracks.setCurrentCell(row, 0)
        cells(
            self.pois,
            [
                (p.name, f"{p.position[0]:.5f}", f"{p.position[1]:.5f}", p.visible)
                for p in self.core.poiManager
            ],
            3,
        )
        row = next((i for i, p in enumerate(self.core.poiManager) if p is self.poi), -1)
        self.pois.setCurrentCell(row, 0)
        cells(
            self.routes,
            [(r.name, routing_summary(r), r.visible) for r in self.core.routingManager],
            2,
        )
        row = next(
            (i for i, r in enumerate(self.core.routingManager) if r is self.route), -1
        )
        self.routes.setCurrentCell(row, 0)
        self.syncing = False
        self.refresh_points()
        self.refresh_route_points()
        self.canvas.navigation.hl_poi = (
            self.poi.name
            if self.poi is not None and self.poi in self.core.poiManager.elements
            else None
        )
        self.canvas.update()

    def _edit(self, item, widget, collection, visible_column):
        if self.syncing or not 0 <= item.row() < len(collection):
            return
        element = collection[item.row()]
        if item.column() == visible_column:
            element.visible = item.checkState() == Qt.CheckState.Checked
        elif item.column() == 0:
            name = item.text().strip()
            if name and name != element.name:
                element.name = collection.get_unique_name(name)
        collection.save()
        self.refresh()
        if collection is self.core.routingManager:
            self.route_selected.emit(element)
        self.changed.emit()

    def _select_track(self, row, *_):
        if self.syncing:
            return
        self.core.trackManager.set_active(
            self.core.trackManager[row]
            if 0 <= row < len(self.core.trackManager)
            else None
        )
        self.point_index = -1
        self.canvas.navigation.hl_track_item = None
        self.refresh_points()
        self.canvas.update()

    def refresh_points(self):
        active = self.core.trackManager.get_active()
        cells(
            self.track_points,
            [
                (i + 1, f"{p[0]:.5f}", f"{p[1]:.5f}")
                for i, p in enumerate(active.points if active is not None else [])
            ],
        )
        self.point_index = (
            self.point_index
            if active is not None and 0 <= self.point_index < len(active)
            else -1
        )
        self.track_points.blockSignals(True)
        self.track_points.setCurrentCell(self.point_index, 0)
        self.track_points.blockSignals(False)
        self.canvas.navigation.hl_track_item = self.point_index

    def _select_point(self, row, *_):
        if self.syncing:
            return
        self.point_index = row
        self.canvas.navigation.hl_track_item = row
        self.canvas.navigation.hl_poi = None
        self.canvas.update()

    def _select_poi(self, row, *_):
        if self.syncing:
            return
        self.poi = (
            self.core.poiManager[row] if 0 <= row < len(self.core.poiManager) else None
        )
        self.canvas.navigation.hl_poi = self.poi.name if self.poi is not None else None
        self.canvas.navigation.hl_track_item = None
        self.canvas.update()

    def _select_route(self, row, *_):
        if self.syncing:
            return
        self.route = (
            self.core.routingManager[row]
            if 0 <= row < len(self.core.routingManager)
            else None
        )
        self.refresh_route_points()
        self.route_selected.emit(self.route)

    def select_route(self, route):
        self.route = route
        self.refresh()

    def refresh_route_points(self):
        rows = []
        for i, p in enumerate(self.route.points if self.route is not None else []):
            try:
                stamp = dateutil.parser.parse(str(p[2]))
                stamp = stamp.astimezone() if stamp.tzinfo is not None else stamp
                label = stamp.strftime("%d/%m %H:%M")
            except (ValueError, TypeError):
                label = str(p[2])
            rows.append(
                (
                    i + 1,
                    label,
                    f"{p[3]:.0f}°" if p[3] else "",
                    f"{p[4]:.1f}" if p[4] else "",
                    f"{p[5]:.1f}" if p[5] else "",
                    f"{p[6]:.0f}°" if p[6] else "",
                )
            )
        cells(self.route_points, rows)

    def new_track(self):
        track = self.core.trackManager.new_element(points=[])
        self.core.trackManager.set_active(track)
        self.point_index = -1
        self.refresh()
        self.tabs.setCurrentIndex(0)
        self.changed.emit()
        return track

    def add_point(self, coordinates):
        active = self.core.trackManager.get_active()
        if active is None:
            if len(self.core.trackManager) > 1:
                self.status.setText("Select a track before adding a point.")
                return
            active = (
                self.core.trackManager[0]
                if len(self.core.trackManager)
                else self.new_track()
            )
            self.core.trackManager.set_active(active)
        active.add(*coordinates)
        self.point_index = len(active) - 1
        self.refresh()
        self.tabs.setCurrentIndex(0)
        self.changed.emit()

    def add_poi(self, coordinates):
        self.poi = self.core.poiManager.create(coordinates)
        self.refresh()
        self.tabs.setCurrentIndex(2)
        self.changed.emit()

    def coordinates_dialog(self, title, callback):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        form = QFormLayout(dialog)
        latitude, longitude = QDoubleSpinBox(), QDoubleSpinBox()
        for control, limit, value in (
            (latitude, 90, self.canvas._center_lat),
            (longitude, 180, self.canvas._center_lon),
        ):
            control.setRange(-limit, limit)
            control.setDecimals(6)
            control.setValue(value)
        form.addRow("Latitude", latitude)
        form.addRow("Longitude", longitude)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            callback((latitude.value(), longitude.value()))
        dialog.deleteLater()

    def add_point_dialog(self):
        self.coordinates_dialog("Add track point", self.add_point)

    def add_poi_dialog(self):
        self.coordinates_dialog("Add POI", self.add_poi)

    def remove_track(self):
        active = self.core.trackManager.get_active()
        if active is not None:
            self.core.trackManager.remove(active)
            if len(self.core.trackManager):
                self.core.trackManager.set_active(self.core.trackManager[-1])
            self.refresh()
            self.changed.emit()

    def remove_poi(self):
        if self.poi is not None and self.poi in self.core.poiManager.elements:
            self.core.poiManager.remove(self.poi)
            self.poi = None
            self.refresh()
            self.changed.emit()

    def remove_route(self):
        if self.route is not None and self.route in self.core.routingManager.elements:
            self.core.routingManager.remove(self.route)
            self.route = None
            self.refresh()
            self.route_selected.emit(None)
            self.changed.emit()

    def edit_point(self, operation):
        active = self.core.trackManager.get_active()
        if active is None or not 0 <= self.point_index < len(active):
            return
        index = self.point_index
        getattr(active, operation)(index)
        if operation == "move_up":
            self.point_index = max(0, index - 1)
        elif operation == "move_down":
            self.point_index = min(len(active) - 1, index + 1)
        self.refresh()
        self.changed.emit()

    def move_poi(self):
        poi = self.poi
        if poi is None or poi not in self.core.poiManager.elements:
            return

        def move(coordinates):
            if poi in self.core.poiManager.elements:
                self.core.poiManager.move(poi.name, *coordinates)
                self.core.poiManager.save()
                self.refresh()
                self.changed.emit()

        self.move_requested.emit(move)

    def move_point(self):
        active, index = self.core.trackManager.get_active(), self.point_index
        if active is None or not 0 <= index < len(active):
            return
        point = active.points[index]

        def move(coordinates):
            if (
                active in self.core.trackManager.elements
                and index < len(active)
                and active.points[index] is point
            ):
                active.move(index, *coordinates)
                self.refresh()
                self.changed.emit()

        self.move_requested.emit(move)

    def fit_track(self):
        active = self.core.trackManager.get_active()
        if active is not None:
            self.canvas.fit_coordinates(active.to_list())

    def fit_poi(self):
        if self.poi is not None and self.poi in self.core.poiManager.elements:
            self.canvas.fit_coordinates([self.poi.position])

    def fit_route(self):
        if self.route is not None and self.route in self.core.routingManager.elements:
            self.canvas.fit_coordinates(self.route.to_list())

    def refresh_ais(self):
        targets = self.core.ais_manager.get_active_targets()
        boat = self.core.boat_info

        def distance(target):
            return (
                distance_nm(
                    boat.latitude, boat.longitude, target.latitude, target.longitude
                )
                if boat.is_valid()
                else math.inf
            )

        self.ais_targets = sorted(
            (t for t in targets if t.has_valid_position()), key=distance
        )
        selected = self.ais.item(self.ais.currentRow(), 0)
        mmsi = selected.text() if selected else None
        cells(
            self.ais,
            [
                (
                    t.mmsi,
                    t.name or "",
                    t.category,
                    f"{t.sog:.1f}" if t.sog is not None else "",
                    f"{t.cog:.1f}" if t.cog is not None else "",
                    f"{distance(t):.1f}" if boat.is_valid() else "",
                )
                for t in self.ais_targets
            ],
        )
        for row, target in enumerate(self.ais_targets):
            self.ais.item(row, 0).setForeground(
                QColor.fromRgbF(*SHIP_TYPE_COLORS[target.category])
            )
            if str(target.mmsi) == mmsi:
                self.ais.setCurrentCell(row, 0)
        self.tabs.setTabVisible(3, bool(targets))
        self.canvas.ais_targets = tuple(self.ais_targets)
        self.ais_updated.emit(tuple(self.ais_targets))
        self.canvas.update()

    def fit_ais(self, row, *_):
        if 0 <= row < len(self.ais_targets):
            target = self.ais_targets[row]
            self.canvas.fit_coordinates([(target.latitude, target.longitude)])

    def import_gpx(self):
        if self.task.busy:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import GPX", "", "GPX files (*.gpx);;All files (*)"
        )
        if path:
            self.import_path(path)

    def import_path(self, path):
        def read(_):
            with open(path) as stream:
                gpx = gpxpy.parse(stream)
            coordinates = [
                (p.latitude, p.longitude)
                for t in gpx.tracks
                for s in t.segments
                for p in s.points
            ] + [(p.latitude, p.longitude) for p in gpx.waypoints]
            if any(
                not math.isfinite(lat)
                or not math.isfinite(lon)
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
                for lat, lon in coordinates
            ):
                raise ValueError("GPX contains invalid coordinates")
            return gpx

        self.task.start("Importing GPX…", read)

    def _import_finished(self, gpx, error):
        if error is not None:
            self.status.setText("Could not import GPX: " + str(error))
            return
        # Same GPX tracks/waypoints semantics as GTK; routes are not imported.
        for source in gpx.tracks:
            source.name = self.core.trackManager.get_unique_name(source.name or "track")
            self.core.trackManager.import_from_gpx(gpx_document(tracks=[source]))
        for source in gpx.waypoints:
            source.name = self.core.poiManager.get_unique_name(source.name or "poi")
            self.core.poiManager.import_from_gpx(gpx_document(waypoints=[source]))
        if gpx.tracks:
            self.core.trackManager.set_active(self.core.trackManager[-1])
        self.refresh()
        self.changed.emit()
        self.status.setText(
            f"Imported {len(gpx.tracks)} tracks and {len(gpx.waypoints)} POIs. "
            "GPX routes are not imported."
        )

    def _task_changed(self):
        if self.task.busy:
            self.status.setText(self.task.message)

    def _save(self, title, name, text, file_filter, suffix):
        path, _ = QFileDialog.getSaveFileName(self, title, name, file_filter)
        if not path:
            return
        if not Path(path).suffix:
            path += suffix
        try:
            Path(path).write_text(text, encoding="utf-8")
            self.status.setText(f"Exported to {path}")
        except OSError as error:
            self.status.setText(f"Could not export: {error}")

    def export_track(self):
        track = self.core.trackManager.get_active()
        if track is not None:
            self._save(
                "Export track",
                track.name + ".gpx",
                gpx_document(tracks=[track.to_gpx_object()]).to_xml(),
                "GPX files (*.gpx)",
                ".gpx",
            )

    def export_route(self):
        if self.route is not None:
            self._save(
                "Export routing",
                self.route.name + ".gpx",
                gpx_document(routes=[self.route.to_gpx_object()]).to_xml(),
                "GPX files (*.gpx)",
                ".gpx",
            )

    def export_all(self):
        gpx = gpxpy.gpx.GPX()
        gpx.tracks = [t.to_gpx_object() for t in self.core.trackManager]
        gpx.routes = [r.to_gpx_object() for r in self.core.routingManager]
        gpx.waypoints = [p.to_gpx_object() for p in self.core.poiManager]
        self._save(
            "Export GPX", "navigation.gpx", gpx.to_xml(), "GPX files (*.gpx)", ".gpx"
        )

    def export_pfec(self):
        self._save(
            "Export POIs as NMEA PFEC",
            "pois.nmea",
            self.core.poiManager.to_nmea_fpec(),
            "NMEA files (*.nmea)",
            ".nmea",
        )

    def dispose(self):
        self.timer.stop()
        self.task.close()
