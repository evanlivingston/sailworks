"""Shared cancellable marine download lifecycle for windows and docked tools."""

import threading
from typing import Any, Callable

from gi.repository import GLib

from gweatherrouting.core.marine.chs import DownloadCancelledError


class MarineDownloadJobs:
    connect: Callable[..., Any]
    _busy_controls: tuple
    cancel_button: Any
    progress_row: Any
    progress: Any
    status: Any

    def _init_jobs(self):
        self._cancel = threading.Event()
        self._busy = False
        self._closed = False
        self._progress_timer = 0
        self.connect("destroy", self._on_destroy)

    def _set_busy(self, busy):
        self._busy = busy
        for widget in self._busy_controls:
            widget.set_sensitive(not busy)
        self.cancel_button.set_sensitive(busy)
        self.progress_row.set_visible(busy)
        if self._progress_timer:
            GLib.source_remove(self._progress_timer)
            self._progress_timer = 0
        if busy:
            self.progress.set_fraction(0)
            self.progress.pulse()
            self._progress_timer = GLib.timeout_add(150, self._pulse_progress)

    def _pulse_progress(self):
        self.progress.pulse()
        return True

    def _report_progress(self, cancel, message):
        if (
            not self._closed
            and self._busy
            and self._cancel is cancel
            and not cancel.is_set()
        ):
            self.status.set_text(message)
        return False

    def _job(self, operation, complete, message, with_progress=False):  # noqa: C901
        if self._busy:
            return
        self._cancel = threading.Event()
        cancel = self._cancel
        self._set_busy(True)
        self.status.set_text(message)

        def finish(result, error):
            if self._closed:
                return False
            self._set_busy(False)
            if cancel.is_set() or isinstance(error, DownloadCancelledError):
                self.status.set_text("Cancelled. No download was added.")
            elif error:
                self.status.set_text("Request failed: " + str(error))
            else:
                try:
                    complete(result)
                except (OSError, ValueError) as save_error:
                    self.status.set_text("Could not save: " + str(save_error))
            return False

        def run():
            try:
                if with_progress:

                    def report(message):
                        GLib.idle_add(self._report_progress, cancel, message)

                    result = operation(cancel, report)
                else:
                    result = operation(cancel)
            except Exception as error:
                GLib.idle_add(finish, None, error)
            else:
                GLib.idle_add(finish, result, None)

        threading.Thread(target=run, daemon=True).start()

    def _cancel_download(self, button):
        self._cancel.set()
        self.cancel_button.set_sensitive(False)
        self.status.set_text("Cancelling request…")

    def _on_destroy(self, window):
        self._closed = True
        self._cancel.set()
        if self._progress_timer:
            GLib.source_remove(self._progress_timer)
            self._progress_timer = 0
