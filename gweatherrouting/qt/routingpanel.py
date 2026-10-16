"""GTK-aligned route setup, comparison and current-aware routing controls."""

from pathlib import Path
from datetime import datetime
import math
from typing import cast

import weatherrouting
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
    QLabel,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QScrollArea,
    QFrame,
    QToolButton,
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


class RoutingPanel(QWidget):
    plan_changed = Signal(object)
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
        setup_scroll = QScrollArea()
        style_scrollarea(setup_scroll)
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setFrameShape(QFrame.Shape.NoFrame)
        setup_scroll.setWidget(setup_page)
        self.pages.addTab(setup_scroll, "Plan")
        layout.addWidget(self.pages, 1)
        setup_layout.addWidget(section_label("Route setup"))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.polar = QComboBox()
        self.polar.addItems(controller.core.polar_manager.polars)
        form.addRow("Polar", self.polar)
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
        self.map_start = None
        self.map_destination = None
        self.start_mode = QComboBox()
        self.start_mode.addItem("First waypoint", "first-track-point")
        self.start_mode.addItem("Boat position", "boat-position")
        self.start_mode.currentIndexChanged.connect(self._plan_changed)
        form.addRow("Start", self.start_mode)
        self.destination = QComboBox()
        self.destination.currentIndexChanged.connect(self._plan_changed)
        form.addRow("Waypoints / destination", self.destination)
        self.coastlines = QCheckBox("Check coastlines")
        self.coastlines.setChecked(True)
        form.addRow(self.coastlines)
        setup_layout.addLayout(form)
        hint = QLabel(
            "Choose a waypoint track or saved POI. "
            "You can also select start and destination points on the map."
        )
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
        layout.addLayout(actions)
        for combo in (self.polar, self.algorithm, self.destination):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(14)
        self._build_advanced_pages()
        self._build_safety_controls(form)
        self.inputs = (
            self.polar,
            self.algorithm,
            self.departure,
            self.destination,
            self.start_mode,
            self.coastlines,
        )
        self.routing.confirm = self._confirm_current
        self.routing.changed.connect(self.refresh)
        controller.boat_changed.connect(self._boat_changed)
        self.refresh_destinations()
        self.refresh()

    def _build_safety_controls(self, form):
        form.addRow(section_label("Routing limits"))
        self.limit_wind = QCheckBox("Maximum true wind")
        self.maximum_wind = self._speed_control(30, 100)
        form.addRow(self.limit_wind, self.maximum_wind)
        self.motor_fallback = QCheckBox("Motor when sailing speed is below")
        self.minimum_sailing_speed = self._speed_control(3, 30)
        form.addRow(self.motor_fallback, self.minimum_sailing_speed)
        self.motor_speed = self._speed_control(5, 30)
        form.addRow("Motoring speed", self.motor_speed)
        self.wave_proxy = QCheckBox("Wind-based speed reduction (wave proxy)")
        self.wave_proxy.setToolTip(
            "Reduces sailing speed above 20 kn wind, up to 25% at 40 kn. "
            "Uses wind speed, not wave-height forecasts. Motoring speed is unchanged."
        )
        form.addRow(self.wave_proxy)
        for control in (self.limit_wind, self.motor_fallback, self.wave_proxy):
            control.toggled.connect(self._safety_changed)
        for control in (
            self.maximum_wind,
            self.minimum_sailing_speed,
            self.motor_speed,
        ):
            control.valueChanged.connect(lambda *_: self.options_changed.emit())
        self._refresh_safety()

    @staticmethod
    def _speed_control(value, maximum):
        widget = QDoubleSpinBox()
        widget.setRange(0.1, maximum)
        widget.setDecimals(1)
        widget.setSuffix(" kn")
        widget.setValue(value)
        return widget

    def _safety_changed(self, *_):
        self._refresh_safety()
        self.options_changed.emit()

    def _refresh_safety(self):
        enabled = not self.routing.busy
        for control in (self.limit_wind, self.motor_fallback, self.wave_proxy):
            control.setEnabled(enabled)
        self.maximum_wind.setEnabled(enabled and self.limit_wind.isChecked())
        for control in (self.minimum_sailing_speed, self.motor_speed):
            control.setEnabled(enabled and self.motor_fallback.isChecked())

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
        self.use_currents.toggled.connect(self._currents_toggled)
        currents.addWidget(self.use_currents)
        note = QLabel("Missing wind and current coverage downloads automatically.")
        note.setWordWrap(True)
        currents.addWidget(note)
        currents.addStretch()
        self.pages.addTab(self._scroll_page(currents_page), "Currents")
        advanced_page = QWidget()
        advanced = QVBoxLayout(advanced_page)
        advanced.addWidget(section_label("Currents"))
        priority_row = QHBoxLayout()
        priority_row.addWidget(QLabel("Overlapping models"))
        self.current_priority = QComboBox()
        self.current_priority.addItem("Finest detail", "finest")
        self.current_priority.addItem("Newest run", "newest")
        self.current_priority.setToolTip(
            "Which current model serves overlapping coverage."
        )
        self.current_priority.currentIndexChanged.connect(
            lambda *_: self.options_changed.emit()
        )
        priority_row.addWidget(self.current_priority, 1)
        self.priority_info = QToolButton()
        self.priority_info.setText("ⓘ")
        self.priority_info.setToolTip("Why this matters")
        self.priority_info.clicked.connect(self._explain_priority)
        priority_row.addWidget(self.priority_info)
        advanced.addLayout(priority_row)
        advanced.addStretch()
        self.pages.addTab(self._scroll_page(advanced_page), "Advanced")
        self.wind_algorithm_name = self.algorithm.currentText()
        self.preflight_dialog = None

    @staticmethod
    def _explain_priority():
        QMessageBox.information(
            None,
            "Overlapping current models",
            "Finest detail (default): where models overlap, the finest grid "
            "serves — a 500 m regional forecast beats a 9 km global one. "
            "Coastal currents follow shoreline and depth, so finer usually "
            "matters more than fresher inshore.\n\n"
            "Newest run: the latest model run serves everywhere, even where "
            "a finer older run has valid data. Better offshore, where flow "
            "is broad and freshness wins.\n\n"
            "Downloads always fetch the newest run of each model; this only "
            "decides ties between data already on disk.",
        )

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
        self._algorithm_changed()
        self.options_changed.emit()

    def _confirm_current(self, report):
        dialog = QMessageBox(
            QMessageBox.Icon.Warning,
            "Current data needed" if report.blocked else "Check current coverage",
            report.describe(True),
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
            current_priority=self.current_priority.currentData(),
            safety={
                key: (
                    getattr(self, key).isChecked()
                    if isinstance(getattr(self, key), QCheckBox)
                    else getattr(self, key).value()
                )
                for key in (
                    "limit_wind",
                    "maximum_wind",
                    "motor_fallback",
                    "minimum_sailing_speed",
                    "motor_speed",
                    "wave_proxy",
                )
            },
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
        priority = saved.get("current_priority")
        self.current_priority.setCurrentIndex(
            max(0, self.current_priority.findData(priority))
        )
        self._algorithm_changed()
        safety = mapping(saved.get("safety"))
        for key in (
            "limit_wind",
            "maximum_wind",
            "motor_fallback",
            "minimum_sailing_speed",
            "motor_speed",
            "wave_proxy",
        ):
            widget = getattr(self, key)
            value = safety.get(key)
            if isinstance(widget, QCheckBox):
                widget.setChecked(value is True)
            elif (
                type(value) in (float, int)
                and math.isfinite(value)
                and widget.minimum() <= value <= widget.maximum()
            ):
                widget.setValue(value)
        self._refresh_safety()
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
        selected_start = self.start_mode.currentData()
        self.destination.blockSignals(True)
        self.destination.clear()
        self.destination.addItem("Choose track or POI…", None)
        for track in self.controller.core.trackManager:
            self.destination.addItem("Track: " + track.name, track)
        for poi in self.controller.core.poiManager:
            self.destination.addItem(poi.name, tuple(poi.position))
        if self.map_destination is not None:
            self.destination.addItem("Map destination", self.map_destination)
        self.start_mode.blockSignals(True)
        self.start_mode.clear()
        self.start_mode.addItem("First waypoint", "first-track-point")
        self.start_mode.addItem("Boat position", "boat-position")
        for poi in self.controller.core.poiManager:
            self.start_mode.addItem("POI: " + poi.name, tuple(poi.position))
        if self.map_start is not None:
            self.start_mode.addItem("Map start", self.map_start)
        self.start_mode.setCurrentIndex(
            max(0, self.start_mode.findData(selected_start))
        )
        self.start_mode.blockSignals(False)
        self.destination.setCurrentIndex(max(0, self.destination.findText(selected)))
        self.destination.blockSignals(False)
        self._plan_changed()
        self.refresh()

    def _plan_changed(self, *_):
        target = self.destination.currentData()
        points = (
            target.to_list()
            if isinstance(target, Track)
            else ([target] if target is not None else [])
        )
        start = self._start_position(points)
        self.plan_changed.emit(
            tuple(([start] if start is not None else []) + list(points))
        )
        self.refresh()

    def _start_position(self, points):
        selected = self.start_mode.currentData()
        if selected == "first-track-point":
            return None
        if selected == "boat-position":
            boat = self.controller.core.boat_info
            return (boat.latitude, boat.longitude) if boat.is_valid() else None
        return selected if isinstance(selected, tuple) else None

    def set_location(self, which, coordinates):
        """Keep map point selection without exposing manual coordinate fields."""
        if self.routing.busy:
            return
        if which == "start":
            self.map_start = tuple(coordinates)
            title = "Map start"
        elif which == "destination":
            self.map_destination = tuple(coordinates)
            title = "Map destination"
        else:
            return
        self.refresh_destinations()
        selector = self.start_mode if which == "start" else self.destination
        selector.setCurrentIndex(selector.findText(title))
        self._plan_changed()

    def _boat_changed(self, *_):
        if self.start_mode.currentData() == "boat-position":
            self._plan_changed()
        else:
            self.refresh()

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
        destination = waypoints[-1] if waypoints else target
        if destination is None:
            raise ValueError("Choose a waypoint track or destination POI.")
        if isinstance(target, Track) and len(waypoints) < 2:
            raise ValueError("Choose a track with at least two points.")
        start = self._start_position(waypoints)
        if self.start_mode.currentData() == "first-track-point" and not waypoints:
            raise ValueError(
                "First waypoint requires a track destination; "
                "choose a start point for a POI or map destination."
            )
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
            auto_download=self.use_currents.isChecked(),
            current_priority=self.current_priority.currentData(),
            tws_max_kn=(
                self.maximum_wind.value() if self.limit_wind.isChecked() else None
            ),
            min_sailing_speed_kn=(
                self.minimum_sailing_speed.value()
                if self.motor_fallback.isChecked()
                else None
            ),
            motor_speed_kn=self.motor_speed.value(),
            wave_correct=self.wave_proxy.isChecked(),
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
        self._refresh_safety()
        for control in (
            *self.inputs,
            *self.parameter_widgets.values(),
            self.use_currents,
            self.current_priority,
        ):
            control.setEnabled(not busy)
        for enabled, values in self.comparison_axes.values():
            enabled.setEnabled(not busy)
            values.setEnabled(not busy and enabled.isChecked())
        self.calculate.setEnabled(
            not busy
            and self.polar.count() > 0
            and self.destination.currentData() is not None
        )
        self.cancel_button.setEnabled(busy and not self.routing.cancel_event.is_set())
        self.progress.setValue(round(self.routing.percent))
        self.status.setText(self.routing.message)
