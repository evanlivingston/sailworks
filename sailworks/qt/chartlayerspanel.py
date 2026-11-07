"""Persistent master switches for the major displayed chart classes."""

from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from sailworks.rendering.chartclasses import CHART_CLASSES

from .satellite import available as satellite_available


class ChartLayersPanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.settings = controller.settings
        self.checkboxes = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        hint = QLabel("Choose which chart sources may appear on the map.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        rows = [("canadianCharts", "Canadian online tiles (CHS)")]
        rows.extend((key, label) for _, key, label in CHART_CLASSES)
        rows.append(("chartSatelliteVisible", "Satellite basemap"))
        satellite_ready = satellite_available()
        for key, label in rows:
            checkbox = QCheckBox(label)
            checkbox.setToolTip(
                "Display only; does not change routing coastline checks. "
                "Individual chart selections and scale limits still apply."
            )
            if key == "chartSatelliteVisible" and not satellite_ready:
                checkbox.setEnabled(False)
                checkbox.setToolTip(
                    "Satellite imagery needs the optional MapLibre runtime. "
                    "Install it with: pip install '.[satellite]'"
                )
            checkbox.setChecked(bool(self.settings.get(key, True)))
            checkbox.toggled.connect(lambda checked, key=key: self._set(key, checked))
            self.settings.register_on_change(
                key, lambda value, key=key: self._sync(key, value)
            )
            self.checkboxes[key] = checkbox
            self._sync(key, self.settings.get(key, True))
            layout.addWidget(checkbox)
            if key == "chartSatelliteVisible" and not satellite_ready:
                missing = QLabel("Satellite imagery is not installed.")
                missing.setWordWrap(True)
                layout.addWidget(missing)
        note = QLabel(
            "Choose individual charts in Local files."
            " With all sources off, only the map background remains."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()

    def _set(self, key, checked):
        self.settings[key] = bool(checked)

    def _sync(self, key, value):
        checkbox = self.checkboxes.get(key)
        if checkbox is not None:
            blocked = checkbox.blockSignals(True)
            checkbox.setChecked(bool(value))
            checkbox.blockSignals(blocked)
