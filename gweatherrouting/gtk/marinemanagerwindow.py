"""Offline station predictions and explicit marine source selection."""

import json
from datetime import datetime, timedelta, timezone

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from gweatherrouting.core.marine.chs import CHSProvider
from gweatherrouting.core.marine.models import Capability
from .marinedownloadjobs import MarineDownloadJobs
from .spatialmarinepanel import SpatialMarinePanel


class MarineManagerWindow(Gtk.Window, MarineDownloadJobs):
    def __init__(
        self,
        manager,
        parent=None,
        provider=None,
        core=None,
        map_widget=None,
        planner=None,
        sources_only=False,
    ):
        super().__init__(title="Tides & currents")
        if parent:
            self.set_transient_for(parent)
        self.set_default_size(980, 720)
        self.manager = manager
        self.provider = provider or CHSProvider()
        self._init_jobs()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        sections = self.sections = Gtk.Notebook()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        root.pack_start(sections, True, True, 0)
        self.spatial_panel = SpatialMarinePanel(
            self, core, map_widget, planner=planner, sources_only=sources_only
        )
        sections.append_page(
            self.spatial_panel, Gtk.Label(label="Route / area currents")
        )
        sections.append_page(box, Gtk.Label(label="Individual stations (advanced)"))
        note = Gtk.Label(
            label="CHS Pacific station predictions · Available offline after download\n"
            "These selections are saved for marine queries; station predictions "
            "do not describe currents along a route. Routing uses spatial current models.",
            xalign=0,
        )
        note.set_line_wrap(True)
        box.pack_start(note, False, False, 0)

        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.set_sensitive(False)
        self.cancel_button.connect("clicked", self._cancel_download)
        if not sources_only:
            row = Gtk.Box(spacing=8)
            self.search = Gtk.SearchEntry(
                placeholder_text="Find station by name or code"
            )
            self.search.connect("search-changed", self._filter_stations)
            row.pack_start(self.search, True, True, 0)
            self.refresh_button = Gtk.Button(label="Refresh CHS stations")
            self.refresh_button.connect("clicked", self._refresh_stations)
            row.pack_start(self.refresh_button, False, False, 0)
            box.pack_start(row, False, False, 0)

            self.station_store = Gtk.ListStore(str, str, str)
            self.station_view = Gtk.TreeView(model=self.station_store)
            for index, title in enumerate(("Code", "Station", "Available series")):
                self.station_view.append_column(
                    Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=index)
                )
            self._scroll(box, self.station_view, 125, False)

            row = Gtk.Box(spacing=8)
            row.pack_start(Gtk.Label(label="Start date (UTC)"), False, False, 0)
            self.start = Gtk.Entry(text=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            self.start.set_width_chars(10)
            row.pack_start(self.start, False, False, 0)
            row.pack_start(Gtk.Label(label="Days"), False, False, 0)
            self.days = Gtk.SpinButton.new_with_range(1, 7, 1)
            row.pack_start(self.days, False, False, 0)
            self.download_button = Gtk.Button(label="Download predictions")
            self.download_button.connect("clicked", self._download)
            row.pack_start(self.download_button, False, False, 0)
            box.pack_start(row, False, False, 0)

        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        activity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        root.pack_start(activity, False, False, 0)
        activity.pack_start(self.status, False, False, 0)
        self.progress_row = Gtk.Box(spacing=8, no_show_all=True)
        self.progress = Gtk.ProgressBar()
        self.progress_row.pack_start(self.progress, True, True, 0)
        self.progress_cancel = self.cancel_button
        self.progress_row.pack_start(self.progress_cancel, False, False, 0)
        activity.pack_start(self.progress_row, False, False, 0)
        self.progress.show()
        self.progress_cancel.show()
        box.pack_start(
            Gtk.Label(
                label="Downloaded sources · priority from top to bottom", xalign=0
            ),
            False,
            False,
            0,
        )
        self.dataset_store = Gtk.ListStore(bool, str, str, str, str)
        self.dataset_view = Gtk.TreeView(model=self.dataset_store)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._toggle)
        self.dataset_view.append_column(Gtk.TreeViewColumn("Use", toggle, active=0))
        for index, title in enumerate(
            ("Source / station", "UTC interval", "Capabilities"), 1
        ):
            self.dataset_view.append_column(
                Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=index)
            )
        self.dataset_view.get_selection().connect("changed", self._show_details)
        self._scroll(box, self.dataset_view, 110, False)
        row = Gtk.Box(spacing=8)
        for title, callback in (
            ("Move up", lambda button: self._move(-1)),
            ("Move down", lambda button: self._move(1)),
            ("Remove download", self._remove),
        ):
            button = Gtk.Button(label=title)
            button.connect("clicked", callback)
            row.pack_start(button, False, False, 0)
        self.fallback = Gtk.CheckButton(
            label="Allow fallback to a lower priority source"
        )
        self.fallback.set_active(manager.allow_fallback)
        self.fallback.connect("toggled", self._set_fallback)
        row.pack_start(self.fallback, False, False, 0)
        box.pack_start(row, False, False, 0)
        notebook = Gtk.Notebook()
        box.pack_start(notebook, True, True, 0)
        self.sample_store = Gtk.ListStore(str, str, str, str, str, str)
        samples = Gtk.TreeView(model=self.sample_store)
        for index, title in enumerate(
            ("Time (UTC)", "Series", "Value", "Unit", "QC", "Event")
        ):
            samples.append_column(
                Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=index)
            )
        predictions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._scroll(predictions, samples, 150, True)
        notebook.append_page(predictions, Gtk.Label(label="Predictions (UTC)"))
        self.details = Gtk.TextView(editable=False, monospace=True)
        self.details.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._scroll(details, self.details, 150, True)
        notebook.append_page(details, Gtk.Label(label="Source details"))
        self._busy_controls = (
            ()
            if sources_only
            else (
                self.refresh_button,
                self.download_button,
                self.spatial_panel.download,
            )
        )
        if not sources_only:
            self._filter_stations()
        self._reload_datasets()
        self.status.set_text(
            "\n".join(manager.errors)
            or (
                "Select a saved source to inspect its details."
                if sources_only
                else "Choose a route or area, then download current coverage."
            )
        )

    @staticmethod
    def _scroll(box, child, height, expand):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(height)
        scroll.add(child)
        box.pack_start(scroll, expand, expand, 0)

    def _filter_stations(self, *args):
        query = self.search.get_text().strip().casefold()
        self.station_store.clear()
        for station in self.manager.stations:
            if query in (station.code + " " + station.name).casefold():
                self.station_store.append(
                    (station.code, station.name, ", ".join(station.series_codes))
                )

    def _refresh_stations(self, button):
        def complete(stations):
            self.manager.store_stations(stations)
            self._filter_stations()
            self.status.set_text(
                "Saved %d stations for offline browsing." % len(stations)
            )

        self._job(self.provider.discover, complete, "Loading CHS Pacific stations…")

    def _download(self, button):
        model, iterator = self.station_view.get_selection().get_selected()
        if iterator is None:
            self.status.set_text("Select a station first.")
            return
        station = next(s for s in self.manager.stations if s.code == model[iterator][0])
        try:
            start = datetime.strptime(self.start.get_text(), "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            self.status.set_text("Enter the UTC start date as YYYY-MM-DD.")
            return
        end = start + timedelta(days=self.days.get_value_as_int())

        def complete(dataset):
            identifier = self.manager.add(dataset)
            self._reload_datasets(identifier)
            self.status.set_text(
                "Saved for offline use. Select Use to enable this source."
            )

        self._job(
            lambda cancel: self.manager.download_station(
                self.provider,
                station,
                start,
                end,
                cancel,
                (
                    Capability.WATER_LEVEL
                    if "wlp" in station.series_codes
                    else Capability.STATION_CURRENT
                ),
            ),
            complete,
            "Downloading predictions for " + station.name + "…",
        )

    def _selected_id(self):
        model, iterator = self.dataset_view.get_selection().get_selected()
        return model[iterator][4] if iterator is not None else None

    def _reload_datasets(self, selected=None):
        self.dataset_store.clear()
        for dataset in self.manager.ordered_datasets():
            iterator = self.dataset_store.append(
                (
                    self.manager.is_enabled(dataset.id),
                    dataset.provider + " / " + dataset.station.name,
                    dataset.requested_start.strftime("%Y-%m-%d %H:%M")
                    + " → "
                    + dataset.requested_end.strftime("%Y-%m-%d %H:%M"),
                    ", ".join(c.value.replace("_", " ") for c in dataset.capabilities),
                    dataset.id,
                )
            )
            if dataset.id == selected:
                self.dataset_view.get_selection().select_iter(iterator)

    def _save_action(self, action):
        try:
            action()
        except (OSError, ValueError) as error:
            self.status.set_text("Could not save selection: " + str(error))

    def _toggle(self, renderer, path):
        row = self.dataset_store[path]
        identifier = row[4]
        self._save_action(lambda: self.manager.enable(identifier, not row[0]))
        self._reload_datasets(identifier)

    def _move(self, offset):
        identifier = self._selected_id()
        if identifier:
            self._save_action(lambda: self.manager.move(identifier, offset))
            self._reload_datasets(identifier)

    def _remove(self, button):
        identifier = self._selected_id()
        if identifier:
            self._save_action(lambda: self.manager.remove(identifier))
            self._reload_datasets()

    def _set_fallback(self, button):
        self._save_action(lambda: self.manager.set_fallback(button.get_active()))

    def _show_details(self, selection):
        identifier = self._selected_id()
        self.sample_store.clear()
        if not identifier:
            self.details.get_buffer().set_text("")
            return
        dataset = self.manager.datasets[identifier]
        rows = sorted(
            (
                sample.time,
                series.code,
                str(sample.value),
                series.unit,
                sample.qc_flag,
                sample.qualifier or "",
            )
            for series in dataset.series
            for sample in series.samples
        )
        for instant, *values in rows:
            self.sample_store.append((instant.strftime("%Y-%m-%d %H:%M"), *values))
        lines = [
            dataset.attribution,
            "Terms: " + dataset.licence_url,
            "Dataset SHA-256: " + identifier,
            "Retrieved (UTC): " + dataset.retrieved_at.isoformat(),
            "Position: %.5f, %.5f"
            % (dataset.station.latitude, dataset.station.longitude),
            "Metadata: "
            + json.dumps(json.loads(dataset.metadata_json), ensure_ascii=False),
            "Source responses: "
            + json.dumps(json.loads(dataset.sources_json), indent=2),
            "",
            "Prediction times are UTC. Current directions are degrees true, flow toward.",
            "Water-level datum and current depth remain unverified.",
        ]
        lines.append(
            "QC 2: prediction quality unevaluated. Null values are missing data."
        )
        lines.extend(
            "%s [%s] (source series %s)"
            % (series.code, series.unit, series.source_code)
            for series in dataset.series
        )
        self.details.get_buffer().set_text("\n".join(lines))
