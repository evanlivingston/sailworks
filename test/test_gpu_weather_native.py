"""Native ShaderEffect readback. Opt in; offscreen/software is not GPU proof.

Run with GWR_NATIVE_GPU_TEST=1 and QT_QPA_PLATFORM unset on a desktop.
"""

from datetime import datetime, timedelta
import os
from pathlib import Path
import time

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GWR_NATIVE_GPU_TEST") != "1", reason="Requires native GPU desktop"
)


def test_native_shader_interpolates_vectors_without_reloading_images():
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication, QColor
    from PySide6.QtQuick import QQuickView, QSGRendererInterface

    from gweatherrouting.qt.gpuweather import WeatherImageProvider, WeatherTexturePair

    app = QGuiApplication.instance() or QGuiApplication([])
    view = QQuickView()
    provider = WeatherImageProvider()
    view.engine().addImageProvider("weather", provider)
    source = (
        Path(__file__).resolve().parents[1]
        / "gweatherrouting/qt/shaders/WeatherField.qml"
    )
    view.setSource(QUrl.fromLocalFile(str(source)))
    assert not view.errors(), str(view.errors())
    root = view.rootObject()
    root.setWidth(64)
    root.setHeight(64)
    view.setColor(QColor(0, 0, 0, 0))
    view.resize(64, 64)
    start = datetime(2026, 1, 1)
    snapshot = WeatherTexturePair.from_arrays(
        start,
        start + timedelta(hours=3),
        [[-30.25]],
        [[12.125]],
        [[42.75]],
        [[-22.375]],
    )
    urls = provider.publish(snapshot)
    for name, url in zip(("u0Source", "v0Source", "u1Source", "v1Source"), urls):
        root.setProperty(name, QUrl(url))
    root.setProperty("outputMode", 1.0)
    view.show()

    def grab(x=None, y=None):
        deadline = time.monotonic() + 5
        while not root.property("ready") and time.monotonic() < deadline:
            app.processEvents()
        assert root.property("ready"), "Numerical textures did not load"
        app.processEvents()
        view.update()
        image = view.grabWindow()
        assert not image.isNull()
        return image.pixelColor(
            image.width() // 2 if x is None else x,
            image.height() // 2 if y is None else y,
        )

    def publish(u0, v0, u1, v1):
        replacement = WeatherTexturePair.from_arrays(
            snapshot.before, snapshot.after, u0, v0, u1, v1
        )
        for name, url in zip(
            ("u0Source", "v0Source", "u1Source", "v1Source"),
            provider.publish(replacement),
        ):
            root.setProperty(name, QUrl(url))

    try:
        grab()
        api = view.rendererInterface().graphicsApi()
        assert api not in (
            QSGRendererInterface.GraphicsApi.Software,
            QSGRendererInterface.GraphicsApi.Unknown,
        ), f"Not a hardware shader backend: {api}"
        print(f"Native numerical weather shader backend: {api.name}")
        assert provider.requests == 4
        for seconds in (0, 1, 60, 3599, 5400, 10799, 10800):
            weight = snapshot.weight(start + timedelta(seconds=seconds))
            root.setProperty("blend", weight)
            color = grab()
            expected = np.array([-30.25, 12.125]) + np.array([73, -34.5]) * weight
            actual = np.array([color.redF(), color.greenF()]) * 256 - 128
            np.testing.assert_allclose(actual, expected, atol=0.55, rtol=0)
            assert color.alpha() == 255
        assert provider.requests == 4, "Changing time reloaded forecast images"
        # Speed is computed *after* U/V interpolation, not a crossfade of speed.
        root.setProperty("blend", 0.5)
        root.setProperty("outputMode", 2.0)
        color = grab()
        assert abs(color.redF() * 128 - np.hypot(6.25, -5.125)) < 0.3

        # Decode first, then interpolate in space: filtering encoded bytes
        # would produce nonsense for signed and noninteger U/V components.
        a = np.array([[-20.25, 4.5], [12.125, 30.75]])
        b = np.array([[40.5, -8.125], [-16.75, 6.25]])
        publish(a, -a, b, -b)
        root.setProperty("blend", 0.25)
        root.setProperty("outputMode", 1.0)
        color = grab()
        # grabWindow uses physical pixels (including Retina scaling).
        image = view.grabWindow()
        dx = ((image.width() // 2 + 0.5) / image.width()) * 2 - 0.5
        dy = ((image.height() // 2 + 0.5) / image.height()) * 2 - 0.5
        field = a + (b - a) * 0.25
        expected = (
            field[0, 0] * (1 - dx) * (1 - dy)
            + field[0, 1] * dx * (1 - dy)
            + field[1, 0] * (1 - dx) * dy
            + field[1, 1] * dx * dy
        )
        np.testing.assert_allclose(
            np.array([color.redF(), color.greenF()]) * 256 - 128,
            [expected, -expected],
            atol=0.55,
            rtol=0,
        )
        assert provider.requests == 8

        # Exact endpoints must ignore NaNs from the unused time layer.
        missing = [[np.nan]]
        publish([[17.25]], [[-2.5]], missing, missing)
        root.setProperty("blend", 0.0)
        color = grab()
        assert abs(color.redF() * 256 - 128 - 17.25) < 0.55
        root.setProperty("blend", 0.5)
        color = grab()
        assert color.red() == color.green() == color.blue() == 0

        # A missing spatial neighbor matters only when its weight is nonzero.
        holes = [[12.25, np.nan], [np.nan, np.nan]]
        publish(holes, holes, holes, holes)
        root.setProperty("blend", 0.5)
        color = grab(0, 0)
        assert abs(color.redF() * 256 - 128 - 12.25) < 0.55
        color = grab()
        assert color.red() == color.green() == color.blue() == 0

        # Exercise resource release/recreation while CPU snapshots survive.
        view.hide()
        view.releaseResources()
        app.processEvents()
        view.show()
        color = grab(0, 0)
        assert abs(color.redF() * 256 - 128 - 12.25) < 0.55
    finally:
        view.close()
        view.releaseResources()
        app.processEvents()
