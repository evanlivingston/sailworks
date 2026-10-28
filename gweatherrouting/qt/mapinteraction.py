"""Application interaction handlers shared by Qt map widgets."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu
import math


class MapInteractionMixin:
    def _update_measurement_overlay(self):
        presenter = getattr(self, "_weather_presenter", None)
        for name in ("foreground", "overlay"):
            overlay = getattr(presenter, name, None)
            if overlay is not None:
                overlay.update()

    def _clear_measurement(self):
        self._measure_points = ()
        self._measure_preview_point = None
        self._measure_mode = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()
        self._update_measurement_overlay()

    def contextMenuEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        self.cancel_move()
        position = self.convert_screen_to_geographic(event.pos().x(), event.pos().y())
        menu = QMenu(self)
        poi = menu.addAction("Add POI here")
        point = menu.addAction("Add track point here")
        menu.addSeparator()
        start = menu.addAction("Set routing start here")
        destination = menu.addAction("Set routing destination here")
        selected = menu.exec(event.globalPos())
        if selected is poi or selected is point:
            self.navigation_request.emit(
                "poi" if selected is poi else "point", position.get_degrees()
            )
        if selected is start or selected is destination:
            self.route_location.emit(
                "start" if selected is start else "destination", position.get_degrees()
            )

    def begin_move(self, callback):
        self._cancel_cursor()
        self._cancel_inertia()
        self._finish_zoom_silent()
        self.move_callback = callback
        self.drag = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocus()
        self.update()

    def cancel_move(self):
        self.move_callback = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()

    def keyPressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.key() == Qt.Key.Key_I:
            self._cancel_cursor()
            self.cancel_move()
            self._measure_mode = True
            self._measure_points = ()
            self._measure_preview_point = None
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setFocus()
            self.update()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and (
            getattr(self, "_measure_mode", False)
            or getattr(self, "_measure_points", ())
        ):
            self._clear_measurement()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.move_callback is not None:
            self.cancel_move()
            event.accept()
            return
        super().keyPressEvent(event)

    def _record_measurement_click(self, x, y):
        if not getattr(self, "_measure_mode", False):
            return False
        point = self.convert_screen_to_geographic(x, y)
        self._measure_points = (
            *getattr(self, "_measure_points", ()),
            point.get_degrees(),
        )
        self._measure_preview_point = None
        if len(self._measure_points) == 2:
            self._measure_mode = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()
        self._update_measurement_overlay()
        return True

    def _update_measurement_preview(self, x, y):
        if (
            not getattr(self, "_measure_mode", False)
            or len(getattr(self, "_measure_points", ())) != 1
        ):
            return False
        self._measure_preview_point = self.convert_screen_to_geographic(
            x, y
        ).get_degrees()
        self._update_measurement_overlay()
        return True

    def _measurement_distance_km(self):
        points = getattr(self, "_measure_points", ())
        if len(points) != 2:
            return None
        return self._distance_between_km(points[0], points[1])

    @staticmethod
    def _distance_between_km(first, second):
        (lat1, lon1), (lat2, lon2) = first, second
        lat1, lat2 = math.radians(lat1), math.radians(lat2)
        dlat, dlon = lat2 - lat1, math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        return 6371.0088 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
