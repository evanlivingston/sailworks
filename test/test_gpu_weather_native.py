"""Native ShaderEffect readback. Opt in; offscreen/software is not GPU proof.

Run with GWR_NATIVE_GPU_TEST=1 and QT_QPA_PLATFORM unset on a desktop.
"""

from datetime import datetime, timedelta
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GWR_NATIVE_GPU_TEST") != "1", reason="Requires native GPU desktop"
)


def _identical_scene_shader_urls(directory):
    original = (
        Path(__file__).resolve().parents[1] / "sailworks/qt/shaders/weather.frag.qsb"
    )
    urls = tuple(directory / f"weather-{slot}.frag.qsb" for slot in ("front", "back"))
    for path in urls:
        path.write_bytes(original.read_bytes())
        assert path.read_bytes() == original.read_bytes()
    assert urls[0] != urls[1]
    return urls


def test_scene_shader_url_control_is_byte_identical(tmp_path):
    urls = _identical_scene_shader_urls(tmp_path)
    assert urls[0].read_bytes() == urls[1].read_bytes()
    shader_dir = Path(__file__).resolve().parents[1] / "sailworks/qt/shaders"
    original = (shader_dir / "weather.frag.qsb").read_bytes()
    for slot in ("front", "back"):
        assert (shader_dir / f"weather-fill-{slot}.frag.qsb").read_bytes() == original
    for scene in ("WeatherScene.qml", "ViewportChartScene.qml"):
        source = (shader_dir / scene).read_text()
        for slot in ("front", "back"):
            assert f'Qt.resolvedUrl("weather-fill-{slot}.frag.qsb")' in source


def test_hidden_fill_shader_preparation_is_invisible():
    """Run the potentially hanging offscreen readback in a bounded process group."""
    command = [sys.executable, __file__, "--hidden-fill-probe"]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        pytest.fail(
            f"Hidden fill probe exceeded 25 seconds: {output.decode(errors='replace')}"
        )
    assert process.returncode == 0, output.decode(errors="replace")


