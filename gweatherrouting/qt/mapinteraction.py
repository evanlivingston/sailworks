"""Application interaction handlers shared by Qt map widgets."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu


class MapInteractionMixin:
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
        if event.key() == Qt.Key.Key_Escape and self.move_callback is not None:
            self.cancel_move()
            event.accept()
            return
        super().keyPressEvent(event)
