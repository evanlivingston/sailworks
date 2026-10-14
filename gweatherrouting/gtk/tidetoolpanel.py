"""Tide heights and automatic station downloads, separate from current models."""

from datetime import datetime, timedelta, timezone
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from gweatherrouting.core.marine.chs import CHSProvider
from gweatherrouting.core.marine.models import Capability, sample_station
from .maplayers.marinemaplayer import chart_instant
from gweatherrouting.core.marine.presentation import stations_in_view, download_tides
from .marinedownloadjobs import MarineDownloadJobs
from .marinesettingswindow import MarineSettingsWindow
from .widgets.toolpanel import settings_row, tool_content
from .widgets.coveragetarget import CoverageTarget
from .widgets.downloadstart import DownloadStartEntry
from .widgets.mapwidget import MapViewport


class TideToolPanel(Gtk.Box, MarineDownloadJobs):
    def __init__(
        self,
        manager,
        clock,
        map_widget,
        layer,
        provider=None,
        settings_callback=None,
        core=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.manager, self.clock, self.map, self.layer = (
            manager,
            clock,
            map_widget,
            layer,
        )
        self.core = core
        self.provider = provider or CHSProvider()
        self._advanced = None
        self.settings_callback = settings_callback
        self._signature = None
        self._init_jobs()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.pack_start(scroll, True, True, 0)
        content = tool_content()
        scroll.add(content)
        self.tide_toggle = Gtk.CheckButton(label="Show station markers")
        self.tide_toggle.set_active(layer.tides_visible)
        self.tide_toggle.connect(
            "toggled", lambda button: layer.set_display(tides=button.get_active())
        )
        self.settings_button = settings_row(
            content, self.tide_toggle, self._open_advanced
        )
        content.pack_start(Gtk.Separator(), False, False, 0)
        self.tide_coverage = self._label(content)
        self._label(content, "Water level at stations · metres above source datum")
        content.pack_start(Gtk.Separator(), False, False, 0)
        self._label(content, "Download")
        self.target = CoverageTarget(map_widget, core)
        self.route = self.target.route
        content.pack_start(self.target, False, False, 0)
        self.start = DownloadStartEntry(clock)
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Start (UTC)"), False, False, 0)
        row.pack_start(self.start, True, True, 0)
        content.pack_start(row, False, False, 0)
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Hours"), False, False, 0)
        self.hours = Gtk.SpinButton.new_with_range(1, 168, 1)
        self.hours.set_value(24)
        row.pack_start(self.hours, False, False, 0)
        content.pack_start(row, False, False, 0)
        self.download = Gtk.Button(label="Download data")
        self.download.connect("clicked", self._download)
        content.pack_start(self.download, False, False, 0)
        content.pack_start(Gtk.Separator(), False, False, 0)
        self._label(content, "Saved station predictions")
        self.sources = Gtk.ListStore(bool, str, str, str, str)
        self.source_view = Gtk.TreeView(model=self.sources)
        self.source_view.set_tooltip_column(4)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._toggle_source)
        self.source_view.append_column(Gtk.TreeViewColumn("Use", toggle, active=0))
        renderer = Gtk.CellRendererText(ellipsize=3, width_chars=18)
        self.source_view.append_column(Gtk.TreeViewColumn("Station", renderer, text=1))
        self.source_view.append_column(
            Gtk.TreeViewColumn("Height", Gtk.CellRendererText(), text=2)
        )
        saved = Gtk.ScrolledWindow(min_content_height=130)
        saved.add(self.source_view)
        content.pack_start(saved, False, False, 0)
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
        self._busy_controls = (self.download,)
        clock.connect("time_change", self._invalidate)
        manager.connect("changed", self._invalidate)
        self._poll_timer = GLib.timeout_add(250, self.refresh)
        self.connect("destroy", self._cleanup)
        self.refresh()

    @staticmethod
    def _label(box, text=""):
        label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=32)
        box.pack_start(label, False, False, 0)
        return label

    def _invalidate(self, *args):
        self._signature = None
        self.refresh()

    def refresh(self):
        self.target.refresh(self.core)
        instant = chart_instant(self.clock.time)
        signature = (
            self.map._center_lat,
            self.map._center_lon,
            self.map._zoom,
            self.map.get_allocated_width(),
            self.map.get_allocated_height(),
            instant,
        )
        if signature == self._signature:
            return True
        self._signature = signature
        selection = self.manager.snapshot(Capability.WATER_LEVEL)
        stations = {d.station.id: d.station for d in selection.datasets}
        visible = stations_in_view(stations.values(), self.map)
        available = sum(
            selection.sample(s.id, instant).status == "available" for s in visible
        )
        coverage = (
            "%d of %d stations in view have predictions" % (available, len(visible))
            if visible
            else "No downloaded tide predictions in view"
        )
        self.tide_coverage.set_markup(
            '<span foreground="%s">●</span> %s'
            % ("#36965c" if available else "#bd862e", coverage)
        )
        self.sources.clear()
        for dataset in self.manager.ordered_datasets():
            if Capability.WATER_LEVEL not in dataset.capabilities:
                continue
            result = sample_station(dataset, Capability.WATER_LEVEL, instant)
            self.sources.append(
                (
                    self.manager.is_enabled(dataset.id),
                    dataset.station.name
                    + "\n"
                    + dataset.requested_start.strftime("%d %b %H:%M")
                    + " → "
                    + dataset.requested_end.strftime("%d %b %H:%M UTC"),
                    (
                        "%.2f m" % result.values[0]
                        if result.status == "available"
                        else "—"
                    ),
                    dataset.id,
                    dataset.provider
                    + " · "
                    + dataset.requested_start.isoformat()
                    + " → "
                    + dataset.requested_end.isoformat(),
                )
            )
        return True

    def _toggle_source(self, renderer, path):
        row = self.sources[path]
        try:
            self.manager.enable(row[3], not row[0])
        except (OSError, ValueError) as error:
            self.status.set_text(str(error))

    def _download(self, button):  # noqa: C901
        try:
            start = datetime.strptime(self.start.get_text(), "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            self.status.set_text("Enter the UTC start as YYYY-MM-DD HH:MM.")
            return
        try:
            # Freeze the route bounds on the GTK thread along with the map view.
            self.target.refresh(self.core)
            route_area = self.target.area() if self.route.get_active() > 0 else None
        except ValueError as error:
            self.status.set_text(str(error))
            return
        end = start + timedelta(hours=self.hours.get_value_as_int())
        # Freeze the view on the GTK thread; the worker never reads live widgets.
        view = MapViewport(self.map)
        cached = tuple(self.manager.stations)

        def operation(cancel, progress):
            return download_tides(
                self.manager,
                self.provider,
                view,
                route_area,
                cached,
                start,
                end,
                cancel,
                progress,
            )

        def complete(result):
            stations, datasets, issues, reused = result
            self.manager.store_stations(stations)
            for dataset in datasets:
                if dataset.id not in self.manager.datasets:
                    self.manager.add(dataset)
                self.manager.enable(dataset.id)
            self.status.set_text(
                "Saved %d tide station predictions; reused %d already downloaded.\n%s"
                % (len(datasets) - reused, reused, "\n".join(issues))
            )
            self._invalidate()

        self._job(
            operation,
            complete,
            "Finding tide stations in the selected area…",
            with_progress=True,
        )

    def _open_advanced(self, button):
        if self.settings_callback:
            self.settings_callback("tides")
            return
        if self._advanced is None:
            self._advanced = MarineSettingsWindow(
                self.manager,
                self.manager.coverage_planner,
                self.get_toplevel(),
                map_widget=self.map,
            )
            self._advanced.connect("destroy", self._advanced_closed)
            self._advanced.show_all()
        self._advanced.select_tab("tides")
        self._advanced.present()

    def _advanced_closed(self, *args):
        self._advanced = None

    def _cleanup(self, *args):
        GLib.source_remove(self._poll_timer)
        self.clock.disconnect("time_change", self._invalidate)
        self.manager.disconnect("changed", self._invalidate)
        if self._advanced:
            self._advanced.destroy()
