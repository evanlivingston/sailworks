"""Stable status-bar columns for cursor observations."""

from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget


class CursorStatus(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        self.fields = {}
        # Missing observations leave their column empty, never move other fields.
        for key, sample in (
            ("wind", "Wind 359.9°, 99.9 kts"),
            ("depth", "Sounding 999.9 m (999 m away)"),
            ("coords", "Lat -89.999999°  Lon -179.999999°"),
            ("current", None),
        ):
            label = QLabel(self)
            label.setMinimumWidth(0)
            if sample:
                label.setFixedWidth(label.fontMetrics().horizontalAdvance(sample) + 8)
            else:
                label.setSizePolicy(
                    QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
                )
            layout.addWidget(label, 0 if sample else 1)
            self.fields[key] = label
        self.hide()

    def set_readout(self, parts, coords, text):
        self._text = text
        for key, label in self.fields.items():
            value = coords if key == "coords" else parts.get(key, "")
            label.setText(value)
            label.setToolTip(value)
        self.setVisible(bool(text))

    def text(self):
        return self._text

    def clear(self):
        self.set_readout({}, "", "")
