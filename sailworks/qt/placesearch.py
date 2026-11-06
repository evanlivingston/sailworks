"""Offline place search box with a keyboard-accessible result popup."""

from PySide6.QtCore import QEvent, QPoint, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QWidget,
)

from sailworks.core.places import PlaceIndex


class PlaceSearch(QWidget):
    """Search the local gazetteer and emit the selected place."""

    place_selected = Signal(object)

    def __init__(self, index=None, near=None, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.setMaximumWidth(380)
        self.index = index if index is not None else PlaceIndex.from_default()
        self.near = near
        self._places = []
        self._suppress = False
        self.edit = QLineEdit(self)
        self.edit.setObjectName("placeSearchEdit")
        self.edit.setPlaceholderText("Search ports, cities and chart names")
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumWidth(260)
        self.edit.setMaximumWidth(380)
        self.edit.setAccessibleName("Search places")
        self.edit.setToolTip(
            "Search ports, cities and loaded chart feature names\n"
            "Place data: NGA World Port Index (CC BY 4.0) and "
            "Natural Earth (public domain)"
        )
        self.edit.installEventFilter(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        self.results = QListWidget(self)
        self.results.setObjectName("placeSearchResults")
        self.results.setWindowFlags(Qt.WindowType.Popup)
        self.results.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.results.setMouseTracking(True)
        self.results.itemClicked.connect(self._activated)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._refresh)
        self.edit.textChanged.connect(self._text_changed)
        self.edit.returnPressed.connect(self._choose_current)

    def set_index(self, index):
        self.index = index
        if self.edit.text():
            self._refresh()

    def refresh(self):
        if self.edit.text():
            self._refresh()

    def _text_changed(self, _text):
        if not self._suppress:
            self.timer.start()

    def _near_point(self):
        if callable(self.near):
            try:
                return self.near()
            except Exception:
                return None
        return self.near

    def _refresh(self):
        text = self.edit.text().strip()
        self._places = self.index.search(text, limit=12, near=self._near_point())
        self.results.clear()
        for place in self._places:
            item = QListWidgetItem(place.label)
            item.setData(Qt.ItemDataRole.UserRole, place)
            item.setToolTip(place.label)
            self.results.addItem(item)
        if not self._places:
            self.results.hide()
            return
        below = self.edit.mapToGlobal(QPoint(0, self.edit.height() + 2))
        width = max(self.edit.width(), 380)
        height = min(320, self.results.sizeHintForRow(0) * len(self._places) + 6)
        self.results.setGeometry(below.x(), below.y(), width, height)
        self.results.show()
        self.results.raise_()

    def _selected_place(self):
        item = self.results.currentItem()
        if item is None and self.results.count():
            item = self.results.item(0)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _choose_current(self):
        place = self._selected_place()
        if place is not None:
            self._select(place)

    def _activated(self, item):
        place = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if place is not None:
            self._select(place)

    def _select(self, place):
        self.results.hide()
        self._suppress = True
        try:
            self.edit.setText(place.name)
            self.edit.setCursorPosition(len(place.name))
        finally:
            self._suppress = False
        self.place_selected.emit(place)

    def _move_selection(self, step):
        if not self.results.isVisible() or not self.results.count():
            return False
        row = self.results.currentRow()
        row = 0 if row < 0 else max(0, min(self.results.count() - 1, row + step))
        self.results.setCurrentRow(row)
        return True

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual name
        if watched is self.edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down and self._move_selection(1):
                return True
            if key == Qt.Key.Key_Up and self._move_selection(-1):
                return True
            if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
                self._choose_current()
                return True
            if key == Qt.Key.Key_Escape:
                self.results.hide()
                return False
        if watched is self.edit and event.type() == QEvent.Type.FocusOut:
            QTimer.singleShot(150, self._hide_if_unfocused)
        return super().eventFilter(watched, event)

    def _hide_if_unfocused(self):
        if (
            not self.edit.hasFocus()
            and QApplication.activePopupWidget() is not self.results
        ):
            self.results.hide()
