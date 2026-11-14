"""Persistent master switches for the major displayed chart classes.

The background is one radio group: the vector basemap or satellite imagery.
The chart classes, including ENC, are independent overlays over that
background, so an uncovered area shows the active background.
"""

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from sailworks.rendering.chartclasses import CHART_CLASSES

from .satellite import available as satellite_available


class ChartLayersPanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.settings = controller.settings
        self.checkboxes = {}
        self.radios = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        hint = QLabel(
            "Choose one map background and which chart sources may overlay it."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.satellite_ready = satellite_available()
        layout.addWidget(self._section_label("Background"))
        self.background_group = QButtonGroup(self)
        self.background_group.setExclusive(True)
        for key, label in (
            ("chartWorldVisible", "Vector basemap"),
            ("chartSatelliteVisible", "Satellite imagery"),
        ):
            radio = QRadioButton(label)
            radio.setToolTip(
                "One background is shown at a time. Satellite imagery keeps "
                "coastlines and ENC soundings legible over it."
            )
            if key == "chartSatelliteVisible" and not self.satellite_ready:
                radio.setEnabled(False)
                radio.setToolTip(
                    "Satellite imagery needs the optional MapLibre runtime. "
                    "Install it with: pip install '.[satellite]'"
                )
            radio.toggled.connect(
                lambda checked, key=key: self._set_background(key, checked)
            )
            self.settings.register_on_change(
                key, lambda value, key=key: self._sync(key, value)
            )
            self.radios[key] = radio
            self.background_group.addButton(radio)
            layout.addWidget(radio)
        # Exactly one stored background is selected: imagery only when its
        # optional runtime is present, the basemap in every other case
        # (including a stored pair with neither or both on).  The stored
        # settings are written to match, so the panel and the viewport never
        # describe different backgrounds.
        satellite_active = self.satellite_ready and bool(
            self.settings.get("chartSatelliteVisible", False)
        )
        background = (
            "chartSatelliteVisible" if satellite_active else "chartWorldVisible"
        )
        blocked = self.radios[background].blockSignals(True)
        self.radios[background].setChecked(True)
        self.radios[background].blockSignals(blocked)
        for key, value in (
            ("chartSatelliteVisible", background == "chartSatelliteVisible"),
            ("chartWorldVisible", background == "chartWorldVisible"),
        ):
            if bool(self.settings.get(key, False)) != value:
                self.settings[key] = value
        if not self.satellite_ready:
            missing = QLabel("Satellite imagery is not installed.")
            missing.setWordWrap(True)
            layout.addWidget(missing)
        layout.addWidget(self._section_label("Overlays"))
        rows = [("canadianCharts", "Canadian online tiles (CHS)")]
        rows.extend(
            (key, label) for kind, key, label in CHART_CLASSES if kind != "world"
        )
        for key, label in rows:
            if key == "chartENCVisible":
                label = "ENC charts (transparent overlay)"
            checkbox = QCheckBox(label)
            checkbox.setToolTip(
                "Display only; does not change routing coastline checks. "
                "Individual chart selections and scale limits still apply. "
                "Uncovered areas show the active background."
            )
            checkbox.setChecked(bool(self.settings.get(key, True)))
            checkbox.toggled.connect(lambda checked, key=key: self._set(key, checked))
            self.settings.register_on_change(
                key, lambda value, key=key: self._sync(key, value)
            )
            self.checkboxes[key] = checkbox
            self._sync(key, self.settings.get(key, True))
            layout.addWidget(checkbox)
        note = QLabel(
            "Choose individual charts in Local files."
            " With all sources off, only the map background remains."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()

    def _section_label(self, text):
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _set(self, key, checked):
        self.settings[key] = bool(checked)

    def _set_background(self, key, checked):
        """Apply a radio choice: its background on, the other one off."""
        if not checked:
            return
        if key == "chartSatelliteVisible" and not self.satellite_ready:
            # Imagery cannot be presented: the basemap stays checked rather
            # than storing a background the viewport has to refuse.
            self.radios["chartWorldVisible"].setChecked(True)
            return
        other = (
            "chartSatelliteVisible"
            if key == "chartWorldVisible"
            else "chartWorldVisible"
        )
        # The chosen background is turned on before the other turns off:
        # while both are briefly on the viewport resolves to the requested
        # one, so no intermediate state leaves the map without a background.
        if not self.settings.get(key, False):
            self.settings[key] = True
        if self.settings.get(other, False):
            self.settings[other] = False

    def _sync(self, key, value):
        widget = self.checkboxes.get(key) or self.radios.get(key)
        if widget is not None:
            blocked = widget.blockSignals(True)
            widget.setChecked(bool(value))
            widget.blockSignals(blocked)
        if key in self.radios and not any(
            radio.isChecked() for radio in self.radios.values()
        ):
            # A background is always selected: clearing the last one restores
            # the basemap instead of leaving a blank map.
            self.radios["chartWorldVisible"].setChecked(True)
