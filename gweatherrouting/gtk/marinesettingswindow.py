"""Marine source and prediction controls opened directly from chart palettes."""

from .marinemanagerwindow import MarineManagerWindow
from .icons import TIDES_CURRENTS_ICON, icon_label


class MarineSettingsWindow(MarineManagerWindow):
    def __init__(self, manager, planner, parent=None, core=None, map_widget=None):
        super().__init__(
            manager,
            parent,
            core=core,
            map_widget=map_widget,
            planner=planner,
            sources_only=True,
        )
        self.set_title("Marine settings")
        self.sections.set_tab_label(
            self.spatial_panel, icon_label("Currents", TIDES_CURRENTS_ICON)
        )
        self.sections.set_tab_label(
            self.sections.get_nth_page(1), icon_label("Tides", TIDES_CURRENTS_ICON)
        )
        manager.connect("changed", self._station_sources_changed)
        manager.fields.connect("changed", self._current_sources_changed)
        self.connect("destroy", self._cleanup_sources)

    def _station_sources_changed(self, *args):
        self._reload_datasets(self._selected_id())

    def _current_sources_changed(self, *args):
        self.spatial_panel._reload()

    def _cleanup_sources(self, *args):
        self.manager.disconnect("changed", self._station_sources_changed)
        self.manager.fields.disconnect("changed", self._current_sources_changed)

    def select_tab(self, tab):
        self.sections.set_current_page(0 if tab == "currents" else 1)
