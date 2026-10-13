"""Shared chart-area or route selection for marine downloads."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from gweatherrouting.core.marine.fields import Area


class CoverageTarget(Gtk.Box):
    def __init__(self, map_widget, core=None, changed=None):
        super().__init__(spacing=8)
        self.map = map_widget
        self.routes = []
        self._signature = None
        self.pack_start(Gtk.Label(label="Coverage for"), False, False, 0)
        self.route = Gtk.ComboBoxText()
        for cell in self.route.get_cells():
            cell.set_property("ellipsize", 3)
            cell.set_property("max-width-chars", 24)
        self.pack_start(self.route, True, True, 0)
        self._handler = self.route.connect(
            "changed", lambda _: changed() if changed else None
        )
        self.refresh(core)

    def refresh(self, core):
        routes = list(core.trackManager) + list(core.routingManager) if core else []
        signature = tuple((id(route), route.name) for route in routes)
        if signature == self._signature:
            return False
        index = self.route.get_active()
        selected = self.routes[index - 1] if 0 < index <= len(self.routes) else None
        self.routes = routes
        self._signature = signature
        self.route.handler_block(self._handler)
        try:
            self.route.remove_all()
            self.route.append_text(
                "Current map area" if self.map else "Vancouver Island area"
            )
            for route in routes:
                self.route.append_text(route.name)
            self.route.set_active(
                next((i + 1 for i, route in enumerate(routes) if route is selected), 0)
            )
        finally:
            self.route.handler_unblock(self._handler)
        return selected is not None and self.route.get_active() == 0

    def area(self):
        index = self.route.get_active()
        if index > 0:
            return Area.from_points(self.routes[index - 1].to_list())
        if not self.map:
            return Area(-126.2, 48, -123, 50.9)
        northwest = self.map.convert_screen_to_geographic(0, 0).get_degrees()
        southeast = self.map.convert_screen_to_geographic(
            self.map.get_allocated_width(), self.map.get_allocated_height()
        ).get_degrees()
        return Area(
            (northwest[1] + 180) % 360 - 180,
            southeast[0],
            (southeast[1] + 180) % 360 - 180,
            northwest[0],
        )
