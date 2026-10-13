"""Views of the application's shared NMEA session."""

import os
import time
from threading import Thread

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango

from gweatherrouting.core.nmeasession import NMEASession, read_nmea_log
from .widgets.toolpanel import tool_content
from .widgets.filechooser import file_chooser


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
        transport = Gtk.Box(spacing=0)
        transport.get_style_context().add_class("linked")
        self.restart_button = self._transport_button(
            "media-skip-backward-symbolic", "Restart playback"
        )
        self.restart_button.connect("clicked", lambda *_: self.session.restart())
        self.play = self._transport_button(
            "media-playback-start-symbolic", "Play", toggle=True
        )
        self.play.connect("toggled", self._play_changed)
        self.stop_button = self._transport_button(
            "media-playback-stop-symbolic", "Stop"
        )
        self.stop_button.connect("clicked", self._stop)
        self.record_button = self._transport_button(
            "media-record-symbolic", "Record", toggle=True
        )
        self.record_button.connect("toggled", self._record)
        record_style = Gtk.CssProvider()
        record_style.load_from_data(b"""
            button { color: #e34b4b; }
            button:checked {
                background-image: none;
                background-color: alpha(#e34b4b, 0.22);
                border-color: #e34b4b;
            }
            button:disabled { color: alpha(#e34b4b, 0.4); }
        """)
        self.record_button.get_style_context().add_provider(
            record_style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        for button in (
            self.restart_button,
            self.play,
            self.stop_button,
            self.record_button,
        ):
            transport.pack_start(button, True, True, 0)
        content.pack_start(transport, False, False, 0)
        self.speed = Gtk.ComboBoxText()
        for value in (0.5, 1, 2, 5, 10):
            self.speed.append(str(value), f"{value:g}×")
        self.speed.connect("changed", self._speed_changed)
        self.playback_speed_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        self.playback_speed_box.set_no_show_all(True)
        speed_label = Gtk.Label(label="Playback speed", xalign=0)
        self.playback_speed_box.pack_start(speed_label, False, False, 0)
        self.playback_speed_box.pack_start(self.speed, False, False, 0)
        speed_label.show()
        self.speed.show()
        content.pack_start(self.playback_speed_box, False, False, 0)
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        content.pack_start(self.progress, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(28)
        content.pack_start(self.status, False, False, 0)
        self.recording_file = Gtk.Label(xalign=0)
        self.recording_file.set_selectable(True)
        self.recording_file.set_line_wrap(True)
        self.recording_file.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.recording_file.set_max_width_chars(28)
        content.pack_start(self.recording_file, False, False, 0)
        self.session.connect("changed", self._changed)
        self.connect("destroy", self._destroy)
        self.refresh()

    @staticmethod
    def _transport_button(icon, label, toggle=False):
        button = Gtk.ToggleButton() if toggle else Gtk.Button()
        button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR))
        button.set_tooltip_text(label)
        button.get_accessible().set_name(label)
        button.set_size_request(42, 36)
        return button

    def _changed(self, _):
        self.refresh()

    def refresh(self):
        if not self._alive:
            return False
        self._syncing = True
        session = self.session
        self.record_button.set_active(session.recording)
        self.record_button.set_tooltip_text(
            "Stop recording and keep capture"
            if session.recording
            else "Start a new timestamped recording"
        )
        self.stop_button.set_sensitive(session.recording or session.playing)
        stop_label = (
            "Stop recording and keep capture" if session.recording else "Stop playback"
        )
        self.stop_button.set_tooltip_text(stop_label)
        self.stop_button.get_accessible().set_name(stop_label)
        self.record_button.set_sensitive(
            session.recording
            or (session.source == "live" and session.streaming and not session.loading)
        )
        self.source.set_active(0 if session.source == "live" else 1)
        self.live.set_active(session.streaming)
        self.live.set_sensitive(session.source == "live" and not session.loading)
        self.load_button.set_sensitive(not session.loading)
        self.source.set_sensitive(not session.loading)
        self.play.set_active(session.playing)
        self.play.get_image().set_from_icon_name(
            (
                "media-playback-pause-symbolic"
                if session.playing
                else "media-playback-start-symbolic"
            ),
            Gtk.IconSize.LARGE_TOOLBAR,
        )
        self.play.set_tooltip_text("Pause" if session.playing else "Play")
        self.play.get_accessible().set_name("Pause" if session.playing else "Play")
        enabled = (
            session.log is not None and session.source == "log" and not session.loading
        )
        self.play.set_sensitive(enabled)
        self.restart_button.set_sensitive(enabled)
        self.playback_speed_box.set_visible(session.source == "log")
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
        elif session.recording:
            status = "Recording · saving automatically…"
        elif session.error:
            status = f"Could not load log: {session.error}"
        elif session.source == "live":
            live_status = (
                "Live stream continues" if session.streaming else "Live stream paused"
            )
            if session.recording_path is not None:
                status = "Recording stopped · saved automatically.\n" f"{live_status}."
            else:
                status = (
                    "Streaming live connections" if session.streaming else live_status
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
        path = session.recording_path
        self.recording_file.set_text(
            f"Recording file:\n{path}"
            if path
            else f"Recordings folder:\n{session.recording_directory}"
        )
        self.recording_file.set_tooltip_text(str(path or session.recording_directory))
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

    def _show_error(self, error):
        dialog = Gtk.MessageDialog(
            transient_for=self.parent_window,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=str(error),
        )
        dialog.run()
        dialog.destroy()

    def _stop(self, _):
        self.session.stop()

    def _record(self, control):
        if self._syncing:
            return
        try:
            if not control.get_active():
                self._stop(control)
            else:
                self.session.start_recording()
        except OSError as exc:
            self.refresh()
            self._show_error(exc)

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
        dialog = file_chooser(
            "Load NMEA log", self.parent_window, Gtk.FileChooserAction.OPEN
        )
        file_filter = Gtk.FileFilter()
        file_filter.set_name("NMEA logs (*.log, *.nmea, *.txt)")
        for pattern in ("*.log", "*.nmea", "*.txt"):
            file_filter.add_pattern(pattern)
        dialog.add_filter(file_filter)
        if self.session.recording_directory.is_dir():
            dialog.set_current_folder(str(self.session.recording_directory))
        response = dialog.run()
        path = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT and path:
            self.load_path(path)

    def _destroy(self, _):
        self._alive = False
        self.session.disconnect("changed", self._changed)