def test_native_scene_hidden_fill_swap_is_atomic():
    """Bound the scene-level image-fill preparation and framebuffer readback."""
    process = subprocess.Popen(
        [sys.executable, __file__, "--scene-fill-swap-probe"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        pytest.fail(
            f"Scene fill probe exceeded 25 seconds: {output.decode(errors='replace')}"
        )
    assert process.returncode == 0, output.decode(errors="replace")


def _scene_fill_swap_probe():
    from PySide6.QtCore import QObject, QUrl
    from PySide6.QtGui import QColor, QGuiApplication
    from PySide6.QtQuick import QQuickView, QSGRendererInterface

    from sailworks.qt.gpuweather import WeatherImageProvider, WeatherTexturePair

    app = QGuiApplication.instance() or QGuiApplication([])
    view = QQuickView()
    provider = WeatherImageProvider()
    view.engine().addImageProvider("weather", provider)
    source = (
        Path(__file__).resolve().parents[1] / "sailworks/qt/shaders/WeatherScene.qml"
    )
    view.setSource(QUrl.fromLocalFile(str(source)))
    assert not view.errors(), str(view.errors())
    root = view.rootObject()
    root.setProperty("width", 64)
    root.setProperty("height", 64)
    # WeatherBarbs also contains fills for its standalone use; select the
    # scene's direct children rather than a nested namesake.
    front = next(
        item
        for item in root.findChildren(QObject, "weatherFill")
        if item.parent() is root
    )
    back = next(
        item
        for item in root.findChildren(QObject, "weatherFillBack")
        if item.parent() is root
    )
    assert front is not None and back is not None
    root.findChild(QObject, "weatherBarbs").setProperty("visible", False)
    assert str(front.property("fragmentShader")).endswith(
        "/weather-fill-front.frag.qsb"
    )
    assert str(back.property("fragmentShader")).endswith("/weather-fill-back.frag.qsb")
    view.setColor(QColor("#123456"))
    view.resize(64, 64)
    view.show()
    start = datetime(2026, 1, 1)
    front_pair = WeatherTexturePair.from_arrays(
        start, start + timedelta(hours=3), [[1]], [[2]], [[3]], [[4]]
    )
    back_pair = WeatherTexturePair.from_arrays(
        start, start + timedelta(hours=3), [[30]], [[31]], [[32]], [[33]]
    )
    names = ("u0Source", "v0Source", "u1Source", "v1Source")

    def settle(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            view.update()

    def capture():
        app.processEvents()
        view.update()
        image = view.grabWindow()
        assert not image.isNull(), "Scene framebuffer capture unavailable"
        return image

    def pixels(image):
        return tuple(
            image.pixel(x, y)
            for y in range(image.height())
            for x in range(image.width())
        )

    try:
        front_urls = provider.reserve_field("fill", front_pair.images, "front")
        assert len(set(front_urls)) == 4
        for name, url in zip(names, front_urls):
            front.setProperty(name, QUrl(url))
        provider.promote_field("fill")
        root.setProperty("fillEnabled", True)
        settle(2)
        before = capture()
        api = view.rendererInterface().graphicsApi()
        assert api not in (
            QSGRendererInterface.GraphicsApi.Software,
            QSGRendererInterface.GraphicsApi.Unknown,
        ), f"Not a hardware shader backend: {api}"
        assert front.property("prepared"), (
            f"front visible={front.property('visible')} "
            f"imagesReady={front.property('imagesReady')} "
            f"displayable={front.property('displayable')} "
            f"effectVisible={front.property('effectVisible')} "
            f"shaderStatus={front.property('shaderStatus')} "
            f"shaderError={front.property('shaderError')} "
            f"sourceError={front.property('sourceError')} "
            f"scene fillEnabled={root.property('fillEnabled')} "
            f"size={front.property('width')}x{front.property('height')} "
            f"requests={provider.requests}"
        )
        assert not front.property("sourceError"), front.property("sourceError")
        baseline = pixels(before)
        assert any(pixel != QColor("#123456").rgba() for pixel in baseline)

        back_urls = provider.reserve_field("fill", back_pair.images, "back")
        assert len(set(back_urls)) == 4 and set(front_urls).isdisjoint(back_urls)
        assert not back.property("visible")
        for name, url in zip(names, back_urls):
            back.setProperty(name, QUrl(url))
            assert pixels(capture()) == baseline, "Pending fill leaked during loading"
        settle(2)
        assert pixels(capture()) == baseline, "Pending fill leaked after preparation"
        assert not back.property("visible")
        assert back.property("prepared"), back.property("shaderError")
        assert not back.property("sourceError"), back.property("sourceError")
        assert (
            provider.requests == 8
        ), f"Expected eight image requests, got {provider.requests}"

        root.setProperty("fillEnabled", False)
        settle(0.2)
        empty = pixels(capture())
        assert all(
            pixel == QColor("#123456").rgba() for pixel in empty
        ), "The scene was not empty with both fill slots hidden"
        back.setProperty("visible", True)
        provider.promote_field("fill")
        settle(2)
        after = capture()
        assert (before.width(), before.height()) == (after.width(), after.height())
        assert pixels(after) != empty, "The prepared back fill did not render"
        assert pixels(after) != baseline, "Slot swap did not change the portrayal"
        assert back.property("prepared") and not back.property("sourceError")
    finally:
        view.close()
        view.releaseResources()
        app.processEvents()


def _hidden_fill_probe():
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QColor, QGuiApplication
    from PySide6.QtQuick import QQuickView, QSGRendererInterface

    from sailworks.qt.gpuweather import WeatherImageProvider, WeatherTexturePair

    app = QGuiApplication.instance() or QGuiApplication([])
    view = QQuickView()
    provider = WeatherImageProvider()
    view.engine().addImageProvider("weather", provider)
    source = (
        Path(__file__).resolve().parents[1] / "sailworks/qt/shaders/WeatherFill.qml"
    )
    view.setSource(QUrl.fromLocalFile(str(source)))
    assert not view.errors(), str(view.errors())
    root = view.rootObject()
    root.setProperty("width", 64)
    root.setProperty("height", 64)
    root.setProperty("visible", False)
    view.setColor(QColor("#123456"))
    view.resize(64, 64)
    view.show()
    start = datetime(2026, 1, 1)
    pair = WeatherTexturePair.from_arrays(
        start, start + timedelta(hours=3), [[1]], [[2]], [[3]], [[4]]
    )
    for name, url in zip(
        ("u0Source", "v0Source", "u1Source", "v1Source"), provider.publish(pair)
    ):
        root.setProperty(name, QUrl(url))

    def settle(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            view.update()

    try:
        settle(2)
        image = view.grabWindow()
        assert not image.isNull(), "Hidden framebuffer capture unavailable"
        api = view.rendererInterface().graphicsApi()
        assert api not in (
            QSGRendererInterface.GraphicsApi.Software,
            QSGRendererInterface.GraphicsApi.Unknown,
        ), f"Cannot prove shader compilation on backend {api}"
        assert not root.property("visible")
        assert all(
            image.pixelColor(x, y) == QColor("#123456")
            for x in range(image.width())
            for y in range(image.height())
        ), "Pending content appeared in the framebuffer"
        assert (
            provider.requests == 4
        ), f"Expected four image requests, got {provider.requests}"
        assert root.property("imagesReady"), "Four provider images did not become ready"
        hidden_prepared = root.property("prepared")
        root.setProperty("visible", True)
        settle(2)
        view.grabWindow()
        settle(0.2)
        assert root.property(
            "prepared"
        ), f"Visible control did not compile: {root.property('shaderError')}"
        assert hidden_prepared, (
            "Hidden images loaded but ShaderEffect did not compile; "
            "hidden readiness not proven"
        )
    finally:
        view.close()
        view.releaseResources()
        app.processEvents()


if __name__ == "__main__" and "--hidden-fill-probe" in sys.argv:
    _hidden_fill_probe()

if __name__ == "__main__" and "--scene-fill-swap-probe" in sys.argv:
    _scene_fill_swap_probe()


def test_native_shader_interpolates_vectors_without_reloading_images():
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication, QColor
    from PySide6.QtQuick import QQuickView, QSGRendererInterface

    from sailworks.qt.gpuweather import WeatherImageProvider, WeatherTexturePair

    app = QGuiApplication.instance() or QGuiApplication([])
    view = QQuickView()
    provider = WeatherImageProvider()
    view.engine().addImageProvider("weather", provider)
    source = (
        Path(__file__).resolve().parents[1] / "sailworks/qt/shaders/WeatherField.qml"
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
