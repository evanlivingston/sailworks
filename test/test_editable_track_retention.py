"""Selected-track drag updates mark positions without recreating QML items."""

import os
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView

SCENE = (
    Path(__file__).resolve().parents[1] / "sailworks/qt/shaders/EditableTrackLayer.qml"
)
POINTS = [[50.803, -1.430], [50.819, -1.380], [50.826, -1.315]]


def _marks(root):
    found = {}
    pending = [root]
    while pending:
        item = pending.pop()
        name = item.objectName()
        if (
            name.startswith("editableTrackMark")
            and name[len("editableTrackMark") :].isdigit()
        ):
            found[name] = item
        pending.extend(item.childItems())
    return found


def test_preview_and_highlight_retain_mark_items():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication.instance() or QGuiApplication([])
    view = QQuickView()
    view.setSource(QUrl.fromLocalFile(str(SCENE)))
    try:
        assert not view.errors(), view.errors()
        root = view.rootObject()
        track = {
            "name": "test",
            "points": POINTS,
            "active": True,
            "highlight": 1,
            "editing": True,
        }
        assert root.setProperty("tracks", [track])
        app.processEvents()
        before = _marks(root)
        assert len(before) == len(POINTS)
        position = (before["editableTrackMark1"].x(), before["editableTrackMark1"].y())

        assert root.setProperty("movePreview", [0, 1, 50.84, -1.35])
        app.processEvents()
        after = _marks(root)
        assert before == after
        assert (
            after["editableTrackMark1"].x(),
            after["editableTrackMark1"].y(),
        ) != position

        assert root.setProperty("tracks", [{**track, "highlight": 2}])
        app.processEvents()
        assert _marks(root) == before
        assert root.property("highlightIndex") == 2
    finally:
        view.close()
        view.releaseResources()
