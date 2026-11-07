"""Offline place search box backed by QCompleter."""

from PySide6.QtCore import QEvent, QModelIndex, QTimer, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCompleter,
    QHBoxLayout,
    QLineEdit,
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

        # QCompleter owns a top-level popup and keeps keyboard focus in the
        # line edit while it is visible. A plain Qt.Popup QListWidget grabs the
        # keyboard instead, so typing stopped after the first result appeared.
        self.model = QStandardItemModel(self)
        self.completer = QCompleter(self.model, self)
        self.completer.setCompletionMode(
            QCompleter.CompletionMode.UnfilteredPopupCompletion
        )
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setMaxVisibleItems(12)
        self.completer.activated[QModelIndex].connect(self._activated)
        self.completer.popup().setObjectName("placeSearchResults")
        self.completer.popup().setMinimumWidth(380)
        self.edit.setCompleter(self.completer)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._refresh)
        self.edit.textChanged.connect(self._text_changed)

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
        self.model.clear()
        for place in self._places:
            item = QStandardItem(place.label)
            item.setData(place, Qt.ItemDataRole.UserRole)
            item.setToolTip(place.label)
            self.model.appendRow(item)
        if not self._places:
            self.completer.popup().hide()
            return
        self.completer.complete()

    def _activated(self, index):
        place = index.data(Qt.ItemDataRole.UserRole) if index is not None else None
        if place is not None:
            self._select(place)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual name
        if watched is self.edit and event.type() == QEvent.Type.KeyPress:
            popup = self.completer.popup()
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and popup.isVisible():
                index = popup.currentIndex()
                if not index.isValid():
                    index = self.model.index(0, 0)
                if index.isValid():
                    self._activated(index)
                    return True
            if key == Qt.Key.Key_Escape and popup.isVisible():
                popup.hide()
                return True
        return super().eventFilter(watched, event)

    def _select(self, place):
        self.completer.popup().hide()
        self._suppress = True
        try:
            self.edit.setText(place.name)
            self.edit.setCursorPosition(len(place.name))
        finally:
            self._suppress = False
        self.place_selected.emit(place)
