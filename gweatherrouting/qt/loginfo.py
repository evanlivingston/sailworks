"""Modal inspection of an already decoded log; never opens or parses its file."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from gweatherrouting.core.n2kdevices import (
    canboat_device_class,
    canboat_pgn_description,
    captured_device_from_dict,
)
from gweatherrouting.core.n2kmanufacturers import manufacturer_name


UNKNOWN = "Unknown / not captured"

_NETWORK_PGNS = {
    59392,
    59904,
    60928,
    61184,
    126208,
    126720,
    126992,
    126993,
    126996,
    126998,
}


def _value(value):
    return UNKNOWN if value is None or value == "" else str(value)


def observed_data_types(device):
    """Summarize data categories using CANboat PGN descriptions."""
    labels = []
    for pgn, count in device.pgn_counts:
        if not count or pgn in _NETWORK_PGNS:
            continue
        description = canboat_pgn_description(pgn) or f"PGN {pgn}"
        if description not in labels:
            labels.append(description)
    if device.other_pgn_frames:
        labels.append("Other PGNs")
    if not labels and device.pgn_counts:
        return "Network / product metadata only"
    if not labels:
        return UNKNOWN
    shown = labels[:4]
    if len(labels) > len(shown):
        shown.append(f"+{len(labels) - len(shown)} more")
    return ", ".join(shown)


def device_details(device):
    product = device.product
    fields = [
        ("Source address", device.source),
        ("Identity epoch", device.epoch),
        ("CANboat device class", canboat_device_class(device)),
        ("NAME", device.name_hex),
        (
            "Manufacturer",
            (
                f"{manufacturer_name(device.manufacturer)} (code {device.manufacturer})"
                if device.manufacturer is not None
                else None
            ),
        ),
    ]
    fields.append(("Observed data", observed_data_types(device)))
    for label, attribute in (
        ("Model", "model"),
        ("Product code", "product_code"),
        ("NMEA 2000 version (raw numeric)", "protocol_version"),
        ("Software", "software"),
        ("Model version", "model_version"),
        ("Serial", "serial"),
        ("Certification level", "certification"),
        ("Load equivalency", "load_equivalency"),
    ):
        fields.append((label, getattr(product, attribute) if product else None))
    fields.extend(
        [
            ("CAN frames", device.frames),
            ("First / last frame", f"{device.first_frame} / {device.last_frame}"),
            (
                "Observed PGNs (frame counts)",
                ", ".join(
                    f"{pgn} {canboat_pgn_description(pgn) or 'Unknown PGN'}: {count:,}"
                    for pgn, count in device.pgn_counts
                ),
            ),
            ("Other PGN frames (inventory limit)", device.other_pgn_frames),
        ]
    )
    text = "\n".join(f"{label}: {_value(value)}" for label, value in fields)
    if device.manufacturer is not None and not (product and product.model):
        text += "\nManufacturer identifies the vendor only; model was not captured."
    if device.identity_limited:
        text += "\nIdentity history limit reached; this bucket is unidentified."
    return text


def _stored_device(data):
    return captured_device_from_dict(data)


class LogInfoDialog(QDialog):
    def __init__(self, log, parent=None):
        super().__init__(parent)
        self.setWindowTitle("File Info")
        self.setModal(True)
        self.resize(980, 720)
        layout = QVBoxLayout(self)
        if isinstance(log, dict):
            devices = tuple(_stored_device(device) for device in log.get("devices", ()))
            diagnostics = tuple(log.get("diagnostics", ()))
            device_metadata_available = log.get("device_metadata_available", False)
            path = log.get("path", UNKNOWN)
            format_name = log.get("format", "Unknown")
            entries = log.get("entries", 0)
            duration = log.get("duration", 0.0)
            timestamped = log.get("timestamped", False)
            skipped = log.get("skipped", 0)
            frame_count = log.get("frame_count")
            source_count = len(log.get("source_tracks", ()))
        else:
            devices = log.devices
            diagnostics = log.diagnostics
            device_metadata_available = log.device_metadata_available
            path = log.path
            format_name = log.format
            entries = len(log.entries)
            duration = log.duration
            timestamped = log.timestamped
            skipped = log.skipped
            frame_count = log.frame_count
            source_count = len(log.source_tracks)
        self.summary = QLabel(
            f"File: {path}\nFormat: {format_name}\n"
            f"Replay entries: {entries:,} · Duration: {duration:,.1f} s\n"
            f"Timestamped: {'Yes' if timestamped else 'No'} · "
            f"Skipped / unsupported: {skipped:,}\n"
            f"CAN frames: {_value(frame_count)} · GPS sources: {source_count}"
        )
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.summary)
        if not device_metadata_available:
            notice = "Device metadata unavailable for text NMEA logs; identities were not captured."
        elif not devices:
            notice = "No CAN devices captured. Device identity: unknown / not captured."
        else:
            notice = (
                "Captured devices (not necessarily GPS receivers). Select a row for details.\n"
                "The CANboat class comes from the captured NAME; PGN descriptions show observed traffic, not guaranteed hardware identity.\n"
                "Epochs separate observed identity changes at an address; reused or ambiguous "
                "addresses keep Source N track labels. Missing claims cannot be inferred."
            )
        self.notice = QLabel(notice)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.devices = QTreeWidget()
        self.devices.setHeaderLabels(
            [
                "Address",
                "Epoch",
                "Model",
                "NAME",
                "CANboat class",
                "Observed PGN data",
                "CAN frames",
            ]
        )
        self.devices.setRootIsDecorated(False)
        for device in devices:
            item = QTreeWidgetItem(
                [
                    str(device.source),
                    str(device.epoch),
                    _value(device.product.model if device.product else None),
                    _value(device.name_hex),
                    _value(canboat_device_class(device)),
                    observed_data_types(device),
                    f"{device.frames:,}",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, device)
            self.devices.addTopLevelItem(item)
        layout.addWidget(self.devices)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        self.devices.currentItemChanged.connect(self._selected)
        if self.devices.topLevelItemCount():
            self.devices.setCurrentItem(self.devices.topLevelItem(0))
        else:
            self.details.setPlainText(UNKNOWN)
        layout.addWidget(QLabel("Diagnostics"))
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setPlainText(
            "\n".join(diagnostics) or "No decode diagnostics."
        )
        layout.addWidget(self.diagnostics)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _selected(self, item, _previous):
        device = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        self.details.setPlainText(device_details(device) if device else UNKNOWN)
