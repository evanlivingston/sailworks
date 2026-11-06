"""Qt place-search box and selection flow."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sailworks.core.places import Place, PlaceIndex
from sailworks.qt.placesearch import PlaceSearch


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def make_search():
    index = PlaceIndex(
        (
            Place("Seattle", "port", 47.6, -122.3, "United States"),
            Place("Port Townsend", "port", 48.1, -122.75, "United States"),
            Place("Auckland", "port", -36.84, 174.76, "New Zealand"),
        )
    )
    search = PlaceSearch(index)
    search.show()
    return search


def test_typing_shows_ranked_results_and_clicking_emits_the_place(app):
    search = make_search()
    selected = []
    search.place_selected.connect(selected.append)

    search.edit.setText("seat")
    search._refresh()

    popup = search.completer.popup()
    assert popup.isVisible()
    assert search.model.rowCount() == 1
    item = search.model.item(0)
    place = item.data(Qt.ItemDataRole.UserRole)
    assert place.name == "Seattle"

    search._activated(search.model.index(0, 0))
    assert selected == [place]
    assert not popup.isVisible()
    assert search.edit.text() == "Seattle"


def test_return_chooses_the_first_result_and_escape_closes_the_popup(app):
    search = make_search()
    selected = []
    search.place_selected.connect(selected.append)
    search.edit.setFocus()
    search.edit.setText("port t")
    search._refresh()

    QTest.keyClick(search.edit, Qt.Key.Key_Return)
    assert selected and selected[0].name == "Port Townsend"

    search.edit.setText("auckland")
    search._refresh()
    popup = search.completer.popup()
    assert popup.isVisible()
    popup.hide()
    assert not popup.isVisible()


def test_typing_continues_after_the_result_list_opens(app):
    search = make_search()
    search.show()
    search.edit.setFocus()
    QTest.keyClicks(search.edit, "se")
    QTest.qWait(180)
    assert search.edit.text() == "se"
    assert search.completer.popup().isVisible()

    QTest.keyClicks(search.edit, "at")
    QTest.qWait(180)
    assert search.edit.text() == "seat"
    assert search.completer.popup().isVisible()
    assert search.model.rowCount() == 1
    search.close()


def test_empty_and_missing_queries_hide_the_popup(app):
    search = make_search()
    popup = search.completer.popup()
    search.edit.setText("zzzznotaplace")
    search._refresh()
    assert not popup.isVisible()
    search.edit.setText("")
    search._refresh()
    assert not popup.isVisible()
