"""Chart-facing marine controls, coverage status and downloads in a tool panel."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from gweatherrouting.core.marine.spatialproviders import CIOPS_DOMAINS
from .maplayers.marinemaplayer import chart_instant
from .marinedownloadjobs import MarineDownloadJobs
from .marinesettingswindow import MarineSettingsWindow
from .widgets.toolpanel import settings_row, tool_content
from .spatialmarinepanel import SpatialMarinePanel


def has_current_provider(manager, latitude, longitude):
    regional = any(
        bounds.south <= latitude <= bounds.north
        and bounds.west <= longitude <= bounds.east
        for _, _, _, _, bounds in CIOPS_DOMAINS.values()
    )
    return regional or bool(manager.coverage_planner.worldwide.credentials)


def no_current_provider():
    return (
        False,
        "No provider for current area",
        "Worldwide currents can be configured in Settings → Currents.",
    )


def current_coverage(manager, latitude, longitude, instant):
    selection = manager.fields.snapshot()
    result = selection.sample(latitude, longitude, instant)
    if result.status == "available":
        field = manager.fields.fields[result.dataset_id]
        return (
            True,
            "Current data available",
            field.manifest()["model"],
        )
    if not selection.fields:
        if not has_current_provider(manager, latitude, longitude):
            return no_current_provider()
        return (
            False,
            "No enabled current models",
            "Download data or enable a saved source.",
        )
    overlapping = [
        f
        for f in selection.fields
        if f.latitudes[0] <= latitude <= f.latitudes[-1]
        and f.longitudes[0] <= longitude <= f.longitudes[-1]
    ]
    if not overlapping:
        if not has_current_provider(manager, latitude, longitude):
            return no_current_provider()
        return False, "No current coverage here", "Download data for this map area."
    if all(not f.times[0] <= instant.timestamp() <= f.times[-1] for f in overlapping):
        return (
            False,
            "Outside the loaded forecast times",
            "Choose a loaded time or download this interval.",
        )
    return (
        False,
        "No valid model current here",
        "The model masks this location or has missing data.",
    )


class MarineToolPanel(Gtk.Box, MarineDownloadJobs):
    def __init__(self, manager, clock, map_widget, layer, core=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.manager, self.clock, self.map, self.layer = (
            manager,
            clock,
            map_widget,
            layer,
        )
        self._advanced = None
        self.core = core
        self._signature = None
        self._area_signature = None
        self._init_jobs()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.pack_start(scroll, True, True, 0)
        content = tool_content()
        scroll.add(content)
        self.current_toggle = Gtk.CheckButton(label="Current arrows")
        self.current_toggle.set_active(layer.currents_visible)
        self.current_toggle.connect(
            "toggled", lambda button: layer.set_display(currents=button.get_active())
        )
        self.settings_button = settings_row(
            content, self.current_toggle, self._stations
        )
        content.pack_start(Gtk.Separator(), False, False, 0)
        status_row = Gtk.Box(spacing=6)
        self.coverage_icon = Gtk.Label()
        status_row.pack_start(self.coverage_icon, False, False, 0)
        self.coverage = Gtk.Label(xalign=0, wrap=True)
        status_row.pack_start(self.coverage, True, True, 0)
        content.pack_start(status_row, False, False, 0)
        self.coverage_detail = self._label(content)
        content.pack_start(Gtk.Separator(), False, False, 0)
        content.pack_start(Gtk.Label(label="Download", xalign=0), False, False, 0)
        self.spatial_panel = SpatialMarinePanel(self, core, map_widget, compact=True)
        content.pack_start(self.spatial_panel, False, False, 0)
        activity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.pack_start(activity, False, False, 0)
        self.status = self._label(activity)
        self.status.set_no_show_all(True)
        self.status.connect(
            "notify::label",
            lambda label, prop: label.set_visible(bool(label.get_text())),
        )
        self.progress_row = Gtk.Box(spacing=6, no_show_all=True)
        self.progress = Gtk.ProgressBar()
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", self._cancel_download)
        self.progress_row.pack_start(self.progress, True, True, 0)
        self.progress_row.pack_start(self.cancel_button, False, False, 0)
        activity.pack_start(self.progress_row, False, False, 0)
        self.progress.show()
        self.cancel_button.show()
        self._busy_controls = (self.spatial_panel.download,)
        self.clock.connect("time_change", self._invalidate)
        manager.connect("changed", self._sources_changed)
        manager.fields.connect("changed", self._sources_changed)
        # Map gestures have no viewport signal; only sample when its signature changes.
        self._poll_timer = GLib.timeout_add(250, self.refresh)
        self.connect("destroy", self._cleanup)
        self.refresh()

    @staticmethod
    def _label(box):
        label = Gtk.Label(xalign=0, wrap=True)
        label.set_max_width_chars(34)
        label.set_selectable(True)
        box.pack_start(label, False, False, 0)
        return label

    def _invalidate(self, *args):
        self._signature = None
        self.refresh()

    def _sources_changed(self, *args):
        self.spatial_panel._reload()
        self._invalidate()

    def refresh(self):
        self.spatial_panel.refresh_routes()
        lat, lon = self.map._center_lat, (self.map._center_lon + 180) % 360 - 180
        instant = chart_instant(self.clock.time)
        signature = (
            lat,
            lon,
            self.map._zoom,
            self.map.get_allocated_width(),
            self.map.get_allocated_height(),
            bool(self.manager.coverage_planner.worldwide.credentials),
            instant,
        )
        view = signature[:-1]
        if (
            view != self._area_signature
            and not self._busy
            and self.map.get_allocated_width() > 1
            and self.map.get_allocated_height() > 1
            and self.spatial_panel.route.get_active() == 0
        ):
            self.spatial_panel._choose_area()
            self._area_signature = view
        if signature == self._signature:
            return True
        self._signature = signature
        available, title, detail = current_coverage(self.manager, lat, lon, instant)
        self.coverage.set_text(title)
        self.coverage_detail.set_text(detail)
        self.coverage_icon.set_markup(
            '<span foreground="%s">●</span>' % ("#36965c" if available else "#bd862e")
        )
        return True

    def _stations(self, button):
        self.open_settings("currents")

    def open_settings(self, tab):
        if self._advanced is None:
            self._advanced = MarineSettingsWindow(
                self.manager,
                self.spatial_panel.planner,
                self.get_toplevel(),
                core=self.core,
                map_widget=self.map,
            )
            self._advanced.connect("destroy", self._advanced_closed)
            self._advanced.show_all()
        self._advanced.select_tab(tab)
        self._advanced.present()

    def _advanced_closed(self, *args):
        self._advanced = None

    def _cleanup(self, *args):
        GLib.source_remove(self._poll_timer)
        self.clock.disconnect("time_change", self._invalidate)
        self.manager.disconnect("changed", self._sources_changed)
        self.manager.fields.disconnect("changed", self._sources_changed)
        if self._advanced:
            self._advanced.destroy()
