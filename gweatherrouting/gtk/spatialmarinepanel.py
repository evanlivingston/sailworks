"""Route/area coverage is the default; individual stations remain optional."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from gweatherrouting.core.marine.models import utc_time
from .widgets.coveragetarget import CoverageTarget
from .widgets.downloadstart import DownloadStartEntry
from .widgets.rowinfo import HoveredRowInfo
from gweatherrouting.core.marine.presentation import source_intervals


class SpatialMarinePanel(Gtk.Box):
    def __init__(  # noqa: C901
        self,
        owner,
        core=None,
        map_widget=None,
        planner=None,
        compact=False,
        sources_only=False,
    ):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=0 if compact else 12
        )
        self.compact = compact
        self.owner = owner
        self.core = core
        self.store = owner.manager.fields
        self.planner = planner or owner.manager.coverage_planner
        self.map = map_widget
        self.routes = []
        if core:
            self.routes = list(core.trackManager) + list(core.routingManager)
        if sources_only:
            self.status = Gtk.Label(xalign=0, wrap=True)
            self.pack_start(self.status, False, False, 0)
            self._build_sources(False)
            self._reload()
            return
        note = Gtk.Label(
            label="Choose a route or map area. Current models are selected automatically.\n"
            "Regional forecasts take priority over worldwide coverage. "
            "Control visibility in the Tides & currents panel. "
            "Enable current-aware routing in the routing dialog.",
            xalign=0,
        )
        note.set_line_wrap(True)
        if not compact:
            self.pack_start(note, False, False, 0)
        self.target = CoverageTarget(map_widget, core, self._choose_area)
        self.route = self.target.route
        self.routes = self.target.routes
        self.pack_start(self.target, False, False, 0)
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Start (UTC)"), False, False, 0)
        self.start = DownloadStartEntry(getattr(owner, "clock", None))
        row.pack_start(self.start, compact, compact, 0)
        if compact:
            self.pack_start(row, False, False, 0)
            row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Hours"), False, False, 0)
        self.hours = Gtk.SpinButton.new_with_range(1, 168, 1)
        self.hours.set_value(24)
        row.pack_start(self.hours, False, False, 0)
        self.download = Gtk.Button(label="Download data")
        self.download.connect("clicked", self._download)
        if compact:
            self.pack_start(row, False, False, 0)
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row.pack_start(self.download, False, False, 0)
        self.pack_start(row, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        self.pack_start(self.status, False, False, 0)
        self._build_sources(compact)
        self.preview_button = Gtk.Button(label="Preview currents at start")
        self.preview_button.connect("clicked", self._preview)
        if not compact:
            self.pack_start(self.preview_button, False, False, 0)
        self.preview = Gtk.TextView(
            editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR
        )
        if not compact:
            self._scroll(self.preview, 100)
        if compact:
            for cell in self.route.get_cells():
                cell.set_property("ellipsize", 3)
                cell.set_property("max-width-chars", 24)
        self._choose_area()
        self._reload()
        if self.store.errors:
            self.status.set_text("\n".join(self.store.errors))

    def _build_sources(self, compact):
        self.sources = Gtk.TreeStore(bool, str, str, str, str, str, bool)
        view = self.source_view = Gtk.TreeView(model=self.sources)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._toggle)
        view.append_column(Gtk.TreeViewColumn("Use", toggle, active=0, inconsistent=6))
        for index, title in enumerate(
            ("Downloaded model", "Resolution", "Valid interval (UTC)"), 1
        ):
            if not compact or index == 1:
                renderer = Gtk.CellRendererText()
                if compact:
                    renderer.set_property("ellipsize", 3)
                    renderer.set_property("width-chars", 24)
                view.append_column(Gtk.TreeViewColumn(title, renderer, text=index))
        view.get_column(1).set_expand(True)
        view.set_expander_column(view.get_column(1))
        self.row_info = HoveredRowInfo(view, self._model_description)
        view.get_selection().connect("changed", self._source_selected)
        self._scroll(view, 150)
        remove = self.remove_button = Gtk.Button(label="Remove selected download")
        remove.set_sensitive(False)
        remove.set_tooltip_text("Expand a model and select a download to remove it.")
        remove.connect("clicked", self._remove)
        self.pack_start(remove, False, False, 0)

    def refresh_routes(self):
        removed = self.target.refresh(self.core)
        self.routes = self.target.routes
        if removed:
            self._choose_area()

    def _scroll(self, widget, height):
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(height)
        scroll.add(widget)
        self.pack_start(scroll, not self.compact, not self.compact, 0)

    def _area(self):
        return self.area

    def _choose_area(self, *args):
        self.area = self.target.area()

    def _download(self, button):
        try:
            if self.compact and self.map and self.route.get_active() == 0:
                self._choose_area()
            area = self._area()
            start = datetime.strptime(self.start.get_text(), "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError as error:
            self.status.set_text(
                "Check the area and UTC date (YYYY-MM-DD HH:MM): " + str(error)
            )
            self.owner.status.set_text(self.status.get_text())
            return
        end = start + timedelta(hours=self.hours.get_value_as_int())

        def complete(result):
            fields, issues = result
            for field in fields:
                self.store.add(field)
            self._reload()
            self.owner.status.set_text(
                "Saved %d spatial datasets for offline use.\n" % len(fields)
                + "\n".join(issues)
            )
            self.status.set_text(
                "No new coverage downloaded. Preview uses previously saved sources."
                if not fields
                else ""
            )
            self._preview(None)

        self.status.set_text("")
        self.owner._job(
            lambda cancel, progress: self.planner.download(
                area, start, end, cancel, progress
            ),
            complete,
            "Downloading current coverage…",
            with_progress=True,
        )

    @staticmethod
    def _group_key(manifest):
        return json.dumps(
            [manifest["provider"], manifest["model"], manifest["resolution_m"]]
        )

    _intervals = staticmethod(source_intervals)

    def _reload(self):
        self.row_info.clear()
        expanded = set()
        for row in self.sources:
            if self.source_view.row_expanded(row.path):
                expanded.add(row[5])
        model, selected = self.source_view.get_selection().get_selected()
        selected_key = (model[selected][4], model[selected][5]) if selected else None
        groups: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
        for field in self.store.fields.values():
            manifest = field.manifest()
            groups.setdefault(self._group_key(manifest), []).append(
                (field.id, manifest)
            )
        self._groups = {
            key: tuple(identifier for identifier, _ in values)
            for key, values in groups.items()
        }
        self.sources.clear()
        for key, values in sorted(
            groups.items(),
            key=lambda item: (
                item[1][0][1]["resolution_m"],
                item[1][0][1]["model"],
                item[0],
            ),
        ):
            manifests = [m for _, m in values]
            manifest = manifests[0]
            enabled = sum(
                identifier not in self.store.excluded for identifier, _ in values
            )
            intervals = self._intervals(manifests)
            resolution = "%g m" % manifest["resolution_m"]
            name = manifest["model"]
            summary = "%d download%s · %s" % (
                len(values),
                "" if len(values) == 1 else "s",
                resolution,
            )
            parent = self.sources.append(
                None,
                (
                    enabled == len(values),
                    name + "\n" + summary + "\n" + intervals if self.compact else name,
                    resolution,
                    intervals,
                    "",
                    key,
                    0 < enabled < len(values),
                ),
            )
            if selected_key == ("", key):
                self.source_view.get_selection().select_iter(parent)
            for identifier, manifest in sorted(
                values,
                key=lambda value: (
                    value[1]["valid_start"],
                    value[1].get("run") or value[1].get("retrieved_at", ""),
                    value[0],
                ),
                reverse=True,
            ):
                interval = self._intervals([manifest])
                run = manifest.get("run") or manifest.get("retrieved_at")
                run_label = (
                    "Run " + utc_time(run).strftime("%d %b %H:%M UTC")
                    if run
                    else "Run unknown"
                )
                name = (
                    (interval if self.compact else "Download " + identifier[:8])
                    + "\n"
                    + run_label
                    + " · "
                    + identifier[:8]
                )
                child = self.sources.append(
                    parent,
                    (
                        identifier not in self.store.excluded,
                        name,
                        resolution,
                        interval,
                        identifier,
                        key,
                        False,
                    ),
                )
                if selected_key == (identifier, key):
                    self.source_view.expand_row(self.sources.get_path(parent), False)
                    self.source_view.get_selection().select_iter(child)
            if key in expanded:
                self.source_view.expand_row(self.sources.get_path(parent), False)

    def _toggle(self, renderer, path):
        row = self.sources[path]
        identifiers = (row[4],) if row[4] else self._groups[row[5]]
        enabled = not row[0]
        try:
            for identifier in identifiers:
                self.store.enable(identifier, enabled)
            self._reload()
        except (OSError, ValueError) as error:
            self.status.set_text(str(error))

    def _remove(self, button):
        model, iterator = self.source_view.get_selection().get_selected()
        if iterator is not None and model[iterator][4]:
            try:
                self.store.remove(model[iterator][4])
                self._reload()
            except (OSError, ValueError) as error:
                self.status.set_text(str(error))

    def _source_selected(self, selection):
        model, iterator = selection.get_selected()
        self.remove_button.set_sensitive(
            iterator is not None and bool(model[iterator][4])
        )

    def _model_description(self, row):
        identifier, key = row[4], row[5]
        identifiers = (identifier,) if identifier else self._groups[key]
        manifests = [self.store.fields[i].manifest() for i in identifiers]
        manifest = manifests[0]
        descriptions = {
            "CIOPS-SalishSea": (
                "Regional ocean-current forecasts for the Salish Sea and "
                "nearby waters, including tides."
            ),
            "CIOPS-West": (
                "Regional ocean-current forecasts for the broader Canadian "
                "west coast, including tides."
            ),
            "SMOC circulation + tides": (
                "Worldwide ocean circulation combined with tidal "
                "currents. Wave-induced Stokes drift is excluded."
            ),
        }
        text = [
            descriptions.get(
                manifest["model"], manifest.get("composition", "Ocean-current model.")
            )
        ]
        text.append("Provider: " + manifest.get("attribution", manifest["provider"]))
        text.append("Grid spacing: %g m" % manifest["resolution_m"])
        if "depth_m" in manifest:
            text.append("Depth: %g m" % manifest["depth_m"])
        text.append("Downloaded intervals (UTC):\n" + self._intervals(manifests))
        if identifier:
            run = manifest.get("run") or manifest.get("retrieved_at")
            if run:
                text.append(
                    "Forecast run: " + utc_time(run).strftime("%d %b %Y %H:%M UTC")
                )
            area = manifest.get("requested_area")
            if area:
                text.append(
                    "Downloaded area: %g° to %g° latitude; %g° to %g° longitude"
                    % (area["south"], area["north"], area["west"], area["east"])
                )
            text.append("Download: " + identifier[:12])
        else:
            text.append(
                "%d saved download%s"
                % (len(manifests), "" if len(manifests) == 1 else "s")
            )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        title = Gtk.Label(label=manifest["model"], xalign=0)
        box.pack_start(title, False, False, 0)
        body = Gtk.Label(
            label="\n\n".join(text), xalign=0, wrap=True, max_width_chars=48
        )
        box.pack_start(body, False, False, 0)
        return box

    def _preview(self, button):
        try:
            area = self._area()
            start = datetime.strptime(self.start.get_text(), "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return
        index = self.route.get_active()
        points = (
            self.routes[index - 1].to_list()
            if index > 0
            else [
                (
                    (area.south + area.north) / 2,
                    (
                        (
                            area.west
                            + (area.east + (360 if area.east < area.west else 0))
                        )
                        / 2
                        + 180
                    )
                    % 360
                    - 180,
                )
            ]
        )
        selection = self.store.snapshot()
        lines = [
            "All samples at start time; this is not a routed arrival-time forecast."
        ]
        for latitude, longitude in points:
            result = selection.sample(latitude, longitude, start)
            if result.status == "available":
                field = self.store.fields[result.dataset_id]
                value = "east %.3f, north %.3f m/s · %s" % (
                    *result.values,
                    field.manifest()["model"],
                )
            else:
                value = result.status.replace("_", " ")
            lines.append("%.5f, %.5f: %s" % (latitude, longitude, value))
        self.preview.get_buffer().set_text("\n".join(lines))
