"""Exercise image-fill transactions with real QML objects, not presentation.

These tests use the headless viewport-method harness with actual scene items and
separate QML engines. They do not assert shader compilation or rendered pixels.
"""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView

from test.test_image_fill_transaction import SOURCES, Scene, stage


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QGuiApplication.instance() or QGuiApplication([])
    yield application


class QmlScene(Scene):
    def __init__(self):
        super().__init__()
        self.view = QQuickView()
        self.view.engine().addImageProvider("weather", self._provider)
        path = (
            Path(__file__).resolve().parents[1]
            / "sailworks/qt/shaders/WeatherScene.qml"
        )
        self.view.setSource(QUrl.fromLocalFile(str(path)))
        assert not self.view.errors(), str(self.view.errors())
        root = self.view.rootObject()
        assert root is not None
        root.setProperty("width", 64)
        root.setProperty("height", 64)

        def direct_child(name):
            return next(
                item
                for item in root.findChildren(QObject, name)
                if item.parent() is root
            )

        self._weather = root
        self._fill = direct_child("weatherFill")
        self._fill_back = direct_child("weatherFillBack")
        self._barbs = direct_child("weatherBarbs")
        # Hold publication pending deterministically, independent of shader
        # backend and image-loading speed. Sources remain real QML properties.
        self._fill_back.setProperty("nativeFields", True)
        self._fill_back.setProperty("nativeReady", False)
        self.view.resize(64, 64)
        self.view.show()

    def close(self):
        self._cancel_image_fill(withdraw=True)
        self._disposed = True
        self.view.close()
        self.view.releaseResources()


def test_real_scene_hide_show_cancels_pending_sources(app):
    scene = QmlScene()
    try:
        token = stage(scene, 1)
        back = scene._fill_back
        assert token
        assert scene._publication_manager.layers["fill"].pending is not None
        assert all(not back.property(name).isEmpty() for name in SOURCES)
        assert not back.property("prepared")

        scene._set_weather_visible(False)
        assert all(back.property(name).isEmpty() for name in SOURCES)
        assert scene._publication_manager.layers["fill"].pending is None
        scene._set_weather_visible(True)
        scene._try_commit_image_fill(token)
        app.processEvents()
        assert not scene._fill.isVisible()
        assert not back.isVisible()
        assert scene._publication_manager.layers["fill"].visible is None
    finally:
        scene.close()


def test_real_scene_teardown_with_pending_sources_rejects_completion(app):
    scene = QmlScene()
    try:
        token = stage(scene, 1)
        assert scene._publication_manager.layers["fill"].pending is not None
        assert all(not scene._fill_back.property(name).isEmpty() for name in SOURCES)
        scene._cancel_image_fill(withdraw=True)
        scene._disposed = True
        assert all(scene._fill_back.property(name).isEmpty() for name in SOURCES)
        assert not scene._provider._reservations
        scene.view.close()
        scene.view.releaseResources()
        app.processEvents()
        scene._try_commit_image_fill(token)
        assert scene._publication_manager.layers["fill"].pending is None
        assert scene._publication_manager.layers["fill"].visible is None
    finally:
        scene.close()


def test_two_real_scenes_keep_pending_transactions_independent(app):
    first = QmlScene()
    second = QmlScene()
    try:
        first_token = stage(first, 1)
        second_token = stage(second, 2)
        second_sources = tuple(second._fill_back.property(name) for name in SOURCES)
        assert first_token and second_token and first_token is not second_token
        assert all(not url.isEmpty() for url in second_sources)
        assert first._provider is not second._provider

        first.close()
        app.processEvents()
        second._try_commit_image_fill(first_token)
        assert second._publication_manager.layers["fill"].pending.generation == 2
        assert (
            tuple(second._fill_back.property(name) for name in SOURCES)
            == second_sources
        )
        assert second._provider._reservations
        assert not second._fill_back.isVisible()
    finally:
        first.close()
        second.close()
