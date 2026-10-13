import logging
import os
from threading import Thread

import gi
import requests

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

logger = logging.getLogger("gweatherrouting")

PolFileFilter = Gtk.FileFilter()
PolFileFilter.set_name("Polar file")
PolFileFilter.add_pattern("*.pol")


class PolarManagerPanel(Gtk.Box):
    def __init__(self, polar_manager, parent=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.parent_window = parent
        self._alive = True
        self._downloading = False
        self.polar_manager = polar_manager
        self.selected_orc_polar = None
        self.selected_local_polar = None

        self.builder = Gtk.Builder()
        self.builder.add_from_file(
            os.path.abspath(os.path.dirname(__file__)) + "/polarmanagerwindow.glade"
        )
        self.builder.connect_signals(self)

        self.pack_start(self.builder.get_object("polar-manager-content"), True, True, 0)

        self.orc_ListStore = self.builder.get_object("orc-list-store")
        self.orc_filter = Gtk.TreeModelFilter(child_model=self.orc_ListStore)
        self.orc_search_text = ""
        self.orc_filter.set_visible_func(self._orc_filter_func)
        self.builder.get_object("orc-treeview").set_model(self.orc_filter)

        self.polar_managerStore = self.builder.get_object("polar-manager-store")
        self.refresh_polars_tab()
        self.polar_manager.connect("polars-list-updated", self.refresh_polars_tab)
        self.connect("destroy", self._destroy)
        self._progress("Loading ORC catalog…")
        Thread(target=self.download_orc_list, daemon=True).start()

    def _destroy(self, widget):
        self._alive = False
        self.polar_manager.disconnect("polars-list-updated", self.refresh_polars_tab)

    def _deliver(self, callback, *args):
        if self._alive:
            callback(*args)
        return False

    def _queue(self, callback, *args):
        GLib.idle_add(self._deliver, callback, *args)

    def _progress(self, text):
        progress = self.builder.get_object("download-progress")
        progress.set_text(text)
        progress.set_tooltip_text(text)
        progress.show()

    def _selection_actions(self):
        available = self.selected_orc_polar is not None and not self._downloading
        for name in ("download-orc-button", "download-orc-menu-item"):
            self.builder.get_object(name).set_sensitive(available)
        for name in ("remove-polar-button", "remove-polar-menu-item"):
            self.builder.get_object(name).set_sensitive(
                self.selected_local_polar is not None
            )

    # ORC data management

    def on_orc_search_changed(self, widget):
        self.orc_search_text = widget.get_text().lower()
        self.orc_filter.refilter()

    def _orc_filter_func(self, model, tree_iter, data=None):
        if not self.orc_search_text:
            return True
        for col in range(3):
            val = model.get_value(tree_iter, col)
            if val and self.orc_search_text in val.lower():
                return True
        return False

    def on_orc_select(self, selection):
        self.selected_orc_polar = None
        store, pathlist = selection.get_selected_rows()
        if not pathlist:
            self._selection_actions()
            return
        tree_iter = store.get_iter(pathlist[0])
        if not store.get_value(tree_iter, 3):
            selection.unselect_iter(tree_iter)
            self.selected_orc_polar = None
            self._selection_actions()
            return
        self.selected_orc_polar = store.get_value(tree_iter, 0)
        self._selection_actions()

    def on_orc_click(self, widget, event):
        if event.button == 3:
            menu = self.builder.get_object("orc-list-menu")
            menu.popup(None, None, None, None, event.button, event.time)

    def on_orc_download(self, widget):
        if self.selected_orc_polar is None or self._downloading:
            return
        self._downloading = True
        polar_name = self.selected_orc_polar
        self._selection_actions()
        self._progress(f"Downloading {polar_name}…")
        Thread(target=self.download_orc, args=(polar_name,), daemon=True).start()

    # Local polar management

    def on_local_polar_click(self, widget, event):
        if event.button == 3:
            menu = self.builder.get_object("polar-manager-menu")
            menu.popup(None, None, None, None, event.button, event.time)

    def on_local_polar_select(self, selection):
        store, pathlist = selection.get_selected_rows()
        if not pathlist:
            self.selected_local_polar = None
            self._selection_actions()
            return
        tree_iter = store.get_iter(pathlist[0])
        self.selected_local_polar = store.get_value(tree_iter, 0)
        self._selection_actions()

    def on_polar_toggle(self, widget, i):
        n = self.polar_managerStore[i][0]
        if self.polar_manager.is_enabled(n):
            self.polar_manager.disable(n)
        else:
            self.polar_manager.enable(n)
            self.polar_manager.dispatch(
                "polars-list-updated", self.polar_manager.polars
            )

    def on_local_polar_remove(self, widget):
        if self.selected_local_polar is not None:
            self.polar_manager.delete_polar(self.selected_local_polar)

    def download_orc_list(self):
        orc_url = "https://raw.githubusercontent.com/jieter/orc-data/refs/heads/master/site/index.json"  # noqa: E501
        try:
            r = requests.get(orc_url, timeout=30)
            r.raise_for_status()
            orc_data = r.json()
            self._queue(self._populate_orc_list, orc_data)
        except Exception:
            logger.error(f"Failed to download orc data file list from {orc_url}")
            self._queue(self._progress, "Failed to download ORC catalog")

    def _is_orc_downloaded(self, orc_entry):
        file_name = orc_entry[0].replace("/", "_") + ".pol"
        return file_name in self.polar_manager.polars_files

    def _populate_orc_list(self, orc_data):
        self.orc_ListStore.clear()
        for d in orc_data:
            selectable = not self._is_orc_downloaded(d)
            self.orc_ListStore.append([*d, selectable])
        self.builder.get_object("download-progress").hide()

    def download_orc(self, polar_name):
        try:
            self.polar_manager.download_orc_polar(polar_name, dispatch=False)
            GLib.idle_add(self._download_completed, polar_name)
        except Exception:
            logger.error(f"Failed to download orc data file {polar_name}")
            self._queue(self._on_orc_download_failed, polar_name)

    def _download_completed(self, polar_name):
        if self._alive:
            self._on_orc_download_done(polar_name)
        else:
            self.polar_manager.dispatch(
                "polars-list-updated", self.polar_manager.polars
            )
        return False

    def _on_orc_download_failed(self, polar_name):
        self._downloading = False
        self._progress(f"Failed to download {polar_name}")
        self._selection_actions()

    def _on_orc_download_done(self, polar_name):
        self._downloading = False
        self._progress(f"Downloaded {polar_name}")
        # Mark the row as no longer selectable
        for row in self.orc_ListStore:
            if row[0] == polar_name:
                row[3] = False
                break
        self.selected_orc_polar = None
        self._selection_actions()
        self.polar_manager.dispatch("polars-list-updated", self.polar_manager.polars)

    def on_open(self, widget):
        toplevel = self.get_toplevel()
        parent_window = (
            toplevel if isinstance(toplevel, Gtk.Window) else self.parent_window
        )
        dialog = Gtk.FileChooserDialog(
            "Please choose a file",
            parent_window,
            Gtk.FileChooserAction.OPEN,
            (
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN,
                Gtk.ResponseType.OK,
            ),
        )

        dialog.add_filter(PolFileFilter)

        response = dialog.run()
        filepath = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.polar_manager.add_polar_file(filepath)

    def refresh_polars_tab(self, widget=None):
        selected = self.selected_local_polar
        self.polar_managerStore.clear()
        for polar in self.polar_manager.polars_files:
            enabled = self.polar_manager.is_enabled(polar)
            meta = self.polar_manager.get_metadata(polar)
            self.polar_managerStore.append(
                [
                    polar,
                    meta.get("sail", ""),
                    meta.get("name", ""),
                    meta.get("type", ""),
                    enabled,
                ]
            )
        if selected is not None:
            for row in self.polar_managerStore:
                if row[0] == selected:
                    self.builder.get_object(
                        "local-treeview"
                    ).get_selection().select_path(row.path)
                    break
        for row in self.orc_ListStore:
            row[3] = not self._is_orc_downloaded(row)
        self.on_orc_select(self.builder.get_object("orc-treeview").get_selection())
