"""Weather overlay controls for the active chart or log map."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .dockchrome import style_scrollarea
from .marinepanel import BusyButton
from .theme import section_label
from gweatherrouting.rendering.windpainter import BARB_DENSITIES


class WeatherPanel(QWidget):
    def __init__(self, controller, active_map, manage_forecasts):
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
        controls.addWidget(self._section_divider())
        controls.addWidget(section_label("Wind"))
        self.wind = QCheckBox("Show wind")
        self.wind.setChecked(active_map().wind_visible)
        self.wind.toggled.connect(
            lambda enabled: active_map().set_wind_visible(enabled)
        )
        controls.addWidget(self.wind)
        self.wind_style = QComboBox()
        self.wind_style.addItem("Barbs", "barbs")
        self.wind_style.addItem("Streamlines", "streamlines")
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
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.addRow("Style", self.wind_style)
        self.wind_gradient = self.checkbox("Gradient", "gribWindGradient")
        form.addRow(self.wind_gradient)
        self.wind_gradient_opacity = self.percent("gribWindGradientOpacity")
        form.addRow("Gradient opacity", self.wind_gradient_opacity)
        self.wind_gradient_style = QComboBox()
        self.wind_gradient_style.addItem("Smooth", True)
        self.wind_gradient_style.addItem("Hard transitions", False)
        self.wind_gradient_style.currentIndexChanged.connect(
            lambda _index: self.set_preference(
                "gribWindGradientSmooth", self.wind_gradient_style.currentData()
            )
        )
        form.addRow("Gradient style", self.wind_gradient_style)
        self.wind_gradient_levels = self.integer_slider("gribWindGradientLevels", 2, 15)
        form.addRow("Gradient levels", self.wind_gradient_levels)
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
        form.addRow("Line density", self.barb_density)
        controls.addLayout(form)
        controls.addWidget(self._section_divider())
        controls.addWidget(section_label("Ship weather observations"))
        self.ship_observations = self.active_map().ship_observations
        self.ship_toggle = QCheckBox("Recent ship observations (NOAA ICOADS)")
        self.ship_toggle.setToolTip(
            "Show delayed ship weather reports. Report positions are not live vessel positions."
        )
        self.ship_toggle.setChecked(self.ship_observations.visible)
        self.ship_toggle.toggled.connect(self.ship_observations.set_visible)
        controls.addWidget(self.ship_toggle)
        self.ship_refresh = BusyButton("Refresh reports")
        self.ship_refresh.clicked.connect(self.ship_observations.refresh)
        controls.addWidget(self.ship_refresh)
        self.ship_observations.changed.connect(self._ship_observations_changed)
        controls.addWidget(self._section_divider())
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
        self.manage_forecasts = QPushButton("Manage / download forecasts…")
        self.manage_forecasts.clicked.connect(manage_forecasts)
        controls.addWidget(self.manage_forecasts)
        controls.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.refresh()

    def checkbox(self, label, key):
        widget = QCheckBox(label)
        widget.setChecked(bool(self.controller.settings[key]))
        widget.toggled.connect(lambda value: self.set_preference(key, value))
        return widget

    def percent(self, key):
        control = QWidget()
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = QSlider()
        slider.setOrientation(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        accessible_name = {
            "gribArrowOpacity": "Wind opacity",
            "gribWindGradientOpacity": "Gradient opacity",
            "gribIsobarOpacity": "Isobar opacity",
        }.get(key, key)
        slider.setAccessibleName(accessible_name)
        slider.setToolTip("Set opacity from 0% to 100%")
        value_label = QLabel()
        value_label.setAccessibleName(f"{accessible_name} value")
        value_label.setMinimumWidth(42)
        value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        slider.setValue(round(self.controller.settings[key] * 100))
        value_label.setText(f"{slider.value()} %")
        slider.valueChanged.connect(lambda value: value_label.setText(f"{value} %"))
        slider.valueChanged.connect(lambda value: self.set_preference(key, value / 100))
        layout.addWidget(slider, 1)
        layout.addWidget(value_label)
        control.slider = slider
        return control

    def integer_slider(self, key, minimum, maximum):
        control = QWidget()
        layout = QHBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setAccessibleName("Gradient levels")
        slider.setToolTip(f"Choose {minimum} to {maximum} gradient color levels")
        value_label = QLabel()
        value_label.setAccessibleName("Gradient levels value")
        value_label.setMinimumWidth(24)
        value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        slider.setValue(self.controller.settings[key])
        value_label.setText(str(slider.value()))
        slider.valueChanged.connect(lambda value: value_label.setText(str(value)))
        slider.valueChanged.connect(lambda value: self.set_preference(key, value))
        layout.addWidget(slider, 1)
        layout.addWidget(value_label)
        control.slider = slider
        return control

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
        presenter = getattr(self.active_map(), "_weather_presenter", None)
        if presenter is not None:
            presenter.request()
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

    @staticmethod
    def _section_divider():
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _ship_observations_changed(self):
        self.ship_refresh.set_busy(self.ship_observations.loading)
        status = self.ship_observations.status
        if status:
            main_window = self.window()
            status_bar = getattr(main_window, "statusBar", None)
            if callable(status_bar):
                timeout = 0 if self.ship_observations.loading else 12_000
                status_bar().showMessage(status, timeout)
        self.active_map().queue_draw()

    def dispose(self):
        try:
            self.ship_observations.changed.disconnect(self._ship_observations_changed)
        except (RuntimeError, TypeError):
            pass

    def refresh(self):
        self.wind_style.blockSignals(True)
        self.wind_style.setCurrentIndex(
            max(0, self.wind_style.findData(self.controller.settings.gribWindStyle))
        )
        self.wind_style.blockSignals(False)
        self.wind_gradient_style.blockSignals(True)
        self.wind_gradient_style.setCurrentIndex(
            max(
                0,
                self.wind_gradient_style.findData(
                    self.controller.settings.gribWindGradientSmooth
                ),
            )
        )
        self.wind_gradient_style.blockSignals(False)
        self.barb_density.blockSignals(True)
        self.barb_density.setCurrentIndex(self.density_index())
        self.barb_density.blockSignals(False)
        self.refresh_density_labels()
        for widget, value in (
            (self.wind, self.active_map().wind_visible),
            (self.wind_gradient, self.controller.settings.gribWindGradient),
            (
                self.wind_gradient_opacity,
                round(self.controller.settings.gribWindGradientOpacity * 100),
            ),
            (
                self.wind_gradient_levels,
                self.controller.settings.gribWindGradientLevels,
            ),
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
            elif hasattr(widget, "slider"):
                widget.slider.setValue(value)
            else:
                widget.setValue(value)
            widget.blockSignals(False)
        self.wind_gradient_opacity.setEnabled(self.wind_gradient.isChecked())
        self.wind_gradient_style.setEnabled(self.wind_gradient.isChecked())
        self.wind_gradient_levels.setEnabled(
            self.wind_gradient.isChecked()
            and not self.controller.settings.gribWindGradientSmooth
        )
        self.isobar_options.setEnabled(self.isobars.isChecked())
