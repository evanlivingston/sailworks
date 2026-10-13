"""Views of the application's shared NMEA session."""

import os
import time
from threading import Thread

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from gweatherrouting.core.nmeasession import NMEASession, read_nmea_log
from .widgets.toolpanel import tool_content


def get_nmea_session(core):
    """Core owns the session; also support lightweight standalone tab hosts."""
    if not hasattr(core, "nmea_session"):
        core.nmea_session = NMEASession(core.connectionManager)
    return core.nmea_session


class NMEAPlaybackClock:
    """One application clock, independent of visibility or the number of views."""

    def __init__(self, session):
        self.session = session
        self.last = time.monotonic()
        self.timer = GLib.timeout_add(100, self._tick)

    def _tick(self):
        now = time.monotonic()
        self.session.advance(now - self.last)
        self.last = now
        return True

    def close(self):
        GLib.source_remove(self.timer)


class NMEAControls(Gtk.Box):
    @classmethod
    def attach(cls, parent, core, dock):
        """Reuse the same component when tabs share a workspace dock."""
        if "nmea" not in dock.panels:
            dock.register("nmea", "NMEA", cls(parent, core))
        return dock.panels["nmea"].get_children()[0]

    def __init__(self, parent, core):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.parent_window = parent
        self.session = get_nmea_session(core)
        self._syncing = False
        self._alive = True
        content = tool_content()
        self.pack_start(content, False, False, 0)
        self.source = Gtk.ComboBoxText()
        self.source.append_text("Live connections")
        self.source.append_text("Loaded log")
        content.pack_start(Gtk.Label(label="Data source", xalign=0), False, False, 0)
        content.pack_start(self.source, False, False, 0)
        self.source.connect("changed", self._source_changed)
        self.live = Gtk.CheckButton(label="Stream live NMEA")
        self.live.connect("toggled", self._stream_changed)
        content.pack_start(self.live, False, False, 0)
        self.load_button = Gtk.Button(label="Load NMEA log…")
        self.load_button.connect("clicked", self._load)
        content.pack_start(self.load_button, False, False, 0)
        row = Gtk.Box(spacing=6)
        self.play = Gtk.ToggleButton(label="Play")
        self.play.connect("toggled", self._play_changed)
        restart = Gtk.Button(label="Restart")
        restart.connect("clicked", lambda *_: self.session.restart())
        self.restart_button = restart
        row.pack_start(self.play, True, True, 0)
        row.pack_start(restart, True, True, 0)
        content.pack_start(row, False, False, 0)
        self.speed = Gtk.ComboBoxText()
        for value in (0.5, 1, 2, 5, 10):
            self.speed.append(str(value), f"{value:g}×")
        self.speed.connect("changed", self._speed_changed)
        content.pack_start(Gtk.Label(label="Playback speed", xalign=0), False, False, 0)
        content.pack_start(self.speed, False, False, 0)
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        content.pack_start(self.progress, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(28)
        content.pack_start(self.status, False, False, 0)
        self.session.connect("changed", self._changed)
        self.connect("destroy", self._destroy)
        self.refresh()

    def _changed(self, _):
        self.refresh()

    def refresh(self):
        if not self._alive:
            return False
        self._syncing = True
        session = self.session
        self.source.set_active(0 if session.source == "live" else 1)
        self.live.set_active(session.streaming)
        self.live.set_sensitive(session.source == "live" and not session.loading)
        self.load_button.set_sensitive(not session.loading)
        self.source.set_sensitive(not session.loading)
        self.play.set_active(session.playing)
        self.play.set_label("Pause" if session.playing else "Play")
        enabled = (
            session.log is not None and session.source == "log" and not session.loading
        )
        self.play.set_sensitive(enabled)
        self.restart_button.set_sensitive(enabled)
        self.speed.set_active_id(
            str(session.speed).removesuffix(".0")
            if session.speed >= 1
            else str(session.speed)
        )
        total = len(session.log.entries) if session.log else 0
        self.progress.set_fraction(session.cursor / total if total else 0)
        self.progress.set_text(
            f"{session.position:.1f} / {session.log.duration:.1f} s"
            if session.log
            else "No log loaded"
        )
        if session.loading:
            status = "Loading NMEA log…"
        elif session.error:
            status = f"Could not load log: {session.error}"
        elif session.source == "live":
            status = (
                "Streaming live connections"
                if session.streaming
                else "Live stream paused"
            )
        elif session.log:
            status = (
                f"{os.path.basename(session.log.path)}\n"
                f"{total:,} sentences · {session.log.skipped:,} skipped"
            )
            if not session.log.timestamped:
                status += "\nNo timestamps: playback uses 10 sentences/s."
        else:
            status = "Load an interpreted NMEA log to play it."
        self.status.set_text(status)
        self._syncing = False
        return False

    def _source_changed(self, control):
        if not self._syncing:
            self.session.select_source("live" if control.get_active() == 0 else "log")

    def _stream_changed(self, control):
        if not self._syncing:
            self.session.set_streaming(control.get_active())

    def _play_changed(self, control):
        if not self._syncing:
            self.session.set_playing(control.get_active())

    def _speed_changed(self, control):
        if not self._syncing and control.get_active_id():
            self.session.speed = float(control.get_active_id())
            self.session.dispatch("changed", None)

    def load_path(self, path):
        token = self.session.begin_load()
        session = self.session

        def read():
            try:
                log, error = read_nmea_log(path), None
            except (OSError, ValueError) as exc:
                log, error = None, exc
            GLib.idle_add(session.finish_load, token, log, error)

        Thread(target=read, daemon=True).start()

    def _load(self, _):
        dialog = Gtk.FileChooserDialog(
            title="Load NMEA log",
            transient_for=self.parent_window,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK
        )
        file_filter = Gtk.FileFilter()
        file_filter.set_name("NMEA logs (*.log, *.nmea, *.txt)")
        for pattern in ("*.log", "*.nmea", "*.txt"):
            file_filter.add_pattern(pattern)
        dialog.add_filter(file_filter)
        response = dialog.run()
        path = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.load_path(path)

    def _destroy(self, _):
        self._alive = False
        self.session.disconnect("changed", self._changed)
