"""YB Tracking race import and download dialog."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from PySide6.QtCore import QObject, QThread, Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from gweatherrouting.core.race import (
    YBRaceError,
    download_yb_race,
    normalize_yb_race_slug,
    parse_race,
)

_ORIGINAL_DOWNLOAD_YB = download_yb_race


_ORPHANED_THREADS = set()


def _release_thread(thread):
    _ORPHANED_THREADS.discard(thread)


class _DownloadThread(QThread):
    completed = Signal(str, object, str)

    def __init__(self, slug: str, parent=None):
        super().__init__(parent)
        self.slug = slug

    def run(self):
        try:
            import gweatherrouting.core.race as race_module

            downloader = None
            if download_yb_race is not _ORIGINAL_DOWNLOAD_YB:
                downloader = download_yb_race
            core_downloader = getattr(race_module, "download_yb_race", None)
            if downloader is None:
                downloader = (
                    core_downloader if callable(core_downloader) else download_yb_race
                )
            race = downloader(self.slug)
            self.completed.emit(self.slug, race, "")
        except Exception as error:  # pragma: no cover - exercised via tests
            self.completed.emit(self.slug, None, str(error))


class YBRaceDialog(QDialog):
    """Collect a YB race code/URL and optionally download it."""

    download_started = Signal(str)
    download_finished = Signal(object)
    download_failed = Signal(str)
    download_completed = Signal(object, str)
    completed = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("YB Tracking")
        self.setModal(True)
        self.downloaded_race = None
        self.accepted_action = "import"
        self._threads = set()
        self._busy = False

        layout = QVBoxLayout(self)
        race_row = QHBoxLayout()
        self.race_entry = QLineEdit()
        self.race_entry.setPlaceholderText("Race code or https://yb.tl/<race>")
        self.race_entry.setAccessibleName("Race code or URL")
        race_row.addWidget(self.race_entry, 1)
        race_row.addWidget(QLabel("Race"))
        layout.addLayout(race_row)
        self.race_entry.textChanged.connect(self._update_link)

        self.update_boat_ids = QCheckBox("Update Boat IDs")
        self.update_handicaps = QCheckBox("Update handicaps")
        self.latest = QCheckBox("Latest")
        self.since_enabled = QCheckBox("Since")
        self.since_date = QLineEdit()
        self.since_date.setPlaceholderText("Date")
        for control in (
            self.update_boat_ids,
            self.update_handicaps,
            self.latest,
            self.since_enabled,
            self.since_date,
        ):
            control.setEnabled(False)
            control.setToolTip("Unavailable: direct YB download is not implemented.")
            layout.addWidget(control)

        self.status = QLabel(
            "Enter a race code or https://yb.tl/<race> URL, then Download or import local JSON."
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.unsupported_status = self.status
        self.status_label = self.status
        self.download_status = self.status
        self.download_status_label = self.status

        layout.addWidget(
            QLabel("Example: For Rolex Middle Sea Race 2014, the race name is rmsr2014")
        )

        self.race_link = QLabel("Race link: —")
        self.race_link.setTextFormat(Qt.TextFormat.RichText)
        self.race_link.setOpenExternalLinks(True)
        layout.addWidget(self.race_link)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.download_button = QPushButton("Download")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.start_download)
        actions.addWidget(self.download_button)
        self.local_import_button = QPushButton("Import JSON…")
        self.local_import_button.clicked.connect(self._import_local)
        actions.addWidget(self.local_import_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

        self.race_entry.textChanged.connect(
            lambda _value: self._update_download_enabled()
        )

    def _update_download_enabled(self):
        self.download_button.setEnabled(
            normalize_yb_race_slug(self.race_entry.text()) is not None
            and not self._busy
        )

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.download_button.setEnabled(
            (not busy) and normalize_yb_race_slug(self.race_entry.text()) is not None
        )
        self.race_entry.setEnabled(not busy)
        self.local_import_button.setEnabled(not busy)
        if hasattr(self, "cancel_button") and self.cancel_button is not None:
            self.cancel_button.setEnabled(not busy)

    def _set_status(self, text: str):
        self.status.setText(text)

    def _update_link(self, value: str) -> None:
        value = value.strip()
        parsed = urlparse(value if "://" in value else "")
        if (
            parsed.scheme.lower() == "https"
            and parsed.hostname
            and parsed.hostname.lower() == "yb.tl"
        ):
            segments = [
                segment for segment in parsed.path.strip("/").split("/") if segment
            ]
            slug = segments[0] if len(segments) == 1 else ""
        elif "://" in value:
            slug = ""
        else:
            slug = value.split("/", 1)[0]
        if not slug or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for ch in slug
        ):
            self.race_link.setText("Race link: —")
            self._update_download_enabled()
            return
        url = f"https://yb.tl/links/{slug}"
        self.race_link.setText(f'<a href="{url}">Race link: {url}</a>')
        self._update_download_enabled()

    def start_download(self):
        slug = normalize_yb_race_slug(self.race_entry.text())
        if slug is None:
            self._set_status("Enter a valid YB race code or https://yb.tl/<race> URL.")
            return
        self.downloaded_race = None
        self.accepted_action = "download"
        self._set_busy(True)
        self.show()
        self.raise_()
        self.activateWindow()
        self._set_status(f"Downloading {slug}…")
        self.download_started.emit(slug)
        thread = _DownloadThread(slug, self)
        self._threads.add(thread)
        thread.completed.connect(self._download_completed)
        thread.finished.connect(lambda thread=thread: self._discard(thread))
        thread.start()

    def _discard(self, thread):
        self._threads.discard(thread)
        thread.deleteLater()

    def _download_completed(self, slug: str, race, error: str):
        if error:
            self.downloaded_race = None
            self._set_busy(False)
            self._set_status(f"Download failed: {error}")
            self.download_failed.emit(error)
            self.download_completed.emit(None, error)
            self.completed.emit(None, error)
            return
        self.downloaded_race = race
        self._set_busy(False)
        self._set_status(f"Downloaded {race.name}: {len(race.boats)} boats.")
        self.download_finished.emit(race)
        self.download_completed.emit(race, "")
        self.completed.emit(race, "")
        self.accept()

    def _import_local(self):
        self.accepted_action = "import"
        self.accept()

    def reject(self):
        if self._busy:
            # Do not tear down an active worker thread.
            self._set_status("Download in progress; please wait for it to finish.")
            return
        super().reject()

    def closeEvent(self, event):
        if self._busy:
            # Keep the dialog alive while the worker thread is running; Qt must
            # not destroy a QThread that is still active.
            event.ignore()
            self._set_status("Download in progress; please wait for it to finish.")
            QTimer.singleShot(0, self._restore_visibility_while_busy)
            return
        super().closeEvent(event)

    def _restore_visibility_while_busy(self):
        if self._busy and not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()
