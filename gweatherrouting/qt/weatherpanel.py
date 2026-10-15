"""Weather overlay controls for the active chart or log map."""

from PySide6.QtWidgets import (
    QCheckBox,
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
        self.on_ground = self.checkbox("Wind arrows over land", "gribArrowOnGround")
        controls.addWidget(self.on_ground)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.wind_opacity = self.percent("gribArrowOpacity")
        form.addRow("Wind opacity", self.wind_opacity)
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
        self.open_forecast = QPushButton("Open forecast…")
        self.open_forecast.clicked.connect(open_forecast)
        controls.addWidget(self.open_forecast)
        self.manage_forecasts = QPushButton("Manage / download forecasts…")
        self.manage_forecasts.clicked.connect(manage_forecasts)
        controls.addWidget(self.manage_forecasts)
        controls.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

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

    def refresh(self):
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
