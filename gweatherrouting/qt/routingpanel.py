"""A complete wind-only route setup, calculation and result view."""

from pathlib import Path
from datetime import datetime
from typing import cast

import weatherrouting
import dateutil.parser
from PySide6.QtCore import QDateTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QScrollArea,
    QFrame,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.routingjob import RouteRequest
from .theme import section_label


class RoutingPanel(QWidget):
    plan_changed = Signal(object)
    route_selected = Signal(object)
    fit_requested = Signal()

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.routing = controller.routing
        self.setMinimumWidth(330)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        self.pages = QTabWidget()
        self.pages.setDocumentMode(True)
        setup_page = QWidget()
        setup_layout = QVBoxLayout(setup_page)
        setup_layout.setContentsMargins(0, 4, 0, 4)
        setup_layout.setSpacing(8)
        self.results_page = QWidget()
        results_layout = QVBoxLayout(self.results_page)
        results_layout.setContentsMargins(0, 4, 0, 4)
        results_layout.setSpacing(8)
        setup_scroll = QScrollArea()
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setFrameShape(QFrame.Shape.NoFrame)
        setup_scroll.setWidget(setup_page)
        self.pages.addTab(setup_scroll, "Plan")
        self.pages.addTab(self.results_page, "Results")
        layout.addWidget(self.pages, 1)
        setup_layout.addWidget(section_label("Route setup"))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.polar = QComboBox()
        self.polar.addItems(controller.core.polar_manager.polars)
        form.addRow("Polar", self.polar)
        self.manage_polars = QPushButton("Manage polars…")
        form.addRow(self.manage_polars)
        self.algorithm = QComboBox()
        for entry in weatherrouting.list_routing_algorithms():
            label = (
                "Isochrone" if entry["name"] == "LinearBestIsoRouter" else entry["name"]
            )
            self.algorithm.addItem(label, entry["class"])
        form.addRow("Algorithm", self.algorithm)
        self.departure = QDateTimeEdit(QDateTime(controller.time_control.time))
        self.departure.setCalendarPopup(True)
        self.departure.setDisplayFormat("yyyy-MM-dd HH:mm")
        form.addRow("Departure (local)", self.departure)
        self.start_lat, self.start_lon, row = self._coordinates(48.4, -123.4)
        form.addRow("Start lat / lon", row)
        self.end_lat, self.end_lon, row = self._coordinates(48.5, -123.5)
        form.addRow("Destination", row)
        self.selecting_destination = False
        self.destination = QComboBox()
        self.destination.addItem("Custom coordinates", None)
        for poi in controller.core.poiManager:
            self.destination.addItem(poi.name, tuple(poi.position))
        self.destination.currentIndexChanged.connect(self._destination_changed)
        form.addRow("Saved POI", self.destination)
        self.use_boat = QPushButton("Use boat position as start")
        self.use_boat.clicked.connect(self._use_boat)
        form.addRow(self.use_boat)
        self.coastlines = QCheckBox("Check coastlines")
        self.coastlines.setChecked(True)
        form.addRow(self.coastlines)
        setup_layout.addLayout(form)
        hint = QLabel("Right-click the map to choose a start or destination.")
        hint.setWordWrap(True)
        hint.setProperty("secondary", True)
        setup_layout.addWidget(hint)
        setup_layout.addStretch()
        actions = QHBoxLayout()
        self.calculate = QPushButton("Calculate route")
        self.calculate.setProperty("primary", True)
        self.calculate.clicked.connect(self._calculate)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.routing.cancel)
        actions.addWidget(self.calculate)
        actions.addWidget(self.cancel_button)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel(self.routing.message)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        results_layout.addWidget(section_label("Saved routes"))
        self.routes = QComboBox()
        self.routes.currentIndexChanged.connect(self._select_route)
        results_layout.addWidget(self.routes)
        self.summary = QLabel("No calculated route selected")
        self.summary.setWordWrap(True)
        results_layout.addWidget(self.summary)
        self.fit = QPushButton("Fit route")
        self.fit.clicked.connect(self.fit_requested.emit)
        results_layout.addWidget(self.fit)
        results_layout.addWidget(section_label("Waypoints"))
        self.waypoints = QTableWidget(0, 4)
        self.waypoints.setMinimumHeight(120)
        self.waypoints.setAlternatingRowColors(True)
        self.waypoints.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.waypoints.setHorizontalHeaderLabels(
            ["Latitude", "Longitude", "Time", "kn"]
        )
        speed_header = self.waypoints.horizontalHeaderItem(3)
        if speed_header is not None:
            speed_header.setToolTip("Boat speed in knots")
        self.waypoints.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.waypoints.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        results_layout.addWidget(self.waypoints, 1)
        layout.addLayout(actions)
        for combo in (self.polar, self.algorithm, self.destination, self.routes):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(14)
        self.inputs = (
            self.polar,
            self.algorithm,
            self.departure,
            self.start_lat,
            self.start_lon,
            self.end_lat,
            self.end_lon,
            self.destination,
            self.coastlines,
        )
        for control in (self.start_lat, self.start_lon, self.end_lat, self.end_lon):
            control.valueChanged.connect(self._plan_changed)
        for control in (self.end_lat, self.end_lon):
            control.valueChanged.connect(self._custom_destination)
        self.routing.changed.connect(self.refresh)
        self.routing.completed.connect(self._completed)
        controller.boat_changed.connect(lambda _: self.refresh())
        self.refresh_routes()
        self.refresh()

    def refresh_polars(self):
        names = list(self.controller.core.polar_manager.polars)
        if names == [self.polar.itemText(i) for i in range(self.polar.count())]:
            return
        selected = self.polar.currentText()
        self.polar.blockSignals(True)
        self.polar.clear()
        self.polar.addItems(names)
        index = self.polar.findText(selected)
        self.polar.setCurrentIndex(index if index >= 0 else (0 if names else -1))
        self.polar.blockSignals(False)
        self.polar.currentIndexChanged.emit(self.polar.currentIndex())
        self.refresh()

    @staticmethod
    def _coordinates(lat, lon):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = []
        for label, limit, value in (("Latitude", 89.9, lat), ("Longitude", 180, lon)):
            control = QDoubleSpinBox()
            control.setRange(-limit, limit)
            control.setDecimals(5)
            control.setSingleStep(0.01)
            control.setValue(value)
            control.setAccessibleName(label)
            controls.append(control)
            layout.addWidget(control)
        return *controls, row

    def _plan_changed(self, *_):
        self.plan_changed.emit(
            (
                (self.start_lat.value(), self.start_lon.value()),
                (self.end_lat.value(), self.end_lon.value()),
            )
        )

    def set_location(self, which, coordinates):
        if self.routing.busy:
            return
        controls = (
            (self.start_lat, self.start_lon)
            if which == "start"
            else (self.end_lat, self.end_lon)
        )
        if which == "destination":
            self.destination.setCurrentIndex(0)
        for control, value in zip(controls, coordinates):
            control.setValue(value)
        self._plan_changed()

    def _custom_destination(self, _):
        if not self.selecting_destination:
            self.destination.setCurrentIndex(0)

    def _destination_changed(self, _):
        coordinates = self.destination.currentData()
        if coordinates is not None:
            self.selecting_destination = True
            try:
                self.end_lat.setValue(coordinates[0])
                self.end_lon.setValue(coordinates[1])
            finally:
                self.selecting_destination = False

    def _use_boat(self):
        boat = self.controller.core.boat_info
        if boat.is_valid():
            self.set_location("start", (boat.latitude, boat.longitude))

    def _calculate(self):
        start = self.start_lat.value(), self.start_lon.value()
        destination = self.end_lat.value(), self.end_lon.value()
        if start == destination:
            self.status.setText("Choose different start and destination positions.")
            return
        polar = self.polar.currentText()
        if not polar:
            self.status.setText("No local polar is available.")
            return
        self.routing.start(
            RouteRequest(
                "routing-" + Path(polar).stem,
                start,
                destination,
                cast(datetime, self.departure.dateTime().toPython()),
                self.controller.core.polar_manager.get_path(polar),
                self.algorithm.currentData(),
                self.coastlines.isChecked(),
            )
        )

    def refresh(self):
        busy = self.routing.busy
        for control in self.inputs:
            control.setEnabled(not busy)
        self.use_boat.setEnabled(not busy and self.controller.core.boat_info.is_valid())
        self.calculate.setEnabled(not busy and self.polar.count() > 0)
        self.cancel_button.setEnabled(busy and not self.routing.cancel_event.is_set())
        self.progress.setValue(round(self.routing.percent))
        self.status.setText(self.routing.message)

    def refresh_routes(self, selected=None):
        selected = (
            selected
            or self.routes.currentData()
            or self.controller.core.routingManager.get_active()
        )
        self.routes.blockSignals(True)
        self.routes.clear()
        for route in self.controller.core.routingManager:
            self.routes.addItem(route.name, route)
        index = next(
            (
                i
                for i in range(self.routes.count())
                if self.routes.itemData(i) is selected
            ),
            0,
        )
        self.routes.setCurrentIndex(index)
        self.routes.blockSignals(False)
        self._select_route(index)

    def _completed(self, route):
        self.refresh_routes(route)
        self.pages.setCurrentWidget(self.results_page)
        self.fit_requested.emit()

    def _select_route(self, _):
        route = self.routes.currentData()
        self.waypoints.setRowCount(0)
        if route is None:
            self.summary.setText("No calculated route selected")
            self.fit.setEnabled(False)
            self.route_selected.emit(())
            return
        self.fit.setEnabled(True)
        self.waypoints.setRowCount(len(route.points))
        for row, point in enumerate(route.points):
            try:
                stamp = dateutil.parser.parse(str(point[2])).strftime("%d %b %H:%M")
            except (ValueError, TypeError):
                stamp = str(point[2])
            values = (
                f"{point[0]:.5f}",
                f"{point[1]:.5f}",
                stamp,
                f"{point[5]:.1f}" if point[5] is not None else "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    item.setToolTip(str(point[2]))
                self.waypoints.setItem(row, column, item)
        self.summary.setText(
            f"{route.length():.1f} nm · {len(route.points)} points\n"
            + (f"Last point: {route.points[-1][2]}" if route.points else "")
        )
        self.route_selected.emit(tuple((p[0], p[1]) for p in route.points))
