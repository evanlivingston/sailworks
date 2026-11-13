"""Route setup, comparison and current-aware routing controls."""

from pathlib import Path
from datetime import datetime
import math
import time
from typing import cast

import weatherrouting
from PySide6.QtCore import QDateTime, QPoint, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
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

from sailworks.core.routingjob import RouteRequest
from sailworks.core.geo.track import Track
from sailworks.core.marine.routing import CurrentIsoRouter
from sailworks.core.routingoptions import (
    AXES,
    comparison_scenarios,
    scenario_request,
    parameter_values,
)
from .scrollchrome import style_scrollarea
from .dropdown import Dropdown
from .theme import section_label
from .text import qt_text


class InfoButton(QToolButton):
    """ⓘ button that opens a readable, word-wrapped explanation."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self._text = text
        self._popup = None
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.setInterval(200)
        self._show_timer.timeout.connect(self._open_popup)
        self.setText("i")
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(18, 18)
        self.setAccessibleName("More information")
        self.setStyleSheet(
            "QToolButton { border: 1px solid palette(mid); border-radius: 9px;"
            " background: palette(base); color: palette(text);"
            " font-size: 10px; padding: 0; }"
            "QToolButton:hover { border-color: palette(highlight); }"
        )

    def info_text(self):
        return self._text

    def enterEvent(self, event):
        self._show_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._show_timer.stop()
        self._hide_popup()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._show_timer.stop()
        if self._popup is None:
            self._open_popup()
        else:
            self._hide_popup()
        super().mousePressEvent(event)

    def hideEvent(self, event):
        self._hide_popup()
        super().hideEvent(event)

    def _open_popup(self):
        if self._popup is not None or not self._text:
            return
        popup = QLabel(self._text)
        popup.setWordWrap(True)
        popup.setMinimumWidth(220)
        popup.setMaximumWidth(340)
        popup.setWindowFlags(Qt.WindowType.ToolTip)
        popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        popup.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        popup.setStyleSheet(
            "QLabel { background-color: palette(base); color: palette(text);"
            " border: 1px solid palette(mid); border-radius: 6px;"
            " padding: 8px 10px; font-size: 12px; }"
        )
        popup.adjustSize()
        anchor = self.mapToGlobal(QPoint(0, self.height() + 6))
        screen = self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            x = min(
                max(anchor.x(), area.left() + 4),
                area.right() - popup.width() - 4,
            )
            y = anchor.y()
            if y + popup.height() > area.bottom() - 4:
                y = self.mapToGlobal(QPoint(0, 0)).y() - popup.height() - 6
            anchor = QPoint(x, y)
        popup.move(anchor)
        popup.show()
        self._popup = popup

    def _hide_popup(self):
        if self._popup is not None:
            self._popup.close()
            self._popup.deleteLater()
            self._popup = None


# Plain-language help for the settings that used to show CDS-style one-liners.
PARAMETER_HELP = {
    "subdiv": (
        "How finely the isochrone search subdivides the grid when checking "
        "whether a point is reachable. Higher values test more intermediate "
        "points, giving a smoother and usually more accurate route but taking "
        "longer to calculate. Lower values are faster and coarser."
    ),
    "concurrent": (
        "Calculate isochrones for several time steps in parallel using more "
        "CPU cores. Speeds up long routes, but uses more memory and can make "
        "progress less predictable while it runs."
    ),
    "min_increase": (
        "How much extra distance (in nautical miles) a new candidate point "
        "must add over the previous isochrone before it is kept. Larger values "
        "prune more aggressively (faster, possibly less optimal route); smaller "
        "values search harder."
    ),
}

SAFETY_HELP = {
    "limit_wind": (
        "Reject route points where the forecast true wind exceeds this speed. "
        "The router will detour or wait instead of sailing into stronger wind."
    ),
    "motor_fallback": (
        "When the boat's sailing speed drops below this value, continue at the "
        "motoring speed instead of stalling. Use it to model a motor assist, or "
        "to keep a minimum rate of progress in light air."
    ),
    "motor_speed": (
        "Speed used while motoring under the fallback above. Set it to roughly "
        "the boat's motoring speed in calm conditions."
    ),
    "wave_proxy": (
        "Reduces sailing speed by up to 25% when the wind exceeds 20 kn, "
        "reaching the full 25% reduction at 40 kn and above, as a stand-in for "
        "rough sea state. It uses forecast wind speed, not wave height, and "
        "does not change the motoring speed."
    ),
}

WAVE_DATA_HELP = (
    "Samples real wave height (m), period (s) and direction (degrees coming "
    "from true north) from a published wave forecast in the active tranche. "
    "Native calculations only. Wave coverage must include the route window; "
    "otherwise the calculation stops instead of substituting the wind proxy. "
    "Real wave heights are recorded in provenance but do not yet replace the "
    "wind-based speed reduction."
)


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
        self.backend = Dropdown("Calculation")
        self.backend.addItem("Legacy (default)", "legacy")
        self.backend.addItem("Native (experimental)", "native")
        self.backend.setToolTip(
            "Legacy runs the selected weatherrouting algorithm. Native runs the "
            "bounded local native engine on the selected forecast and request."
        )
        self.backend.currentIndexChanged.connect(self._backend_changed)
        form.addRow(self.backend)
        self.wave_data = Dropdown("Wave data")
        self.wave_data.setAccessibleName("Routing wave data")
        self.wave_data.setToolTip(WAVE_DATA_HELP)
        self.wave_data.currentIndexChanged.connect(self._wave_changed)
        form.addRow(self._row_with_info(self.wave_data, WAVE_DATA_HELP))
        self.wave_data_note = QLabel()
        self.wave_data_note.setWordWrap(True)
        self.wave_data_note.setProperty("secondary", True)
        form.addRow(self.wave_data_note)
        self._wave_entries = ()
        self._wave_entry_id = ""
        self.polar = Dropdown("Polar")
        self.polar.addItems(controller.core.polar_manager.polars)
        form.addRow(self.polar)
        self.algorithm = Dropdown("Algorithm")
        for entry in weatherrouting.list_routing_algorithms():
            label = (
                "Isochrone" if entry["name"] == "LinearBestIsoRouter" else entry["name"]
            )
            self.algorithm.addItem(label, entry["class"])
        form.addRow(self.algorithm)
        self._build_parameter_controls(form)
        self.departure = QDateTimeEdit(QDateTime(controller.time_control.time))
        self.departure.setCalendarPopup(True)
        self.departure.setDisplayFormat("yyyy-MM-dd HH:mm")
        form.addRow("Departure (local)", self.departure)
        self.departure.dateTimeChanged.connect(lambda *_: self.options_changed.emit())
        self.map_start = None
        self.map_destination = None
        # The start mode depends on the chosen track/POI, so pick the
        # destination first.
        self.destination = Dropdown("Waypoints / destination")
        self.start_mode = Dropdown("Start")
        self.start_mode.addItem("First waypoint", "first-track-point")
        self.start_mode.addItem("Boat position", "boat-position")
        self.destination.currentIndexChanged.connect(self._plan_changed)
        self.start_mode.currentIndexChanged.connect(self._plan_changed)
        form.addRow(self.destination)
        form.addRow(self.start_mode)
        self.coastlines = QCheckBox("Check coastlines")
        self.coastlines.setChecked(True)
        form.addRow(self.coastlines)
        self.coastline_clearance = QDoubleSpinBox()
        self.coastline_clearance.setRange(0, 100)
        self.coastline_clearance.setDecimals(1)
        self.coastline_clearance.setSingleStep(0.5)
        self.coastline_clearance.setSuffix(" nm")
        self.coastline_clearance.setToolTip(
            "Reject route legs within this distance of the coastline."
        )
        self.coastline_label = QLabel("Keep off coast")
        form.addRow(self.coastline_label, self.coastline_clearance)
        self.coastline_clearance.valueChanged.connect(
            lambda *_: self.options_changed.emit()
        )
        self.coastlines.toggled.connect(self._sync_coastline_clearance)
        self._sync_coastline_clearance(self.coastlines.isChecked())
        setup_layout.addLayout(form)
        self.backend_note = QLabel()
        self.backend_note.setWordWrap(True)
        self.backend_note.setProperty("secondary", True)
        setup_layout.addWidget(self.backend_note)
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
        self.status = QLabel(qt_text(self.routing.message))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._native_elapsed_timer = QTimer(self)
        self._native_elapsed_timer.setInterval(1000)
        self._native_elapsed_timer.timeout.connect(self._tick_native_heartbeat)
        layout.addLayout(actions)
        self._build_advanced_pages()
        self._build_safety_controls()
        form.addRow(self.parameters_section)
        self.inputs = (
            self.backend,
            self.wave_data,
            self.polar,
            self.algorithm,
            self.departure,
            self.destination,
            self.start_mode,
            self.coastlines,
            self.coastline_clearance,
        )
        self.routing.confirm = self._confirm_current
        self.routing.changed.connect(self.refresh)
        controller.boat_changed.connect(self._boat_changed)
        analysis = getattr(controller, "analysis", None)
        if analysis is not None and hasattr(analysis, "changed"):
            analysis.changed.connect(self._refresh_wave_sources)
        self.options_changed.connect(self._update_backend_note)
        self.refresh_destinations()
        self._refresh_wave_sources()
        self._backend_changed()
        self.refresh()

    def _build_safety_controls(self):
        self.safety_form = QFormLayout()
        self.safety_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.safety_form.addRow(section_label("Routing limits"))
        self.limit_wind = QCheckBox("Maximum true wind")
        self.limit_wind.setAccessibleDescription(SAFETY_HELP["limit_wind"])
        self.maximum_wind = self._speed_control(30, 100)
        self.maximum_wind.setAccessibleDescription(SAFETY_HELP["limit_wind"])
        self.safety_form.addRow(
            self._row_with_info(self.limit_wind, SAFETY_HELP["limit_wind"]),
            self.maximum_wind,
        )
        self.motor_fallback = QCheckBox("Motor when sailing speed is below")
        self.motor_fallback.setAccessibleDescription(SAFETY_HELP["motor_fallback"])
        self.minimum_sailing_speed = self._speed_control(3, 30)
        self.minimum_sailing_speed.setAccessibleDescription(
            SAFETY_HELP["motor_fallback"]
        )
        self.safety_form.addRow(
            self._row_with_info(self.motor_fallback, SAFETY_HELP["motor_fallback"]),
            self.minimum_sailing_speed,
        )
        self.motor_speed = self._speed_control(5, 30)
        self.motor_speed.setAccessibleDescription(SAFETY_HELP["motor_speed"])
        motor_label = QLabel("Motoring speed")
        self.safety_form.addRow(
            self._row_with_info(motor_label, SAFETY_HELP["motor_speed"]),
            self.motor_speed,
        )
        self.wave_proxy = QCheckBox("Wind-based speed reduction (wave proxy)")
        self.wave_proxy.setAccessibleDescription(SAFETY_HELP["wave_proxy"])
        self.safety_form.addRow(
            self._row_with_info(self.wave_proxy, SAFETY_HELP["wave_proxy"])
        )
        self.parameters_box.layout().addLayout(self.safety_form)
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
        self.parameter_form = QFormLayout()
        self.parameter_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        layout.addLayout(self.parameter_form)

        self.parameters_header = QToolButton()
        self.parameters_header.setText("Advanced")
        self.parameters_header.setCheckable(True)
        self.parameters_header.setChecked(False)
        self.parameters_header.setAutoRaise(True)
        self.parameters_header.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.parameters_header.setArrowType(Qt.ArrowType.RightArrow)
        self.parameters_header.toggled.connect(self._toggle_parameters)
        self.parameters_header.toggled.connect(lambda *_: self.options_changed.emit())

        self.parameters_section = QWidget()
        section_layout = QVBoxLayout(self.parameters_section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(4)
        section_layout.addWidget(self.parameters_header)
        section_layout.addWidget(self.parameters_box)
        self.parameters_box.setVisible(False)
        form.addRow(self.parameters_section)

        self.algorithm.currentIndexChanged.connect(self._algorithm_changed)
        self._algorithm_changed()

    def _toggle_parameters(self, expanded):
        self.parameters_header.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.parameters_box.setVisible(expanded)

    def _sync_coastline_clearance(self, checked):
        self.coastline_label.setVisible(checked)
        self.coastline_clearance.setVisible(checked)

    def _changed_legacy_parameter_names(self):
        algorithm = self.algorithm.currentData()
        if algorithm is None:
            return ()
        declared = getattr(algorithm, "PARAMS", {}) or {}
        values = self._read_parameters()
        return tuple(
            sorted(
                str(key)
                for key, value in values.items()
                if key not in declared or value != declared[key].default
            )
        )

    def _update_backend_note(self, *_):
        if self.backend.currentData() == "native":
            changed = self._changed_legacy_parameter_names()
            note = (
                "Native (experimental) uses the selected forecast, currents, "
                "polar, waypoints, safety limits and shoreline. It runs its own "
                "engine options: the selected Legacy algorithm and Advanced "
                "parameters are not applied. Native does not download missing "
                "data."
            )
            if changed:
                note += (
                    " Changed Legacy parameters must be reset before a Native "
                    "calculation: " + ", ".join(changed) + "."
                )
            if self._wave_entry_id:
                note += (
                    " Real wave heights will be sampled for coverage and "
                    "provenance; the wind-based speed reduction remains a "
                    "separate Legacy-style proxy."
                )
            self.backend_note.setText(note)
        else:
            note = (
                "Legacy (default) uses the selected algorithm and Advanced "
                "parameters."
            )
            if self._wave_entry_id:
                note += (
                    " Wave data is a Native input: set Wave data to None or "
                    "switch to Native."
                )
            self.backend_note.setText(note)

    def _backend_changed(self, *_):
        self._refresh_wave_sources()
        self._update_backend_note()
        self.options_changed.emit()

    def _wave_changed(self, *_):
        self._wave_entry_id = str(self.wave_data.currentData() or "")
        self._update_backend_note()
        self.options_changed.emit()

    def _refresh_wave_sources(self, *_):
        """List wave entries published alongside the active wind forecast."""
        analysis = getattr(self.controller, "analysis", None)
        active = getattr(analysis, "active", None)
        entries = ()
        if active is not None:
            entries = tuple(
                entry
                for entry in active.tranche.entries
                if "wave_height" in entry.fields
                and dict(getattr(entry, "field_times", ()) or ()).get("wave_height")
            )
        self._wave_entries = entries
        selected = self._wave_entry_id
        self.wave_data.blockSignals(True)
        try:
            self.wave_data.clear()
            self.wave_data.addItem("None (no real wave heights)", "")
            for entry in entries:
                label = (
                    f"{entry.model_id} · "
                    f"{entry.run_time.day} {entry.run_time:%b %H:%M} UTC"
                )
                self.wave_data.addItem(label, entry.entry_id)
            index = self.wave_data.findData(selected) if selected else 0
            if entries:
                self.wave_data.setCurrentIndex(max(0, index))
                self._wave_entry_id = str(self.wave_data.currentData() or "")
            elif active is None:
                # The active forecast is still loading; keep the saved choice
                # until the tranche can be inspected.
                self.wave_data.setCurrentIndex(0)
            else:
                self.wave_data.setCurrentIndex(0)
                self._wave_entry_id = ""
        finally:
            self.wave_data.blockSignals(False)
        if not entries:
            self.wave_data_note.setText(
                "No wave forecast is published with the active forecast."
            )
        elif self.backend.currentData() != "native":
            self.wave_data_note.setText("Real wave data applies to Native only.")
        else:
            self.wave_data_note.setText("Wave coverage must include the route window.")
        self.wave_data.setEnabled(
            not self.routing.busy
            and self.backend.currentData() == "native"
            and bool(entries)
        )
        self._update_backend_note()

    @staticmethod
    def _info_button(text):
        return InfoButton(text)

    def _row_with_info(self, widget, text):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(widget, 1)
        layout.addWidget(self._info_button(text))
        return row

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
            widget.setAccessibleDescription(
                PARAMETER_HELP.get(key, param.tooltip or param.name)
            )
            widget.setAccessibleName(param.name)
            label = QLabel(qt_text(param.name))
            label.setWordWrap(True)
            label_row = QWidget()
            label_layout = QHBoxLayout(label_row)
            label_layout.setContentsMargins(0, 0, 0, 0)
            label_layout.setSpacing(4)
            label_layout.addWidget(label, 1)
            label_layout.addWidget(
                self._info_button(PARAMETER_HELP.get(key, param.tooltip or param.name))
            )
            self.parameter_form.addRow(label_row, widget)
            self.parameter_widgets[key] = widget
        self.parameters_section.setVisible(True)
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
        self.current_priority = Dropdown("Overlapping models")
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

    @staticmethod
    def _selection_state(value):
        if isinstance(value, Track):
            return {"type": "track", "name": value.name}
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                return {
                    "type": "position",
                    "value": [float(value[0]), float(value[1])],
                }
            except (TypeError, ValueError):
                return None
        if isinstance(value, str) and value:
            return {"type": "value", "value": value}
        return None

    @staticmethod
    def _same_position(left, right):
        try:
            return (
                abs(float(left[0]) - float(right[0])) <= 1e-9
                and abs(float(left[1]) - float(right[1])) <= 1e-9
            )
        except (TypeError, ValueError, IndexError):
            return False

    def _combo_has_position(self, combo, value):
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return False
        for index in range(combo.count()):
            data = combo.itemData(index)
            if isinstance(data, (list, tuple)) and self._same_position(data, value):
                return True
        return False

    def _restore_combo(self, combo, saved):
        if not isinstance(saved, dict):
            return False
        kind = saved.get("type")
        value = saved.get("value")
        for index in range(combo.count()):
            data = combo.itemData(index)
            if kind == "track" and isinstance(data, Track) and data.name == value:
                combo.setCurrentIndex(index)
                return True
            if (
                kind == "position"
                and isinstance(data, (list, tuple))
                and self._same_position(data, value)
            ):
                combo.setCurrentIndex(index)
                return True
            if kind == "value" and data == value:
                combo.setCurrentIndex(index)
                return True
        return False

    def saved_options(self):
        cache = dict(self.parameter_cache)
        cache[self.parameter_name] = self._read_parameters()
        return dict(
            parameters=cache,
            backend=self.backend.currentData(),
            destination=self._selection_state(self.destination.currentData()),
            start=self._selection_state(self.start_mode.currentData()),
            departure=self.departure.dateTime().toPython().isoformat(),
            advanced_expanded=self.parameters_header.isChecked(),
            use_currents=self.use_currents.isChecked(),
            current_priority=self.current_priority.currentData(),
            wave_entry_id=self._wave_entry_id,
            coastline_clearance_nm=self.coastline_clearance.value(),
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
        from sailworks.core.workspacestate import mapping

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
        destination = mapping(saved.get("destination"))
        start = mapping(saved.get("start"))
        for combo, key, saved_value in (
            (self.destination, "destination", destination),
            (self.start_mode, "start", start),
        ):
            if saved_value.get("type") == "position" and not self._combo_has_position(
                combo, saved_value.get("value")
            ):
                value = saved_value.get("value")
                if isinstance(value, (list, tuple)) and len(value) >= 2:
                    if key == "destination":
                        self.map_destination = tuple(value[:2])
                    else:
                        self.map_start = tuple(value[:2])
        self.refresh_destinations()
        self._restore_combo(self.destination, destination)
        self._restore_combo(self.start_mode, start)
        departure = saved.get("departure")
        if isinstance(departure, str):
            try:
                self.departure.setDateTime(QDateTime(datetime.fromisoformat(departure)))
            except ValueError:
                pass
        self.parameters_header.setChecked(saved.get("advanced_expanded") is True)
        backend = saved.get("backend")
        self.backend.setCurrentIndex(
            max(
                0,
                self.backend.findData(
                    backend if backend in ("legacy", "native") else "legacy"
                ),
            )
        )
        self.use_currents.setChecked(saved.get("use_currents") is True)
        clearance_nm = saved.get("coastline_clearance_nm")
        if (
            type(clearance_nm) in (int, float)
            and math.isfinite(clearance_nm)
            and self.coastline_clearance.minimum()
            <= clearance_nm
            <= self.coastline_clearance.maximum()
        ):
            self.coastline_clearance.setValue(clearance_nm)
        priority = saved.get("current_priority")
        self.current_priority.setCurrentIndex(
            max(0, self.current_priority.findData(priority))
        )
        wave_entry_id = saved.get("wave_entry_id")
        self._wave_entry_id = wave_entry_id if isinstance(wave_entry_id, str) else ""
        self._refresh_wave_sources()
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
        self.options_changed.emit()
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
        backend = self.backend.currentData() or "legacy"
        try:
            if backend != "native" and self._wave_entry_id:
                raise ValueError(
                    "Real wave data is a Native calculation input. Set Wave "
                    "data to None or switch to Native."
                )
            request, scenarios = self._request()
            if backend == "native":
                from sailworks.core.marine.nativerequest import (
                    changed_legacy_parameters,
                    unapplied_legacy_parameter_message,
                )

                algorithm_name, changed = changed_legacy_parameters(request)
                if changed:
                    raise ValueError(
                        unapplied_legacy_parameter_message(algorithm_name, changed)
                    )
        except (ValueError, OverflowError) as error:
            self.status.setText(str(error))
            return
        self.routing.start(request, scenarios, backend=backend)

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
        roundings = ()
        if isinstance(target, Track):
            spec_getter = getattr(target, "point_rounding_spec", None)
            if callable(spec_getter):
                roundings = tuple(spec_getter(index) for index in range(len(waypoints)))
            if roundings and self.start_mode.currentData() == "first-track-point":
                # The first point is the start, not a rounding mark.
                roundings = (None,) + roundings[1:]
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
            roundings=roundings,
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
            wave_entry_id=self._wave_entry_id or None,
            coastline_clearance_distance_m=self.coastline_clearance.value() * 1852,
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

    @staticmethod
    def _elapsed_text(seconds):
        total = max(0, int(seconds))
        if total >= 3600:
            return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
        return f"{total // 60:02d}:{total % 60:02d}"

    @staticmethod
    def _counter_text(counters):
        if not isinstance(counters, dict):
            return ""
        parts = []
        retained = counters.get("retained_states")
        generated = counters.get("generated_states")
        if isinstance(retained, int) and not isinstance(retained, bool):
            if isinstance(generated, int) and not isinstance(generated, bool):
                parts.append(f"retained {retained:,}/{generated:,} states")
            else:
                parts.append(f"retained {retained:,} states")
        elif isinstance(generated, int) and not isinstance(generated, bool):
            parts.append(f"generated {generated:,} states")
        land_checks = counters.get("land_checks")
        if isinstance(land_checks, int) and not isinstance(land_checks, bool):
            parts.append(f"land checks {land_checks:,}")
        closest = counters.get("closest_approach_nm")
        if (
            isinstance(closest, (int, float))
            and not isinstance(closest, bool)
            and math.isfinite(closest)
        ):
            parts.append(f"closest {closest:.1f} nm")
        return " · ".join(parts)

    def _native_heartbeat_active(self):
        cancel = getattr(self.routing, "cancel_event", None)
        return bool(
            self.routing.busy
            and getattr(self.routing, "backend", None) == "native"
            and getattr(self.routing, "native_started_at", None) is not None
            and not (cancel is not None and cancel.is_set())
        )

    def _display_status(self):
        message = self.routing.message
        if not self._native_heartbeat_active():
            return message
        elapsed = self._elapsed_text(time.monotonic() - self.routing.native_started_at)
        text = f"{message} · elapsed {elapsed}"
        counters = self._counter_text(getattr(self.routing, "native_counters", None))
        if counters:
            text += f" · {counters}"
        return text

    def _tick_native_heartbeat(self):
        if not self._native_heartbeat_active():
            self._native_elapsed_timer.stop()
            return
        self.status.setText(self._display_status())

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
        self.wave_data.setEnabled(
            not busy
            and self.backend.currentData() == "native"
            and bool(self._wave_entries)
        )
        for enabled, values in self.comparison_axes.values():
            enabled.setEnabled(not busy)
            values.setEnabled(not busy and enabled.isChecked())
        self.calculate.setEnabled(
            not busy
            and self.polar.count() > 0
            and self.destination.currentData() is not None
        )
        self.cancel_button.setEnabled(busy and not self.routing.cancel_event.is_set())
        if getattr(self.routing, "progress_indeterminate", False):
            # Native runs expose lifecycle messages only: show a busy bar
            # rather than a percentage the engine cannot report.
            if self.progress.maximum() != 0:
                self.progress.setRange(0, 0)
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 100)
            self.progress.setValue(round(self.routing.percent))
        if self._native_heartbeat_active():
            if not self._native_elapsed_timer.isActive():
                self._native_elapsed_timer.start()
        else:
            self._native_elapsed_timer.stop()
        self.status.setText(self._display_status())
