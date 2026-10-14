"""Instrument connection editing and shared display preferences."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.connectionconfig import connection_config, connection_key
from gweatherrouting.core.nmea2000datasource import N2K_FORMATS
from gweatherrouting.core.serialdatasource import SerialDataSource


def combo(entries):
    widget = QComboBox()
    for label, value in entries:
        widget.addItem(label, value)
    return widget


class ConnectionDialog(QDialog):
    def __init__(self, parent, config=None):
        super().__init__(parent)
        self.setWindowTitle("Edit connection" if config else "Add connection")
        self.resize(440, 480)
        self.config = dict(config or {})
        self.result_config = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        layout.addLayout(form)
        self.kind = combo([("Network", "network"), ("Serial", "serial")])
        self.protocol = combo([("NMEA 0183", "nmea0183"), ("NMEA 2000", "nmea2000")])
        self.wire_format = combo([(str(v["label"]), k) for k, v in N2K_FORMATS.items()])
        self.direction = combo([("Input", "in"), ("Output", "out"), ("Both", "both")])
        for label, widget in (
            ("Connection", self.kind),
            ("Protocol", self.protocol),
            ("Gateway format", self.wire_format),
            ("Direction", self.direction),
        ):
            form.addRow(label, widget)
        self.form = form
        self.network_panel = QWidget()
        network = QFormLayout(self.network_panel)
        network.setContentsMargins(0, 0, 0, 0)
        self.network = combo([("TCP", "tcp"), ("UDP", "udp")])
        self.host = QLineEdit("localhost")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(10110)
        network.addRow("Transport", self.network)
        network.addRow("Host", self.host)
        network.addRow("Port", self.port)
        layout.addWidget(self.network_panel)
        self.serial_panel = QWidget()
        serial = QFormLayout(self.serial_panel)
        serial.setContentsMargins(0, 0, 0, 0)
        self.serial_port = QComboBox()
        self.serial_port.setEditable(True)
        self.serial_port.addItems(SerialDataSource.detect())
        self.baud = QSpinBox()
        self.baud.setRange(1, 4000000)
        self.baud.setValue(9600)
        serial.addRow("Serial port", self.serial_port)
        serial.addRow("Baud rate", self.baud)
        layout.addWidget(self.serial_panel)
        layout.addStretch()
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        data = self.config
        protocol = data.get("protocol", "nmea0183")
        wire_format = data.get(
            "format", protocol if protocol in N2K_FORMATS else "actisense"
        )
        if protocol in N2K_FORMATS:
            protocol = "nmea2000"
        for widget, value in (
            (self.kind, data.get("type", "network")),
            (self.protocol, protocol),
            (self.wire_format, wire_format),
            (self.direction, data.get("direction", "in")),
            (self.network, data.get("network", "tcp")),
        ):
            widget.setCurrentIndex(max(0, widget.findData(value)))
        self.host.setText(data.get("host", "localhost"))
        self.port.setValue(int(data.get("port", 10110)))
        self.serial_port.setCurrentText(
            data.get("data-port", self.serial_port.currentText())
        )
        self.baud.setValue(int(data.get("baudrate", 9600)))
        self.kind.currentIndexChanged.connect(self.refresh)
        self.protocol.currentIndexChanged.connect(self.protocol_changed)
        self.wire_format.currentIndexChanged.connect(self.format_changed)
        self.refresh()

    def refresh(self):
        serial = self.kind.currentData() == "serial"
        self.serial_panel.setVisible(serial)
        self.network_panel.setVisible(not serial)
        n2k = self.protocol.currentData() == "nmea2000"
        self.form.setRowVisible(self.wire_format, n2k)
        self.direction.setEnabled(not n2k)
        if n2k:
            self.direction.setCurrentIndex(0)

    def protocol_changed(self):
        self.refresh()
        self.format_changed()

    def format_changed(self):
        if self.protocol.currentData() == "nmea2000":
            self.baud.setValue(
                int(str(N2K_FORMATS[self.wire_format.currentData()]["baudrate"]))
            )

    def save(self):
        data = dict(self.config)
        data.update(
            type=self.kind.currentData(),
            protocol=self.protocol.currentData(),
            direction=self.direction.currentData(),
        )
        if data["protocol"] == "nmea2000":
            data["format"] = self.wire_format.currentData()
        if data["type"] == "serial":
            data.update(
                {
                    "data-port": self.serial_port.currentText(),
                    "baudrate": self.baud.value(),
                }
            )
        else:
            data.update(
                network=self.network.currentData(),
                host=self.host.text(),
                port=self.port.value(),
            )
        try:
            self.result_config = connection_config(data)
        except (ValueError, TypeError) as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.task = controller.connection_task
        self.applying = False
        self.setWindowTitle("Settings")
        self.resize(620, 500)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        connections = QWidget()
        content = QVBoxLayout(connections)
        hint = QLabel(
            "Add or edit instruments, then apply to save and reconnect.\n"
            + (
                "Offline mode keeps all connections closed."
                if controller.offline
                else ""
            )
        )
        hint.setWordWrap(True)
        content.addWidget(hint)
        self.connections = QListWidget()
        content.addWidget(self.connections)
        self.draft = [dict(c) for c in controller.connections.connections]
        row = QHBoxLayout()
        self.add_button = QPushButton("Add…")
        self.edit_button = QPushButton("Edit…")
        self.remove_button = QPushButton("Remove")
        self.apply_button = QPushButton("Apply connections")
        self.add_button.clicked.connect(lambda: self.edit_connection(False))
        self.edit_button.clicked.connect(lambda: self.edit_connection(True))
        self.remove_button.clicked.connect(self.remove_connection)
        self.apply_button.clicked.connect(self.apply_connections)
        for button in (
            self.add_button,
            self.edit_button,
            self.remove_button,
            self.apply_button,
        ):
            row.addWidget(button)
        content.addLayout(row)
        self.connection_status = QLabel()
        self.connection_status.setWordWrap(True)
        content.addWidget(self.connection_status)
        self.tabs.addTab(connections, "Connections")
        display = QWidget()
        form = QFormLayout(display)
        for label, key in (
            ("Smooth zoom", "smoothZoom"),
            ("Pan inertia", "panInertia"),
            ("Wind arrows on ground", "gribArrowOnGround"),
            ("Canadian online charts", "canadianCharts"),
        ):
            checkbox = QCheckBox(label)
            checkbox.setChecked(bool(controller.settings[key]))
            checkbox.toggled.connect(
                lambda value, key=key: self.set_preference(key, value)
            )
            form.addRow(checkbox)
            if key == "canadianCharts":
                checkbox.setEnabled(not controller.offline)
        self.opacity = QSpinBox()
        self.opacity.setRange(0, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setValue(round(controller.settings.gribArrowOpacity * 100))
        self.opacity.valueChanged.connect(
            lambda value: self.set_preference("gribArrowOpacity", value / 100)
        )
        form.addRow("Wind arrow opacity", self.opacity)
        self.tabs.addTab(display, "Display")
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.connections.itemSelectionChanged.connect(self.refresh_controls)
        self.task.changed.connect(self.refresh_controls)
        self.task.finished.connect(self.applied)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh_controls)
        self.timer.start()
        self.reload()

    def reload(self):
        selected = self.connections.currentRow()
        self.connections.clear()
        for c in self.draft:
            protocol = "NMEA 0183" if c["protocol"] == "nmea0183" else "NMEA 2000"
            self.connections.addItem(
                f"{connection_key(c)} · {protocol} · {c['direction']}"
            )
        if self.draft:
            self.connections.setCurrentRow(max(0, min(selected, len(self.draft) - 1)))
        self.refresh_controls()

    def refresh_controls(self):
        busy = self.task.busy
        selected = self.connections.currentRow() >= 0
        self.add_button.setEnabled(not busy)
        self.edit_button.setEnabled(not busy and selected)
        self.remove_button.setEnabled(not busy and selected)
        self.apply_button.setEnabled(
            not busy and self.draft != self.controller.connections.connections
        )
        statuses = [] if busy else self.controller.connections.get_status()
        connected = sum(s["connected"] for s in statuses)
        packets = sum(s["packets"] for s in statuses)
        self.connection_status.setText(
            "Offline · connections closed"
            if self.controller.offline
            else f"{connected} / {len(statuses)} connected · {packets:,} packets"
        )
        self.status.setText(self.task.message)

    def edit_connection(self, edit):
        index = self.connections.currentRow()
        dialog = ConnectionDialog(self, self.draft[index] if edit else None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            assert dialog.result_config is not None
            if edit:
                self.draft[index] = dialog.result_config
            else:
                self.draft.append(dialog.result_config)
            self.reload()

    def remove_connection(self):
        index = self.connections.currentRow()
        if index >= 0:
            self.draft.pop(index)
            self.reload()

    def apply_connections(self):
        configs = [dict(c) for c in self.draft]
        self.applying = self.task.start(
            "Saving connections and reconnecting…",
            lambda _: self.controller.connections.replace_connections(configs),
        )

    def applied(self, result, error):
        if error is None and self.applying:
            self.task.message = "Connections saved" + (
                " · offline" if self.controller.offline else ""
            )
            self.draft = [dict(c) for c in self.controller.connections.connections]
            self.reload()
        self.applying = False

    def set_preference(self, key, value):
        previous = self.controller.settings[key]
        try:
            self.controller.settings[key] = value
        except OSError as exc:
            self.controller.settings.__dict__[key] = previous
            dict.__setitem__(self.controller.settings, key, previous)
            QMessageBox.warning(self, "Unable to save setting", str(exc))
        self.controller.data.redraw()
