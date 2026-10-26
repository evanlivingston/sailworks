"""File Info uses immutable loaded metadata, not synchronous UI file parsing."""

import os
from types import SimpleNamespace
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from gweatherrouting.core.n2kdevices import CapturedDevice, DeviceCollector
from gweatherrouting.core.nmeasession import NMEALog
from gweatherrouting.qt.loginfo import LogInfoDialog, device_details
from test.test_n2kdevices import claim, fragments, message


@pytest.fixture(scope="session")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def captured_log():
    collector = DeviceCollector()
    for part in [
        claim((185 << 21) | 1),
        *fragments(),
        claim(2),
        message(130000, b"", source=99),
    ]:
        collector.observe(part)
    return NMEALog(
        "captured.ebl",
        (),
        23,
        True,
        format="EBL (partial)",
        diagnostics=("Partial EBL support", "An incomplete captured record"),
        devices=collector.devices(),
        device_metadata_available=True,
        frame_count=collector.frames,
    )


def test_file_info_lists_epochs_identity_unknowns_and_diagnostics(app):
    dialog = LogInfoDialog(captured_log())
    try:
        assert dialog.isModal()
        assert "captured.ebl" in dialog.summary.text()
        assert "Replay entries: 0" in dialog.summary.text()
        assert dialog.devices.topLevelItemCount() == 3
        assert dialog.devices.topLevelItem(0).text(2) == "Captured GPS"
        details = dialog.details.toPlainText()
        for expected in (
            "Manufacturer: Beede Instruments (code 185)",
            "Product code: 1234",
            "Software: 2.4",
            "Model version: Rev B",
            "Serial: SERIAL-7",
            "NAME: 0x",
            "126996 Product Information: 20",
        ):
            assert expected in details
        dialog.devices.setCurrentItem(dialog.devices.topLevelItem(1))
        assert "Model: Unknown / not captured" in dialog.details.toPlainText()
        assert "model was not captured" in dialog.details.toPlainText()
        assert "NAME: 0x0000000000000002" in dialog.details.toPlainText()
        dialog.devices.setCurrentItem(dialog.devices.topLevelItem(2))
        assert "Source address: 99" in dialog.details.toPlainText()
        assert "NAME: Unknown / not captured" in dialog.details.toPlainText()
        assert "An incomplete captured record" in dialog.diagnostics.toPlainText()
    finally:
        dialog.close()


def test_text_nmea_and_empty_ebl_are_explicit_about_missing_metadata(app):
    for available, expected in (
        (False, "unavailable for text NMEA"),
        (True, "No CAN devices captured"),
    ):
        dialog = LogInfoDialog(
            NMEALog("missing.log", (), 0, False, device_metadata_available=available)
        )
        try:
            assert expected in dialog.notice.text()
            assert dialog.devices.topLevelItemCount() == 0
            assert "Unknown / not captured" in dialog.details.toPlainText()
        finally:
            dialog.close()


def test_file_info_summarizes_observed_device_data_types(app):
    device = CapturedDevice(
        source=42,
        epoch=1,
        name=60 << 49,
        manufacturer=None,
        product=None,
        frames=7,
        pgn_counts=(
            (130306, 2),
            (128259, 1),
            (127489, 2),
            (127508, 2),
        ),
        other_pgn_frames=0,
        first_frame=1,
        last_frame=7,
    )
    log = NMEALog(
        "sensors.log",
        (),
        0,
        False,
        devices=(device,),
        device_metadata_available=True,
    )
    dialog = LogInfoDialog(log)
    try:
        assert dialog.devices.headerItem().text(4) == "CANboat class"
        assert dialog.devices.headerItem().text(5) == "Observed PGN data"
        assert dialog.devices.topLevelItem(0).text(4) == "Navigation (60)"
        assert dialog.devices.topLevelItem(0).text(5) == (
            "Wind Data, Speed (water referenced), "
            "Engine Parameters, Dynamic, Battery Status"
        )
        assert "Observed PGNs (frame counts)" in (dialog.details.toPlainText())
        assert "not guaranteed hardware identity" in dialog.notice.text()
    finally:
        dialog.close()


@pytest.mark.parametrize(
    "code, expected",
    [
        (229, "Garmin (code 229)"),
        (2000, "Unknown manufacturer (2000) (code 2000)"),
        (None, "Unknown / not captured"),
    ],
)
def test_file_info_manufacturer_name_code_and_missing_model_are_distinct(
    code, expected
):
    collector = DeviceCollector()
    if code is None:
        collector.observe(message(129025, b"\x00" * 8))
    else:
        collector.observe(claim(code << 21))
    details = device_details(collector.devices()[0])
    assert f"Manufacturer: {expected}\n" in details
    assert "Model: Unknown / not captured\n" in details
    assert ("vendor only; model was not captured" in details) == (code is not None)


def test_file_info_dialog_can_use_cached_library_metadata(app):
    from dataclasses import asdict

    log = captured_log()
    cached = {
        "path": log.path,
        "format": log.format,
        "entries": len(log.entries),
        "duration": log.duration,
        "timestamped": log.timestamped,
        "skipped": log.skipped,
        "frame_count": log.frame_count,
        "source_tracks": [],
        "device_metadata_available": log.device_metadata_available,
        "devices": [asdict(device) for device in log.devices],
        "diagnostics": list(log.diagnostics),
    }
    dialog = LogInfoDialog(cached)
    try:
        assert dialog.isModal()
        assert dialog.devices.topLevelItemCount() == len(log.devices)
        assert "captured.ebl" in dialog.summary.text()
        assert log.devices[0].name_hex in dialog.devices.topLevelItem(0).text(3)
    finally:
        dialog.close()


def test_nmea_panel_has_library_not_direct_load_or_file_info(app, tmp_path):
    from gweatherrouting.qt.window import PreviewWindow

    session = SimpleNamespace(log=None)
    window = QWidget()
    window.session = session
    window.controller = SimpleNamespace(connection_task=SimpleNamespace(busy=False))
    window.syncing = False
    window._source_changed = lambda _index: None
    window.show_log_library = lambda: None
    window._track_source_changed = lambda _index: None
    window._record = lambda _active: None
    panel = PreviewWindow._nmea_panel(window)
    try:
        buttons = {button.text() for button in panel.findChildren(QPushButton)}
        assert buttons == {"Log library…"}
        assert not hasattr(window, "streaming")
        assert not hasattr(window, "load_button")
        assert not hasattr(window, "file_info_button")
    finally:
        panel.close()
        window.close()
