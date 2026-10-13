"""Controls for comparing measured sailing performance with a selected polar."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .widgets.toolpanel import tool_content
from .nmeacontrols import get_nmea_session

from gweatherrouting.core.polarperformance import wind_band


class PerformanceControls(Gtk.Box):
    def __init__(self, parent, core, chart):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.parent = parent
        self.core = core
        self.chart = chart
        self.session = get_nmea_session(core)
        self._revision = None
        self._alive = True
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.pack_start(scroll, True, True, 0)
        content = tool_content()
        scroll.add(content)

        self.show_points = Gtk.CheckButton(label="Show actual performance")
        self.show_points.set_active(True)
        content.pack_start(self.show_points, False, False, 0)
        self.basis = self._combo(
            content, "Boat speed", ["Speed through water", "Speed over ground"]
        )
        self.basis.set_tooltip_text(
            "Speed through water compares boat performance directly. "
            "GPS speed over ground includes the effect of current."
        )
        self.wind = self._combo(content, "Wind speed", ["All wind speeds"])
        self.wind.set_tooltip_text(
            "Points use the nearest polar wind speed, with boundaries at midpoints. "
            "Wind speeds outside the polar's range are excluded."
        )
        clear = Gtk.Button(label="Clear samples")
        clear.connect("clicked", self._clear)
        content.pack_start(clear, False, False, 0)
        self.count = Gtk.Label(xalign=0)
        self.count.set_line_wrap(True)
        self.count.set_max_width_chars(28)
        content.pack_start(self.count, False, False, 0)

        for control in (self.show_points, self.basis, self.wind):
            control.connect(
                "toggled" if control is self.show_points else "changed", self.refresh
            )
        self.session.connect("changed", self._session_changed)
        self._timer = GLib.timeout_add(1000, self._tick)
        self.connect("destroy", self._destroy)
        self.refresh()

    @staticmethod
    def _combo(content, title, labels):
        content.pack_start(Gtk.Label(label=title, xalign=0), False, False, 0)
        combo = Gtk.ComboBoxText()
        for label in labels:
            combo.append_text(label)
        combo.set_active(0)
        content.pack_start(combo, False, False, 0)
        return combo

    def _session_changed(self, _):
        self._revision = None
        self.refresh()

    def _tick(self):
        revision, samples = self.session.active_history.snapshot()
        if revision != self._revision:
            self._revision = revision
            self.refresh(samples=samples)
        return True

    def _destroy(self, widget):
        self._alive = False
        GLib.source_remove(self._timer)
        self.session.disconnect("changed", self._session_changed)

    def polar_changed(self):
        self.wind.remove_all()
        self.wind.append_text("All wind speeds")
        polar = self.chart.polar
        if polar:
            for speed in polar.tws:
                self.wind.append_text(f"TWS {speed:g} kt")
        self.wind.set_active(0)
        self.refresh()

    def refresh(self, control=None, samples=None):
        if samples is None:
            _, samples = self.session.active_history.snapshot()
        basis = "stw" if self.basis.get_active() == 0 else "sog"
        selected = self.wind.get_active() - 1
        wind_filter = selected if selected >= 0 else None
        samples = tuple(p for p in samples if p.basis == basis)
        winds = self.chart.polar.tws if self.chart.polar else []
        visible = tuple(
            p
            for p in samples
            if wind_band(p.tws, winds) is not None
            and (wind_filter is None or wind_band(p.tws, winds) == wind_filter)
        )
        self.count.set_text(
            f"{len(visible):,} / {len(samples):,} {basis.upper()} samples in range"
        )
        self.chart.set_performance(
            visible if self.show_points.get_active() else (), wind_filter
        )

    def save_state(self):
        wind = self.wind.get_active() - 1
        winds = self.chart.polar.tws if self.chart.polar else []
        return {
            "show_points": self.show_points.get_active(),
            "basis": "sog" if self.basis.get_active() == 1 else "stw",
            "wind": float(winds[wind]) if 0 <= wind < len(winds) else None,
        }

    def restore_state(self, state):
        if not isinstance(state, dict):
            return
        if isinstance(state.get("show_points"), bool):
            self.show_points.set_active(state["show_points"])
        if state.get("basis") in ("stw", "sog"):
            self.basis.set_active(1 if state["basis"] == "sog" else 0)
        winds = list(self.chart.polar.tws) if self.chart.polar else []
        wind = state.get("wind")
        self.wind.set_active(winds.index(wind) + 1 if wind in winds else 0)

    def _clear(self, button):
        self.session.active_history.clear()
        self.refresh()
