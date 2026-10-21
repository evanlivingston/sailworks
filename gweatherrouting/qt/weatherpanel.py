"""Weather overlay controls for the active chart or log map."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .dockchrome import style_scrollarea
from .theme import section_label
from gweatherrouting.rendering.windpainter import BARB_DENSITIES


class WeatherPanel(QWidget):
    def __init__(self, controller, active_map, open_forecast, manage_forecasts):
        super().__init__()
        self.controller = controller
        self.active_map = active_map
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = style_scrollarea(QScrollArea())
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        controls = QVBoxLayout(content)
        controls.setContentsMargins(12, 8, 12, 8)
        controls.addWidget(section_label("Wind"))
        self.wind = QCheckBox("Show wind")
        self.wind.setChecked(active_map().wind_visible)
        self.wind.toggled.connect(
            lambda enabled: active_map().set_wind_visible(enabled)
        )
        controls.addWidget(self.wind)
        self.wind_style = QComboBox()
        self.wind_style.addItem("Barbs", "barbs")
        self.wind_style.addItem("Streamlines (legacy renderer)", "streamlines")
        self.wind_style.setToolTip(
            "Static forecast-flow curves with direction chevrons,"
            " or native-point wind barbs. No particle animation."
        )
        self.wind_style.setCurrentIndex(
            max(0, self.wind_style.findData(controller.settings.gribWindStyle))
        )
        self.wind_style.currentIndexChanged.connect(
            lambda _index: self.set_preference(
                "gribWindStyle", self.wind_style.currentData()
            )
        )
        controls.addWidget(self.wind_style)
        self.on_ground = self.checkbox("Show wind over land", "gribArrowOnGround")
        controls.addWidget(self.on_ground)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.wind_opacity = self.percent("gribArrowOpacity")
        form.addRow("Wind opacity", self.wind_opacity)
        self.barb_density = QComboBox()
        for key, label, _spacing in BARB_DENSITIES:
            self.barb_density.addItem(label, key)
        self.barb_density.setCurrentIndex(self.density_index())
        self.refresh_density_labels()
        self.barb_density.currentIndexChanged.connect(
            lambda _index: self.set_preference(
                "gribBarbDensity", self.barb_density.currentData()
            )
        )
        form.addRow("Density", self.barb_density)
        controls.addLayout(form)
        controls.addWidget(section_label("Isobars"))
        self.isobars = self.checkbox("Show isobars", "gribIsobarsVisible")
        controls.addWidget(self.isobars)
        self.isobar_options = QWidget()
        form = QFormLayout(self.isobar_options)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(1, 20)
        self.interval.setDecimals(1)
        self.interval.setSuffix(" hPa")
        self.interval.setValue(controller.settings.gribIsobarInterval)
        self.interval.valueChanged.connect(
            lambda value: self.set_preference("gribIsobarInterval", value)
        )
        form.addRow("Spacing", self.interval)
        self.isobar_opacity = self.percent("gribIsobarOpacity")
        form.addRow("Opacity", self.isobar_opacity)
        self.labels = self.checkbox("Pressure labels", "gribIsobarLabels")
        form.addRow(self.labels)
        controls.addWidget(self.isobar_options)
        self.isobar_options.setEnabled(self.isobars.isChecked())
        note = QLabel(
            "Isobars use mean sea-level pressure when included in the GRIB forecast."
        )
        note.setWordWrap(True)
        controls.addWidget(note)
        controls.addWidget(section_label("GRIB forecasts"))
        self.display_forecast = QComboBox()
        self.display_forecast.setToolTip(
            "Display only: choose one file, production run and member. "
            "This does not change routing sources or either map clock."
        )
        self.display_forecast.currentIndexChanged.connect(self.choose_forecast)
        controls.addWidget(self.display_forecast)
        self.forecast_identity = QLabel()
        self.forecast_identity.setWordWrap(True)
        controls.addWidget(self.forecast_identity)
        self.weather_renderer = QComboBox()
        self.weather_renderer.addItem("Viewport ENC + barbs", "viewport")
        self.weather_renderer.addItem("Legacy chart/weather recovery", "legacy")
        self.weather_renderer.setToolTip(
            "Viewport ENC is the default. Legacy recovery restores chart and weather "
            "presentation without changing forecast selection or routing."
        )
        self.weather_renderer.currentIndexChanged.connect(
            lambda _index: self.set_preference(
                "weatherRenderer", self.weather_renderer.currentData()
            )
        )
        controls.addWidget(self.weather_renderer)
        self.open_forecast = QPushButton("Open forecast…")
        self.open_forecast.clicked.connect(open_forecast)
        controls.addWidget(self.open_forecast)
        self.manage_forecasts = QPushButton("Manage / download forecasts…")
        self.manage_forecasts.clicked.connect(manage_forecasts)
        controls.addWidget(self.manage_forecasts)
        controls.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)
        controller.display_forecast.changed.connect(self.refresh_forecasts)
        self.refresh_forecasts()
        self.refresh()

    def choose_forecast(self, _index):
        selection = self.controller.display_forecast
        previous = selection.selected_key
        try:
            selection.choose(self.display_forecast.currentData() or "")
        except OSError as exc:
            self.controller.settings.__dict__["displayForecast"] = previous
            dict.__setitem__(self.controller.settings, "displayForecast", previous)
            QMessageBox.warning(self, "Unable to save display forecast", str(exc))
            self.refresh_forecasts()

    def refresh_forecasts(self):
        selection = self.controller.display_forecast
        self.display_forecast.blockSignals(True)
        self.display_forecast.clear()
        self.display_forecast.addItem("None — weather unavailable", "")
        for choice in selection.choices:
            self.display_forecast.addItem(choice.label, choice.key)
        index = self.display_forecast.findData(selection.selected_key)
        if index < 0:
            self.display_forecast.addItem(
                "Selected forecast unavailable", selection.selected_key
            )
            index = self.display_forecast.count() - 1
        self.display_forecast.setCurrentIndex(index)
        self.display_forecast.blockSignals(False)
        self.forecast_identity.setText(selection.status)
        self.forecast_identity.setToolTip(selection.error)

    def checkbox(self, label, key):
        widget = QCheckBox(label)
        widget.setChecked(bool(self.controller.settings[key]))
        widget.toggled.connect(lambda value: self.set_preference(key, value))
        return widget

    def percent(self, key):
        widget = QSpinBox()
        widget.setRange(0, 100)
        widget.setSuffix(" %")
        widget.setValue(round(self.controller.settings[key] * 100))
        widget.valueChanged.connect(lambda value: self.set_preference(key, value / 100))
        return widget

    def set_preference(self, key, value):
        settings = self.controller.settings
        previous = settings[key]
        try:
            settings[key] = value
        except OSError as exc:
            settings.__dict__[key] = previous
            dict.__setitem__(settings, key, previous)
            QMessageBox.warning(self, "Unable to save setting", str(exc))
        self.refresh()
        self.controller.data.redraw()

    def density_index(self):
        index = self.barb_density.findData(self.controller.settings.gribBarbDensity)
        return index if index >= 0 else self.barb_density.findData("standard")

    def refresh_density_labels(self):
        streamlines = self.controller.settings.gribWindStyle == "streamlines"
        for index, (key, label, _spacing) in enumerate(BARB_DENSITIES):
            self.barb_density.setItemText(
                index, "Maximum" if streamlines and key == "all" else label
            )
        self.barb_density.setToolTip(
            "Choose streamline spacing. Denser settings add paths;"
            " Maximum remains capped for responsiveness."
            if streamlines
            else "Choose how many wind barbs to display."
            " All forecast points shows every visible sample without a"
            " symbol-count cap."
        )

    def refresh(self):
        self.weather_renderer.blockSignals(True)
        self.weather_renderer.setCurrentIndex(
            max(
                0,
                self.weather_renderer.findData(
                    self.controller.settings.get("weatherRenderer", "viewport")
                ),
            )
        )
        self.weather_renderer.blockSignals(False)
        self.wind_style.blockSignals(True)
        self.wind_style.setCurrentIndex(
            max(0, self.wind_style.findData(self.controller.settings.gribWindStyle))
        )
        self.wind_style.blockSignals(False)
        self.barb_density.blockSignals(True)
        self.barb_density.setCurrentIndex(self.density_index())
        self.barb_density.blockSignals(False)
        self.refresh_density_labels()
        for widget, value in (
            (self.wind, self.active_map().wind_visible),
            (self.on_ground, self.controller.settings.gribArrowOnGround),
            (self.wind_opacity, round(self.controller.settings.gribArrowOpacity * 100)),
            (self.isobars, self.controller.settings.gribIsobarsVisible),
            (self.interval, self.controller.settings.gribIsobarInterval),
            (
                self.isobar_opacity,
                round(self.controller.settings.gribIsobarOpacity * 100),
            ),
            (self.labels, self.controller.settings.gribIsobarLabels),
        ):
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            else:
                widget.setValue(value)
            widget.blockSignals(False)
        self.isobar_options.setEnabled(self.isobars.isChecked())
