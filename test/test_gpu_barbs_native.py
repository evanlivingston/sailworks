"""Read back the actual numerical barb mesh on a native desktop GPU."""

from datetime import datetime, timedelta
import os
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GWR_NATIVE_GPU_TEST") != "1", reason="Requires native GPU desktop"
)


@pytest.mark.parametrize("missing", ("none", "some", "all"))
def test_barb_mesh_matches_cpu_geometry_and_reuses_textures(missing):
    import cairo
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication, QColor
    from PySide6.QtQuick import QQuickView, QSGRendererInterface
    from gweatherrouting.qt.gpuweather import (
        WeatherImageProvider,
        WeatherTexturePair,
        encode_component,
    )
    from gweatherrouting.rendering.windpainter import WindPainter

    app = QGuiApplication.instance() or QGuiApplication([])
    view = QQuickView()
    provider = WeatherImageProvider()
    view.engine().addImageProvider("weather", provider)
    view.setSource(
        QUrl.fromLocalFile(
            str(
                Path(__file__).resolve().parents[1]
                / "gweatherrouting/qt/shaders/WeatherBarbs.qml"
            )
        )
    )
    assert not view.errors(), str(view.errors())
    root = view.rootObject()
    root.setWidth(128)
    root.setHeight(128)
    root.setProperty("columns", 2)
    root.setProperty("rows", 2)
    view.resize(128, 128)
    view.setColor(QColor("black"))
    start = datetime(2026, 1, 1)
    u = np.array([[10.0, 30.0], [0.0, -12.0]])
    v = np.array([[0.0, 0.0], [0.0, 5.0]])
    if missing == "some":
        u[0, 1] = np.nan
        v[1, 1] = np.inf
    elif missing == "all":
        u[:] = np.nan
        v[:] = np.nan
    ax = np.array([[32.0, 96.0], [32.0, 96.0]])
    ay = np.array([[32.0, 32.0], [96.0, 96.0]])
    pair = WeatherTexturePair.from_arrays(
        start, start + timedelta(hours=3), u, v, -u, -v
    )
    images = pair.images + (encode_component(ax), encode_component(ay))
    urls = provider.publish(
        SimpleNamespace(images=images, nbytes=sum(i.sizeInBytes() for i in images))
    )
    for name, url in zip(
        (
            "u0Source",
            "v0Source",
            "u1Source",
            "v1Source",
            "anchorXSource",
            "anchorYSource",
        ),
        urls,
    ):
        root.setProperty(name, QUrl(url))
    view.show()
    try:
        deadline = time.monotonic() + 10
        while not root.property("ready") and time.monotonic() < deadline:
            app.processEvents()
            view.grabWindow()
        assert root.property("ready"), root.property("shaderError")
        api = view.rendererInterface().graphicsApi()
        assert api not in (
            QSGRendererInterface.GraphicsApi.Software,
            QSGRendererInterface.GraphicsApi.Unknown,
        )
        print(f"Native wind barb shader backend: {api.name}")
        for weight in (0.0, 1 / 180, 0.499, 0.5, 0.501, 1.0):
            root.setProperty("blend", weight)
            app.processEvents()
            image = view.grabWindow()
            image = image.convertToFormat(image.Format.Format_RGBA8888)
            pixels = np.frombuffer(image.constBits(), np.uint8).reshape(
                image.height(), image.width(), 4
            )
            actual = pixels[..., :3].max(axis=2) > 60
            valid = np.isfinite(u) & np.isfinite(v)
            # Use the transport's endpoint/linear interpolation arithmetic.
            # Multiplication by (1 - 2*w) flips signed zero after w=0.5,
            # reversing a calm reference shaft even though the staged blend
            # remains +0.0. The expressions are not IEEE-754 equivalents.
            if weight <= 0:
                uv_u, uv_v = u[valid], v[valid]
            elif weight >= 1:
                uv_u, uv_v = -u[valid], -v[valid]
            else:
                uv_u = u[valid] + (-u[valid] - u[valid]) * weight
                uv_v = v[valid] + (-v[valid] - v[valid]) * weight
            directions = np.degrees(np.arctan2(-uv_u, -uv_v)) % 360
            geometry = WindPainter.build_barb_geometry(
                ax[valid], ay[valid], directions, np.hypot(uv_u, uv_v)
            )
            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32, image.width(), image.height()
            )
            cr = cairo.Context(surface)
            cr.scale(image.width() / 128, image.height() / 128)
            WindPainter.paint_barb_geometry(cr, geometry, 1.0)
            surface.flush()
            reference = np.frombuffer(surface.get_data(), np.uint8).reshape(
                image.height(), image.width(), 4
            )
            expected = reference[..., :3].max(axis=2) > 60
            if expected.any():
                overlap = (actual & expected).sum() / (actual | expected).sum()
                assert (
                    overlap > 0.72
                ), f"Barb silhouette overlap {overlap:.3f} at blend {weight}"
            else:
                assert not actual.any(), "Empty wind slots painted over the map"
            half_w, half_h = image.width() // 2, image.height() // 2
            for y in range(2):
                for x in range(2):
                    region = np.s_[
                        y * half_h : (y + 1) * half_h, x * half_w : (x + 1) * half_w, :3
                    ]
                    # Cairo is native little-endian BGRA; readback is RGBA.
                    expected_rgb = reference[region][..., ::-1].max(axis=(0, 1))
                    actual_rgb = pixels[region].max(axis=(0, 1))
                    np.testing.assert_allclose(actual_rgb, expected_rgb, atol=3, rtol=0)
        assert provider.requests == 6, "Time scrubbing reloaded numerical textures"
    finally:
        view.close()
        view.releaseResources()
        app.processEvents()
