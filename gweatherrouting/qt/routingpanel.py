"""GTK-aligned route setup, comparison and current-aware routing controls."""

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
    QSpinBox,
    QLineEdit,
    QMessageBox,
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
from gweatherrouting.core.geo.track import Track
from gweatherrouting.core.marine.routing import CurrentIsoRouter
from gweatherrouting.core.routingoptions import (
    AXES,
    comparison_scenarios,
    scenario_request,
    parameter_values,
)
from .dockchrome import style_scrollarea
from .theme import section_label
from gweatherrouting.rendering.navigation import routing_summary


class RoutingPanel(QWidget):
    plan_changed = Signal(object)
    route_selected = Signal(object)
    fit_requested = Signal()
    options_changed = Signal()

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.routing = controller.routing
        self.setMinimumWidth(280)
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
        style_scrollarea(setup_scroll)
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
        self._build_parameter_controls(form)
        self.departure = QDateTimeEdit(QDateTime(controller.time_control.time))
        self.departure.setCalendarPopup(True)
        self.departure.setDisplayFormat("yyyy-MM-dd HH:mm")
        form.addRow("Departure (local)", self.departure)
        self.start_lat, self.start_lon, row = self._coordinates(48.4, -123.4)
        self.start_mode = QComboBox()
        self.start_mode.addItem("Custom coordinates", "custom")
        self.start_mode.addItem("First track point", "first-track-point")
        self.start_mode.addItem("Boat position", "boat-position")
        self.start_mode.currentIndexChanged.connect(self._plan_changed)
        form.addRow("Start position", self.start_mode)
        form.addRow("Start lat / lon", row)
        self.end_lat, self.end_lon, row = self._coordinates(48.5, -123.5)
        form.addRow("Destination", row)
        self.selecting_destination = False
        self.destination = QComboBox()
        self.destination.addItem("Custom coordinates", None)
        for poi in controller.core.poiManager:
            self.destination.addItem(poi.name, tuple(poi.position))
        for track in controller.core.trackManager:
            self.destination.addItem("Track: " + track.name, track)
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
        self.waypoints.setMinimumHeight(80)
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
        self._build_advanced_pages()
        self.inputs = (
            self.polar,
            self.algorithm,
            self.departure,
            self.start_lat,
            self.start_lon,
            self.end_lat,
            self.end_lon,
            self.destination,
            self.start_mode,
            self.coastlines,
        )
        for control in (self.start_lat, self.start_lon, self.end_lat, self.end_lon):
            control.valueChanged.connect(self._plan_changed)
        for control in (self.end_lat, self.end_lon):
            control.valueChanged.connect(self._custom_destination)
        self.routing.confirm = self._confirm_current
        self.routing.changed.connect(self.refresh)
        self.routing.completed.connect(self._completed)
        controller.boat_changed.connect(lambda _: self.refresh())
        self.refresh_destinations()
        self.refresh_routes()
        self.refresh()

    def _build_parameter_controls(self, form):
        self.parameter_cache = {}
        self.parameter_widgets = {}
        self.parameter_name = ""
        self.parameters_box = QWidget()
        layout = QVBoxLayout(self.parameters_box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(section_label("Router Parameters"))
        self.parameter_form = QFormLayout()
        self.parameter_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        layout.addLayout(self.parameter_form)
        form.addRow(self.parameters_box)
        self.algorithm.currentIndexChanged.connect(self._algorithm_changed)
        self._algorithm_changed()

    def _read_parameters(self):
        return {
            key: widget.isChecked() if isinstance(widget, QCheckBox) else widget.value()
            for key, widget in self.parameter_widgets.items()
        }

    def _algorithm_changed(self, *_):
        if self.parameter_name:
            self.parameter_cache[self.parameter_name] = self._read_parameters()
        self.parameter_widgets = {}
        while self.parameter_form.rowCount():
            self.parameter_form.removeRow(0)
        algorithm = self.algorithm.currentData()
        if algorithm is None:
            return
        self.parameter_name = algorithm.__name__
        values = self.parameter_cache.get(self.parameter_name, {})
        for key, param in algorithm.PARAMS.items():
            widget = self._parameter_widget(param, values.get(key, param.value))
            if widget is None:
                continue
            widget.setToolTip(param.tooltip)
            widget.setAccessibleName(param.name)
            label = QLabel(param.name)
            label.setWordWrap(True)
            self.parameter_form.addRow(label, widget)
            self.parameter_widgets[key] = widget
        self.parameters_box.setVisible(bool(self.parameter_widgets))
        self.options_changed.emit()

    def _parameter_widget(self, param, value):
        if param.ttype == "bool":
            check = QCheckBox()
            check.setChecked(bool(value))
            check.toggled.connect(lambda *_: self.options_changed.emit())
            return check
        if param.ttype not in ("int", "float"):
            return None
        number = QSpinBox() if param.ttype == "int" else QDoubleSpinBox()
        if isinstance(number, QDoubleSpinBox):
            number.setDecimals(param.digits)
        number.setRange(param.lower, param.upper)
        number.setSingleStep(param.step)
        number.setValue(value)
        number.valueChanged.connect(lambda *_: self.options_changed.emit())
        return number

    @staticmethod
    def _scroll_page(content):
        scroll = style_scrollarea(QScrollArea())
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _build_advanced_pages(self):
        compare_page = QWidget()
        compare = QVBoxLayout(compare_page)
        note = QLabel("Enable each axis to compare and enter comma-separated values.")
        note.setWordWrap(True)
        compare.addWidget(note)
        self.comparison_axes = {}
        for key, label, text, _ in AXES:
            enabled, values = QCheckBox(label), QLineEdit(text)
            values.setEnabled(False)
            enabled.toggled.connect(values.setEnabled)
            enabled.toggled.connect(lambda *_: self.options_changed.emit())
            values.textChanged.connect(lambda *_: self.options_changed.emit())
            compare.addWidget(enabled)
            compare.addWidget(values)
            self.comparison_axes[key] = (enabled, values)
        compare.addStretch()
        self.pages.addTab(self._scroll_page(compare_page), "Compare")
        currents_page = QWidget()
        currents = QVBoxLayout(currents_page)
        self.use_currents = QCheckBox("Include currents")
        self.use_currents.setToolTip("Include ocean and tidal currents in routing.")
        self.auto_currents = QCheckBox("Download data automatically")
        self.auto_currents.setToolTip(
            "Use the latest GFS wind run and download missing wind and current coverage."
        )
        self.auto_currents.setChecked(True)
        self.auto_currents.setEnabled(False)
        self.use_currents.toggled.connect(self._currents_toggled)
        self.auto_currents.toggled.connect(lambda *_: self.options_changed.emit())
        currents.addWidget(self.use_currents)
        currents.addWidget(self.auto_currents)
        currents.addStretch()
        self.pages.addTab(self._scroll_page(currents_page), "Currents")
        self.wind_algorithm_name = self.algorithm.currentText()
        self.preflight_dialog = None

    def _currents_toggled(self, enabled):
        if enabled:
            self.wind_algorithm_name = self.algorithm.currentText()
            self.coastlines.setChecked(True)
        self.algorithm.blockSignals(True)
        self.algorithm.clear()
        if enabled:
            self.algorithm.addItem("Isochrone · currents", CurrentIsoRouter)
        else:
            for entry in weatherrouting.list_routing_algorithms():
                self.algorithm.addItem(
                    (
                        "Isochrone"
                        if entry["name"] == "LinearBestIsoRouter"
                        else entry["name"]
                    ),
                    entry["class"],
                )
            self.algorithm.setCurrentIndex(
                max(0, self.algorithm.findText(self.wind_algorithm_name))
            )
        self.algorithm.blockSignals(False)
        self.auto_currents.setEnabled(enabled and not self.routing.busy)
        self._algorithm_changed()
        self.options_changed.emit()

    def _confirm_current(self, report):
        dialog = QMessageBox(
            QMessageBox.Icon.Warning,
            "Current data needed" if report.blocked else "Check current coverage",
            report.describe(self.auto_currents.isChecked()),
            QMessageBox.StandardButton.NoButton,
            self,
        )
        cancel = dialog.addButton("Cancel routing", QMessageBox.ButtonRole.RejectRole)
        run = (
            dialog.addButton("Run anyway", QMessageBox.ButtonRole.AcceptRole)
            if not report.blocked
            else None
        )
        dialog.setDefaultButton(cancel)
        self.preflight_dialog = dialog
        self.routing.dismiss_preflight.connect(dialog.reject)
        try:
            dialog.exec()
            return (
                run is not None and dialog.clickedButton() is run and not report.blocked
            )
        finally:
            self.routing.dismiss_preflight.disconnect(dialog.reject)
            self.preflight_dialog = None
            dialog.deleteLater()

    def saved_options(self):
        cache = dict(self.parameter_cache)
        cache[self.parameter_name] = self._read_parameters()
        return dict(
            parameters=cache,
            use_currents=self.use_currents.isChecked(),
            auto_currents=self.auto_currents.isChecked(),
            comparison={
                key: dict(enabled=enabled.isChecked(), values=values.text())
                for key, (enabled, values) in self.comparison_axes.items()
            },
        )

    def restore_options(self, saved):
        from gweatherrouting.core.workspacestate import mapping

        cache = mapping(saved.get("parameters"))
        self.parameter_cache = {}
        for algorithm in [
            entry["class"] for entry in weatherrouting.list_routing_algorithms()
        ]:
            try:
                self.parameter_cache[algorithm.__name__] = parameter_values(
                    algorithm, mapping(cache.get(algorithm.__name__))
                )
            except ValueError:
                pass
        # Preserve the validated cache while replacing the initially displayed defaults.
        self.parameter_name = ""
        self.use_currents.setChecked(saved.get("use_currents") is True)
        if isinstance(saved.get("auto_currents"), bool):
            self.auto_currents.setChecked(saved["auto_currents"])
        self._algorithm_changed()
        for key, (enabled, values) in self.comparison_axes.items():
            axis = mapping(mapping(saved.get("comparison")).get(key))
            text = axis.get("values")
            if isinstance(text, str) and len(text) <= 4096:
                values.setText(text)
            enabled.setChecked(axis.get("enabled") is True)

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

    def refresh_destinations(self):
        selected = self.destination.currentText()
        self.destination.blockSignals(True)
        self.destination.clear()
        self.destination.addItem("Custom coordinates", None)
        for poi in self.controller.core.poiManager:
            self.destination.addItem(poi.name, tuple(poi.position))
        for track in self.controller.core.trackManager:
            self.destination.addItem("Track: " + track.name, track)
        selected_start = self.start_mode.currentData()
        self.start_mode.blockSignals(True)
        while self.start_mode.count() > 3:
            self.start_mode.removeItem(3)
        for poi in self.controller.core.poiManager:
            self.start_mode.addItem("POI: " + poi.name, tuple(poi.position))
        self.start_mode.setCurrentIndex(
            max(0, self.start_mode.findData(selected_start))
        )
        self.start_mode.blockSignals(False)
        self.destination.setCurrentIndex(max(0, self.destination.findText(selected)))
        self.destination.blockSignals(False)

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
        points = self.destination.currentData()
        if isinstance(points, Track):
            points = points.to_list()
        else:
            points = [(self.end_lat.value(), self.end_lon.value())]
        start = self._start_position(points)
        self.plan_changed.emit(
            tuple(([start] if start is not None else []) + list(points))
        )

    def _start_position(self, points):
        selected = self.start_mode.currentData()
        if selected == "first-track-point":
            return None
        if selected == "boat-position":
            boat = self.controller.core.boat_info
            return (boat.latitude, boat.longitude) if boat.is_valid() else None
        return (
            selected
            if isinstance(selected, tuple)
            else (self.start_lat.value(), self.start_lon.value())
        )

    def set_location(self, which, coordinates):
        if self.routing.busy:
            return
        if which == "start":
            self.start_mode.setCurrentIndex(0)
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
        if isinstance(coordinates, Track):
            points = coordinates.to_list()
            coordinates = points[-1] if points else None
        if coordinates is not None:
            self.selecting_destination = True
            try:
                self.end_lat.setValue(coordinates[0])
                self.end_lon.setValue(coordinates[1])
            finally:
                self.selecting_destination = False
        self._plan_changed()

    def _use_boat(self):
        boat = self.controller.core.boat_info
        if boat.is_valid():
            self.set_location("start", (boat.latitude, boat.longitude))

    def _calculate(self):
        try:
            request, scenarios = self._request()
        except (ValueError, OverflowError) as error:
            self.status.setText(str(error))
            return
        self.routing.start(request, scenarios)

    def _request(self):
        target = self.destination.currentData()
        waypoints = (
            tuple(tuple(p) for p in target.to_list())
            if isinstance(target, Track)
            else ()
        )
        destination = (
            waypoints[-1] if waypoints else (self.end_lat.value(), self.end_lon.value())
        )
        if isinstance(target, Track) and len(waypoints) < 2:
            raise ValueError("Choose a track with at least two points.")
        start = self._start_position(waypoints)
        if self.start_mode.currentData() == "first-track-point" and not waypoints:
            raise ValueError("First track point requires a track destination.")
        if self.start_mode.currentData() == "boat-position" and start is None:
            raise ValueError("No valid boat position is available.")
        if start == destination and not waypoints:
            raise ValueError("Choose different start and destination positions.")
        polar = self.polar.currentText()
        if not polar:
            raise ValueError("No local polar is available.")
        request = RouteRequest(
            "routing-" + Path(polar).stem,
            start,
            cast(tuple[float, float], destination),
            cast(datetime, self.departure.dateTime().toPython()),
            self.controller.core.polar_manager.get_path(polar),
            self.algorithm.currentData(),
            self.coastlines.isChecked(),
            parameters=tuple(self._read_parameters().items()),
            waypoints=waypoints,
            use_currents=self.use_currents.isChecked(),
            auto_download=self.use_currents.isChecked()
            and self.auto_currents.isChecked(),
        )
        axes = {
            key: values.text()
            for key, (enabled, values) in self.comparison_axes.items()
            if enabled.isChecked()
        }
        scenarios = tuple(
            scenario_request(request, scenario)
            for scenario in comparison_scenarios(axes)
        )
        return request, scenarios

    def refresh(self):
        busy = self.routing.busy
        for control in (
            *self.inputs,
            *self.parameter_widgets.values(),
            self.use_currents,
        ):
            control.setEnabled(not busy)
        self.auto_currents.setEnabled(not busy and self.use_currents.isChecked())
        for enabled, values in self.comparison_axes.values():
            enabled.setEnabled(not busy)
            values.setEnabled(not busy and enabled.isChecked())
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
            routing_summary(route)
            + f" · {len(route.points)} points\n"
            + (f"Last point: {route.points[-1][2]}" if route.points else "")
        )
        self.route_selected.emit(tuple((p[0], p[1]) for p in route.points))
