"""E2x QSG float transport probe: can the application's scene graph carry a float texture?

Experiment-only code.  Nothing here is imported by the application and nothing
under ``gweatherrouting/`` is modified.  The probe answers the bounded question
of issue #71: does the render path the application actually uses --
``QQuickWidget`` + ``QQuickImageProvider`` + QML ``Image`` -> ``ShaderEffect``
``property variant`` -- carry a native float texture, or does the transport
need a native bridge?

The application shader ``gweatherrouting/qt/shaders/weather.frag`` is compiled
with three changes, each asserted here:

* ``decodeField`` reads one float32 per texel instead of unpacking two
  byte-packed texels (the float-transport variant), or is left byte-identical
  (the control variant);
* the field size is no longer halved, because a float texture holds one texel
  per cell; and
* probe-only readback modes are added, which write the IEEE-754 bits of the
  interpolated value into the 8-bit framebuffer so the readback is exact.

Everything else -- the ``texelFetch`` sampling, the manual bilinear arithmetic,
the 2x2 weight loop, the NaN/Inf rejection and the missing-cell branch -- is
used verbatim from the application source.

Readback is a *fidelity* measurement.  The probe never reports a frame rate.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from experiment_float_textures import find_qsb  # noqa: E402

APP_SHADERS = ROOT / "gweatherrouting/qt/shaders"
APPLICATION_SHADER = APP_SHADERS / "weather.frag"
APPLICATION_PACKER = ROOT / "gweatherrouting/qt/gpuweather.py"

# The application's numerical gate.  Unchanged; docs/history/rendering-m0-
# float-textures.md records E1b at 2.384185791015625e-7 and the failed E1
# hardware-filter run at 0.002604166666666824.
NUMERICAL_LIMIT = 2e-5

# Probe QML: the structure of gweatherrouting/qt/shaders/WeatherField.qml, with
# the probe's own shader switch, cell uniforms and texture-provider override.
PROBE_QML = """import QtQuick

Item {
    id: root
    property alias u0Source: imageU0.source
    property alias v0Source: imageV0.source
    property alias u1Source: imageU1.source
    property alias v1Source: imageV1.source
    property var textureProvider: null
    property string shaderFile: "weather.frag.qsb"
    property real blend: 0
    property real outputMode: 0
    property real uCellX: 0
    property real uCellY: 0
    property real uCode: 0
    property real uPointX: 0
    property real uPointY: 0
    property bool imagesReady: imageU0.status === Image.Ready && imageV0.status === Image.Ready
                         && imageU1.status === Image.Ready && imageV1.status === Image.Ready
    property bool ready: effect.status === ShaderEffect.Compiled
    property string shaderError: effect.status === ShaderEffect.Error ? effect.log : ""
    property string shaderStatus: effect.status === ShaderEffect.Compiled ? "Compiled"
                                : (effect.status === ShaderEffect.Error ? "Error" : "Loading")
    property string imageStatus: imageU0.status === Image.Ready ? "Ready"
                                : (imageU0.status === Image.Error ? "Error" : "other")
    Image { id: imageU0; visible: false; smooth: false; cache: false }
    Image { id: imageV0; visible: false; smooth: false; cache: false }
    Image { id: imageU1; visible: false; smooth: false; cache: false }
    Image { id: imageV1; visible: false; smooth: false; cache: false }
    ShaderEffect {
        id: effect
        anchors.fill: parent
        visible: true
        property variant u0: root.textureProvider ? root.textureProvider : imageU0
        property variant v0: root.textureProvider ? root.textureProvider : imageV0
        property variant u1: root.textureProvider ? root.textureProvider : imageU1
        property variant v1: root.textureProvider ? root.textureProvider : imageV1
        property real blend: root.blend
        property real outputMode: root.outputMode
        property real uCellX: root.uCellX
        property real uCellY: root.uCellY
        property real uCode: root.uCode
        property real uPointX: root.uPointX
        property real uPointY: root.uPointY
        fragmentShader: root.shaderFile
    }
}
"""

# The application's byte-packed decoder, kept verbatim.
PACKED_DECODE = """float decodeField(sampler2D field, ivec2 cell) {
    // Two opaque RGB texels per float; never filter these encoded bytes.
    uvec3 low = uvec3(round(texelFetch(field, ivec2(cell.x * 2, cell.y), 0).rgb * 255.0));
    uint high = uint(round(texelFetch(field, ivec2(cell.x * 2 + 1, cell.y), 0).r * 255.0));
    return uintBitsToFloat(low.r | (low.g << 8) | (low.b << 16) | (high << 24));
}"""

FLOAT_DECODE = """float decodeField(sampler2D field, ivec2 cell) {
    // FLOAT TRANSPORT VARIANT: one float32 per texel; no byte unpacking.
    return texelFetch(field, cell, 0).r;
}"""

# Probe-only readback modes.  The cell mode is an early-out so that an
# explicitly addressed texel is read without any interpolation; the
# interpolated-value modes reuse the application's own output block.
PROBE_CELL_BRANCH = """void main() {
    // PROBE (experiment only): the sample position may be chosen by the probe
    // instead of the rasteriser, so the comparison with the CPU oracle is not
    // limited by the rasteriser's fragment-position quantisation.  The
    // application's own arithmetic below is untouched.
    vec2 qt_TexCoord0 = qt_TexCoord0Raster;
    if (outputMode < -1.5) {
        qt_TexCoord0 = vec2(uPointX, uPointY);
    }
    // PROBE (experiment only): a constant ramp, so the probe can verify that
    // the 8-bit framebuffer transfer is linear before it trusts a byte-split
    // float payload.
    if (outputMode < -0.5 && outputMode > -1.5) {
        fragColor = vec4(uCode / 255.0, 0.5, 0.25, 1.0) * qt_Opacity;
        return;
    }
    // PROBE (experiment only): one explicitly addressed texel, read before
    // any interpolation, so a NaN neighbourhood cannot mask the result.
    if (outputMode > 4.5) {
        float cellValue = decodeField(u0, ivec2(int(uCellX), int(uCellY)));
        uint cellBits = floatBitsToUint(cellValue);
        if (outputMode > 5.5) {
            fragColor = vec4(float((cellBits >> 24u) & 255u) / 255.0, 0.0, 0.0, 1.0) * qt_Opacity;
        } else {
            fragColor = vec4(
                float(cellBits & 255u) / 255.0,
                float((cellBits >> 8u) & 255u) / 255.0,
                float((cellBits >> 16u) & 255u) / 255.0,
                1.0) * qt_Opacity;
        }
        return;
    }
"""

PROBE_OUTPUT = """    // PROBE (experiment only): byte-exact readback of the interpolated value.
    if (outputMode > 2.5 || outputMode < -1.5) {
        uint probeBits = floatBitsToUint(uv.x);
        if (outputMode > 3.5 || outputMode < -2.5) {
            fragColor = vec4(float((probeBits >> 24u) & 255u) / 255.0, 0.0, 0.0, 1.0) * qt_Opacity;
        } else {
            fragColor = vec4(
                float(probeBits & 255u) / 255.0,
                float((probeBits >> 8u) & 255u) / 255.0,
                float((probeBits >> 16u) & 255u) / 255.0,
                1.0) * qt_Opacity;
        }
    } else if (outputMode > 1.5) {"""

APPLICATION_OUTPUT = """    if (outputMode > 1.5) {"""

APPLICATION_MAIN = "void main() {"


def shader_variant(source, float_transport):
    """Compile the application's weather.frag for one transport.

    Only the decode accessor, the field size and the probe readback change.
    The spatial interpolation block is asserted to survive verbatim.
    """
    text = source
    if PACKED_DECODE not in text:
        raise ValueError("Reference weather.frag decoder changed")
    text = text.replace(
        PACKED_DECODE, FLOAT_DECODE if float_transport else PACKED_DECODE
    )
    packed_size = "ivec2 size = textureSize(u0, 0) / ivec2(2, 1);"
    float_size = "ivec2 size = textureSize(u0, 0);"
    if packed_size not in text:
        raise ValueError("Reference weather.frag field size changed")
    text = text.replace(packed_size, float_size if float_transport else packed_size)
    if APPLICATION_OUTPUT not in text:
        raise ValueError("Reference weather.frag output block changed")
    text = text.replace(APPLICATION_OUTPUT, PROBE_OUTPUT, 1)
    if APPLICATION_MAIN not in text:
        raise ValueError("Reference weather.frag main changed")
    text = text.replace(APPLICATION_MAIN, PROBE_CELL_BRANCH, 1)
    # The cell uniforms are probe-only additions to the application's block.
    old_block = """    float blend;
    float outputMode;
};"""
    new_block = """    float blend;
    float outputMode;
    float uCellX;
    float uCellY;
    float uCode;
    float uPointX;
    float uPointY;
};"""
    if old_block not in text:
        raise ValueError("Reference weather.frag uniform block changed")
    text = text.replace(old_block, new_block, 1)
    # The probe shadows the rasterised texture coordinate with a local of the
    # same name, so the application's body below is used verbatim.
    varying = "layout(location = 0) in vec2 qt_TexCoord0;"
    if varying not in text:
        raise ValueError("Reference weather.frag texture coordinate changed")
    text = text.replace(varying, "layout(location = 0) in vec2 qt_TexCoord0Raster;", 1)
    # The interpolation block must survive byte-identically.
    for line in (
        "    vec2 point = clamp(qt_TexCoord0 * vec2(size) - 0.5, vec2(0), vec2(size - 1));",
        "    ivec2 base = ivec2(floor(point));",
        "    vec2 delta = fract(point);",
        "            vec2 value = vectorAt(min(base + ivec2(x, y), size - 1));",
        "            if (any(isnan(value)) || any(isinf(value))) {",
        "                fragColor = vec4(0);",
    ):
        if line not in text:
            raise ValueError(f"Reference weather.frag interpolation changed: {line!r}")
    return text


def compile_shader(directory, source_text, name):
    import PySide6

    qsb = find_qsb(Path(PySide6.__file__).parent)
    source = directory / name
    output = directory / f"{name}.qsb"
    source.write_text(source_text)
    subprocess.run(
        [str(qsb), "--msl", "12", "-o", str(output), str(source)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    if not output.is_file():
        raise RuntimeError(f"shader baker produced no {output.name}")
    return output


def application_packer():
    """The application's own byte packer, loaded without the package import."""
    spec = importlib.util.spec_from_file_location("gwr_gpuweather", APPLICATION_PACKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.encode_component


def float_image(values):
    """A float32 QImage, one component per texel, alpha opaque."""
    from PySide6.QtGui import QImage

    values = np.asarray(values, dtype="<f4")
    height, width = values.shape
    pixels = np.zeros((height, width, 4), dtype="<f4")
    pixels[..., 0] = values
    pixels[..., 3] = 1.0
    return QImage(
        pixels.data, width, height, width * 16, QImage.Format.Format_RGBA32FPx4
    ).copy()


def bilinear_oracle(field, height, width, flip_rows=False, flip_y=False):
    """CPU mirror of the application's texelFetch bilinear arithmetic.

    ``field`` is (rows, cols) float32; the result is the interpolated value at
    each device-pixel centre, in the same accumulation order as the shader.
    """
    source = field[::-1] if flip_rows else field
    rows, cols = source.shape
    x = (np.arange(width, dtype=np.float32) + np.float32(0.5)) / np.float32(width)
    y = (np.arange(height, dtype=np.float32) + np.float32(0.5)) / np.float32(height)
    if flip_y:
        y = np.float32(1.0) - y
    xx, yy = np.meshgrid(x, y)
    point_x = np.clip(xx * np.float32(cols) - np.float32(0.5), 0, np.float32(cols - 1))
    point_y = np.clip(yy * np.float32(rows) - np.float32(0.5), 0, np.float32(rows - 1))
    base_x = np.floor(point_x).astype(np.int32)
    base_y = np.floor(point_y).astype(np.int32)
    delta_x = (point_x - base_x).astype(np.float32)
    delta_y = (point_y - base_y).astype(np.float32)
    total = np.zeros((height, width), dtype=np.float32)
    for step_y in (0, 1):
        for step_x in (0, 1):
            weight = (
                (delta_x if step_x else np.float32(1.0) - delta_x)
                * (delta_y if step_y else np.float32(1.0) - delta_y)
            ).astype(np.float32)
            weight = np.where(weight > np.float32(1e-12), weight, np.float32(0.0))
            cell_x = np.minimum(base_x + step_x, cols - 1)
            cell_y = np.minimum(base_y + step_y, rows - 1)
            total = (total + source[cell_y, cell_x] * weight).astype(np.float32)
    return total


def best_orientation(values, field):
    """The smallest error over the four fragment/row orientation candidates.

    The positions are the modelled fragment centres, which is what the
    application's own output block assumes.
    """
    height, width = values.shape
    best = None
    for flip_rows in (False, True):
        for flip_y in (False, True):
            oracle = bilinear_oracle(
                field, height, width, flip_rows=flip_rows, flip_y=flip_y
            )
            finite = np.isfinite(oracle) & np.isfinite(values)
            if not finite.any():
                error = float("inf")
                compared = 0
            else:
                error = float(
                    np.max(
                        np.abs(
                            values[finite].astype(np.float64)
                            - oracle[finite].astype(np.float64)
                        )
                    )
                )
                compared = int(finite.sum())
            if best is None or error < best["max_abs_error"]:
                best = {
                    "max_abs_error": error,
                    "flip_rows": bool(flip_rows),
                    "flip_y": bool(flip_y),
                    "compared_pixels": compared,
                }
    return best


def reconstruct_float(low, high):
    """Rebuild float32 values from two byte-split 8-bit frames."""
    bits = (
        low[..., 0].astype(np.uint32)
        | (low[..., 1].astype(np.uint32) << 8)
        | (low[..., 2].astype(np.uint32) << 16)
        | (high[..., 0].astype(np.uint32) << 24)
    )
    return bits.view(np.float32).reshape(low.shape[:2])


class ImageProvider:
    """Factory for the application's provider shape; filled in by the caller."""

    @staticmethod
    def build(image):
        from PySide6.QtGui import QImage
        from PySide6.QtQuick import QQuickImageProvider

        class Provider(QQuickImageProvider):
            def __init__(self):
                super().__init__(QQuickImageProvider.ImageType.Image)
                self.image = image
                self.requests = 0

            def requestImage(self, identifier, size, requested_size):  # noqa: N802
                self.requests += 1
                result = QImage(self.image)
                if size is not None:
                    size.setWidth(result.width())
                    size.setHeight(result.height())
                return result

        return Provider()


class Probe:
    """The QQuickWidget scene the probe renders into."""

    def __init__(self, directory, width=32, height=24):
        from PySide6.QtCore import QUrl, Qt
        from PySide6.QtQml import QQmlComponent
        from PySide6.QtQuickWidgets import QQuickWidget
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self.quick = QQuickWidget()
        self.width, self.height = width, height
        self.quick.resize(width, height)
        self.quick.setClearColor(Qt.GlobalColor.transparent)
        self.quick.show()
        self.pump(30)
        self.window = self.quick.quickWindow()
        self.dpr = self.quick.devicePixelRatioF()
        (directory / "probe.qml").write_text(PROBE_QML)
        component = QQmlComponent(
            self.quick.engine(), QUrl.fromLocalFile(str(directory / "probe.qml"))
        )
        self.component = component
        self.root = component.create()
        if self.root is None:
            raise RuntimeError(str(component.errors()))
        self.quick.setContent(QUrl(), component, self.root)
        self.root.setWidth(width)
        self.root.setHeight(height)
        self.pump(30)
        time.sleep(0.1)
        self.pump(30)

    def settle(self, seconds=0.2):
        self.pump(20)
        time.sleep(seconds)
        self.pump(20)

    def pump(self, milliseconds=20):
        """Pump the event loop for a bounded time.

        ``QCoreApplication.processEvents`` can block indefinitely on the Cocoa
        event dispatcher, so the probe never calls it: it runs a bounded
        ``QEventLoop`` instead, which always returns.
        """
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def set_shader(self, name, timeout=20.0):
        self.root.setProperty("shaderFile", name)
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.quick.update()
            self.pump(10)
            if str(self.root.property("shaderStatus")) == "Compiled":
                return True
        return False

    def grab(self):
        for _ in range(3):
            self.quick.update()
            self.pump(10)
        image = self.quick.grabFramebuffer()
        if image.isNull():
            raise RuntimeError("framebuffer readback failed")
        return (
            np.frombuffer(image.constBits(), np.uint8)
            .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
            .reshape(image.height(), image.width(), 4)
            .copy()
        )

    def render(self, output_mode):
        self.root.setProperty("outputMode", float(output_mode))
        self.last_frame = self.grab()
        return self.last_frame

    def read_interpolated(self):
        """Two byte-split renders -> the exact float32 the shader wrote."""
        low = self.render(3)
        high = self.render(4)
        return reconstruct_float(low, high), low[..., 3]

    def read_cell(self, cell):
        self.root.setProperty("uCellX", float(cell[0]))
        self.root.setProperty("uCellY", float(cell[1]))
        low = self.render(5)
        high = self.render(6)
        values = reconstruct_float(low, high)
        return float(values[values.shape[0] // 2, values.shape[1] // 2])

    def bind_image_provider(self, label, image):
        from PySide6.QtCore import QUrl

        self.clear_texture_provider()
        provider = ImageProvider.build(image)
        self.quick.engine().addImageProvider(label, provider)
        url = QUrl(f"image://{label}/field")
        for name in ("u0Source", "v0Source", "u1Source", "v1Source"):
            self.root.setProperty(name, url)
        self.settle(0.3)
        return provider

    def bind_texture_provider(self, texture):
        from PySide6.QtQuick import QSGTextureProvider

        class Provider(QSGTextureProvider):
            def texture(self):
                return texture

        self.root.setProperty("textureProvider", Provider())
        self.settle(0.2)
        return None

    def bind_texture_type_provider(self, label, images):
        """The Python-only float transport.

        ``QQuickImageProvider`` in ``ImageType.Texture`` mode hands the scene
        graph a ``QQuickTextureFactory``; the factory returns a QSGTexture the
        probe built with ``QQuickWindow.createTextureFromImage`` from a float
        QImage.  The textures and the factories are built on the GUI thread:
        the provider's ``requestTexture`` runs on a loader thread, where
        creating GPU resources aborts the process.

        ``images`` maps an image identifier to a QImage; the provider returns
        the factory for the identifier the QML Image asked for.
        """
        from PySide6.QtQuick import QQuickImageProvider, QQuickTextureFactory

        textures = {
            key: self.window.createTextureFromImage(image)
            for key, image in images.items()
        }

        class Factory(QQuickTextureFactory):
            def __init__(self, texture):
                super().__init__()
                self._texture = texture

            def createTexture(self, window):
                return self._texture

            def textureSize(self):
                return self._texture.textureSize()

            def textureByteCount(self):
                size = self._texture.textureSize()
                return size.width() * size.height() * 4

        factories = {key: Factory(texture) for key, texture in textures.items()}

        class Provider(QQuickImageProvider):
            def __init__(self):
                super().__init__(QQuickImageProvider.ImageType.Texture)
                self.requests = 0

            def requestTexture(self, identifier, size, requested_size):
                self.requests += 1
                return factories[identifier]

        provider = Provider()
        self.quick.engine().addImageProvider(label, provider)
        return provider, textures

    def bind_texture_type_source(self, label, key):
        from PySide6.QtCore import QUrl

        self.clear_texture_provider()
        url = QUrl(f"image://{label}/{key}")
        for name in ("u0Source", "v0Source", "u1Source", "v1Source"):
            self.root.setProperty(name, url)
        self.settle(0.4)

    def read_at_position(self, position):
        """The application's arithmetic at a position the probe chooses.

        The probe replaces the rasterised fragment position with a uniform one.
        The application's own interpolation code is untouched, so the
        comparison with the CPU oracle is not limited by the rasteriser's
        fragment-position quantisation.
        """
        self.root.setProperty("uPointX", float(position[0]))
        self.root.setProperty("uPointY", float(position[1]))
        low = self.render(-2)
        high = self.render(-3)
        values = reconstruct_float(low, high)
        return float(values[values.shape[0] // 2, values.shape[1] // 2])

    def bind_simple_texture_node(self, texture):
        """The application's own QSGSimpleTextureNode path (gpuscene.ImageItem)."""
        from PySide6.QtCore import QRectF
        from PySide6.QtQuick import QQuickItem, QSGSimpleTextureNode

        class NodeItem(QQuickItem):
            def updatePaintNode(self, old, _):
                node = old or QSGSimpleTextureNode()
                if old is None:
                    node.setOwnsTexture(True)
                node.setTexture(texture)
                node.setRect(QRectF(0, 0, self.width(), self.height()))
                return node

        item = NodeItem()
        item.setFlag(QQuickItem.Flag.ItemHasContents, True)
        item.setParentItem(self.root)
        item.setWidth(self.width)
        item.setHeight(self.height)
        self.settle(0.3)
        for _ in range(3):
            self.quick.update()
            self.pump(10)
        return item

    def clear_texture_provider(self):
        self.root.setProperty("textureProvider", None)
        self.settle(0.1)

    def close(self):
        self.quick.close()
        self.quick.deleteLater()
        self.pump(20)


def api_facts(probe):
    """Facts about the bindings and the formats the host actually produces."""
    from PySide6.QtGui import QImage, QRhiTexture
    from PySide6.QtQuick import QQuickTextureFactory

    facts = {}
    facts["qimage_format_r32f_available"] = hasattr(QImage.Format, "Format_R32F")
    facts["qimage_float_formats"] = [
        name
        for name in dir(QImage.Format)
        if not name.startswith("_") and ("32F" in name or "16F" in name)
    ]
    probe_image = float_image(np.array([[1.0, -2.0], [3.5, 4.25]], dtype="<f4"))
    for label, image in (
        ("rgba32f", probe_image),
        ("rgbx32f", probe_image.convertToFormat(QImage.Format.Format_RGBX32FPx4)),
        ("rgba16f", probe_image.convertToFormat(QImage.Format.Format_RGBA16FPx4)),
        ("rgba8888", probe_image.convertToFormat(QImage.Format.Format_RGBA8888)),
    ):
        texture = probe.window.createTextureFromImage(image)
        rhi = texture.rhiTexture()
        facts[f"create_texture_from_image_{label}"] = {
            "qsg_size": [
                texture.textureSize().width(),
                texture.textureSize().height(),
            ],
            "has_alpha_channel": texture.hasAlphaChannel(),
            "rhi_format": None if rhi is None else rhi.format().name,
            "rhi_flags": [] if rhi is None else [f.name for f in rhi.flags()],
            "render_target": bool(
                rhi is not None and QRhiTexture.Flag.RenderTarget in rhi.flags()
            ),
        }
    raw = probe.window.rhi().newTexture(
        QRhiTexture.Format.R32F,
        probe_image.size(),
        flags=QRhiTexture.Flag.UsedAsTransferSource,
    )
    raw.create()
    wrapped = probe.window.createTextureFromRhiTexture(raw)
    rhi = wrapped.rhiTexture()
    facts["create_texture_from_rhi_texture_r32f"] = {
        "qsg_size": [wrapped.textureSize().width(), wrapped.textureSize().height()],
        "rhi_format": None if rhi is None else rhi.format().name,
    }
    factory = QQuickTextureFactory.textureFactoryForImage(probe_image)
    facts["texture_factory_for_float_image"] = {
        "factory": type(factory).__name__ if factory is not None else None,
        "image_format": None if factory is None else str(factory.image().format()),
    }
    try:
        from PySide6.QtQuick import QSGDynamicTexture

        QSGDynamicTexture()
        facts["qsg_dynamic_texture_instantiable"] = True
    except Exception as exc:  # noqa: BLE001
        facts["qsg_dynamic_texture_instantiable"] = False
        facts["qsg_dynamic_texture_error"] = str(exc)
    return facts


def calibrate(probe):
    """Verify the readback instrument: the framebuffer transfer must be linear."""
    codes = {}
    for code in (0, 32, 64, 128, 192, 255):
        probe.root.setProperty("outputMode", -1.0)
        probe.root.setProperty("uCode", float(code))
        pixels = probe.render(-1)
        codes[code] = [int(v) for v in pixels[0, 0]]
    return {
        "framebuffer_linear": bool(
            all(abs(codes[code][0] - code) <= 1 for code in codes)
        ),
        "codes": codes,
        "note": (
            "an exactly linear 8-bit framebuffer lets the probe carry a "
            "byte-split float32 payload through the readback without loss"
        ),
    }


PROBE_POSITIONS = ((0.07, 0.09), (0.5, 0.5), (0.93, 0.91))


def oracle_at_position(field, position):
    """CPU mirror of the shader's arithmetic at one probe-chosen position."""
    rows, cols = field.shape
    point_x = np.float32(position[0]) * np.float32(cols) - np.float32(0.5)
    point_y = np.float32(position[1]) * np.float32(rows) - np.float32(0.5)
    point_x = min(max(point_x, np.float32(0.0)), np.float32(cols - 1))
    point_y = min(max(point_y, np.float32(0.0)), np.float32(rows - 1))
    base_x = int(np.floor(point_x))
    base_y = int(np.floor(point_y))
    delta_x = np.float32(point_x - base_x)
    delta_y = np.float32(point_y - base_y)
    total = np.float32(0.0)
    for step_y in (0, 1):
        for step_x in (0, 1):
            weight = np.float32(
                (delta_x if step_x else np.float32(1.0) - delta_x)
                * (delta_y if step_y else np.float32(1.0) - delta_y)
            )
            if weight <= np.float32(1e-12):
                continue
            cell_y = min(base_y + step_y, rows - 1)
            cell_x = min(base_x + step_x, cols - 1)
            total = np.float32(total + np.float32(field[cell_y, cell_x]) * weight)
    return float(total)


def measure(probe, field, transport, shader, blend=0.5, cells=(), probe_positions=()):
    """Render one field over one transport and measure numbers, alpha and bits."""
    outcome = {
        "transport": transport,
        "shader": shader,
        "blend": blend,
        "image_status": str(probe.root.property("imageStatus")),
        "shader_status": str(probe.root.property("shaderStatus")),
        "shader_error": str(probe.root.property("shaderError")),
    }
    probe.root.setProperty("blend", float(blend))
    values, alpha = probe.read_interpolated()
    outcome["frame_size"] = [int(values.shape[0]), int(values.shape[1])]
    outcome["alpha_pixels"] = int((alpha > 0).sum())
    outcome["significant_alpha_pixels"] = int((alpha > 25).sum())
    outcome["total_pixels"] = int(alpha.size)
    outcome["interpolated_min"] = float(np.nanmin(values))
    outcome["interpolated_max"] = float(np.nanmax(values))
    outcome["nan_values_in_readback"] = int(np.isnan(values).sum())
    outcome["raw_center_pixel"] = [
        int(v) for v in probe.last_frame[values.shape[0] // 2, values.shape[1] // 2]
    ]
    outcome["numerical"] = best_orientation(values, field)
    outcome["numerical"]["limit"] = NUMERICAL_LIMIT
    outcome["numerical"]["passed"] = bool(
        outcome["numerical"]["max_abs_error"] <= NUMERICAL_LIMIT
    )
    if probe_positions:
        sampled, expected, errors = {}, {}, []
        for position in probe_positions:
            value = probe.read_at_position(position)
            oracle = oracle_at_position(field, position)
            key = f"{position[0]:.4f},{position[1]:.4f}"
            sampled[key] = value
            expected[key] = oracle
            if np.isfinite(value) and np.isfinite(oracle):
                errors.append(abs(float(value) - float(oracle)))
        outcome["probe_position_values"] = sampled
        outcome["probe_position_expected"] = expected
        if errors:
            outcome["probe_position_max_abs_error"] = float(max(errors))
            outcome["probe_position_limit"] = NUMERICAL_LIMIT
            outcome["probe_position_passed"] = bool(max(errors) <= NUMERICAL_LIMIT)
    if cells:
        sampled = {}
        for cell in cells:
            sampled[f"{cell[0]},{cell[1]}"] = probe.read_cell(cell)
        outcome["sampled_cells"] = sampled
        outcome["expected_cells"] = {
            f"{cell[0]},{cell[1]}": float(field[cell[1], cell[0]])
            for cell in cells
            if np.isfinite(field[cell[1], cell[0]])
        }
        outcome["sampled_cells_exact"] = all(
            np.float32(sampled[key]) == np.float32(outcome["expected_cells"][key])
            for key in outcome["expected_cells"]
        )
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=100.0)
    parser.add_argument(
        "--isolated",
        help="run only this stage in this process and write its own report",
    )
    args = parser.parse_args()

    report = {
        "protocol": "qsg-float-transport-e2x-v1",
        "status": "incomplete",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "numerical_limit": NUMERICAL_LIMIT,
        "presentation_latency_measured": False,
        "readback_is_fidelity": True,
        "transports": {},
        "errors": [],
    }
    deadline = time.perf_counter() + args.timeout

    def flush():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")

    def record(name, function):
        print(f"stage {name} ...", file=sys.stderr, flush=True)
        if time.perf_counter() > deadline:
            report["errors"].append(f"{name}: probe deadline reached before the stage")
            flush()
            return None
        try:
            value = function()
        except Exception as exc:  # noqa: BLE001 - every stage is reported
            report["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"stage {name} failed: {exc}", file=sys.stderr, flush=True)
            flush()
            return None
        report["transports"][name] = value
        print(f"stage {name} done", file=sys.stderr, flush=True)
        flush()
        return value

    application_source = APPLICATION_SHADER.read_text()
    pack = application_packer()

    with tempfile.TemporaryDirectory(prefix="gwr-float-transport-") as temporary:
        work = Path(temporary)
        compile_shader(work, shader_variant(application_source, True), "weather.frag")
        compile_shader(work, shader_variant(application_source, False), "packed.frag")

        probe = Probe(work)
        report["environment"] = {
            "qt": __import__("PySide6").__version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "host": platform.node(),
            "graphics_api": probe.window.rendererInterface().graphicsApi().name,
            "device_pixel_ratio": probe.dpr,
            "logical_size": [probe.width, probe.height],
        }

        rows, cols = 6, 8
        grid = (
            (
                np.arange(rows * cols, dtype="<f4").reshape(rows, cols)
                / np.float32(rows * cols)
            )
            - np.float32(0.5)
        ) * np.float32(20.0)
        grid[0, 0] = np.float32(-1000.0)
        grid[1, 3] = np.float32(1000.0)
        grid[4, 5] = np.float32(-3.25)
        grid[5, 7] = np.float32(12.75)
        missing = grid.copy()
        missing[2, 2] = np.nan
        missing[3, 5] = np.nan
        all_missing = np.full((rows, cols), np.nan, dtype="<f4")
        cells = ((0, 0), (3, 1), (5, 4), (7, 5))

        # -- 1. the application's current transport, unchanged: the control.
        def control_packed():
            probe.set_shader("packed.frag.qsb")
            provider = probe.bind_image_provider("probepacked", pack(grid))
            outcome = measure(
                probe,
                grid,
                "image_provider_rgba8888_packed",
                "application weather.frag (byte-packed decoder, unchanged)",
                cells=cells,
                probe_positions=PROBE_POSITIONS,
            )
            outcome["provider_requests"] = provider.requests
            outcome["upload_bytes"] = int(provider.image.sizeInBytes())
            return outcome

        # -- 2. a float QImage through the application's own Image provider.
        def float_through_image_provider():
            probe.set_shader("weather.frag.qsb")
            provider = probe.bind_image_provider("probefloat", float_image(grid))
            outcome = measure(
                probe,
                grid,
                "image_provider_rgba32f",
                "application weather.frag (float decoder)",
                cells=cells,
            )
            outcome["provider_requests"] = provider.requests
            outcome["upload_bytes"] = int(provider.image.sizeInBytes())
            return outcome

        # -- 3. a float texture handed to the ShaderEffect by a texture provider.
        def float_through_texture_provider():
            probe.set_shader("weather.frag.qsb")
            texture = probe.window.createTextureFromImage(float_image(grid))
            probe.bind_texture_provider(texture)
            outcome = measure(
                probe,
                grid,
                "qsg_texture_provider_rgba32f",
                "application weather.frag (float decoder)",
                cells=cells,
            )
            rhi = texture.rhiTexture()
            outcome["rhi_format"] = None if rhi is None else rhi.format().name
            outcome["rhi_flags"] = [] if rhi is None else [f.name for f in rhi.flags()]
            return outcome

        # -- 4. the Python-only float transport: an ImageType.Texture provider.
        def float_through_texture_type_provider():
            probe.set_shader("weather.frag.qsb")
            provider, textures = probe.bind_texture_type_provider(
                "probetexture", {"field": float_image(grid)}
            )
            probe.bind_texture_type_source("probetexture", "field")
            outcome = measure(
                probe,
                grid,
                "image_provider_texture_type_rgba32f",
                "application weather.frag (float decoder)",
                cells=cells,
                probe_positions=PROBE_POSITIONS,
            )
            texture = textures["field"]
            rhi = texture.rhiTexture()
            outcome["provider_requests"] = provider.requests
            outcome["rhi_format"] = None if rhi is None else rhi.format().name
            outcome["rhi_flags"] = [] if rhi is None else [f.name for f in rhi.flags()]
            outcome["upload_bytes"] = int(float_image(grid).sizeInBytes())
            return outcome

        # -- 5. the application's QSGSimpleTextureNode path.
        def simple_texture_node(label, image):
            texture = probe.window.createTextureFromImage(image)
            probe.bind_simple_texture_node(texture)
            rhi = texture.rhiTexture()
            return {
                "case": label,
                "qsg_size": [
                    texture.textureSize().width(),
                    texture.textureSize().height(),
                ],
                "has_alpha_channel": texture.hasAlphaChannel(),
                "rhi_format": None if rhi is None else rhi.format().name,
                "rhi_flags": [] if rhi is None else [f.name for f in rhi.flags()],
                "render_target": bool(
                    rhi is not None and "RenderTarget" in [f.name for f in rhi.flags()]
                ),
                "upload_bytes": int(image.sizeInBytes()),
            }

        # -- 6. missing and masked values.
        #
        # Each case binds exactly one float texture.  Swapping the provider's
        # texture inside one process aborts the process, so every case runs in
        # its own process and never replaces a texture.
        missing_fields = {
            "all_missing": all_missing,
            "partially_missing": missing,
            "finite_control": grid,
        }

        def missing_case(case):
            field = missing_fields[case]
            probe.set_shader("weather.frag.qsb")
            provider, _textures = probe.bind_texture_type_provider(
                f"probemissing{case}", {case: float_image(field)}
            )
            probe.bind_texture_type_source(f"probemissing{case}", case)
            outcome = measure(
                probe,
                field,
                "image_provider_texture_type_rgba32f",
                "application weather.frag (float decoder)",
            )
            outcome["provider_requests"] = provider.requests
            outcome["case"] = case
            return outcome

        def missing_data_packed():
            """The same cases over the application's current byte-packed path."""
            cases = {}
            for case, field in missing_fields.items():
                probe.set_shader("packed.frag.qsb")
                provider = probe.bind_image_provider(
                    f"probemissingpacked{case}", pack(field)
                )
                cases[case] = measure(
                    probe,
                    field,
                    "image_provider_rgba8888_packed",
                    "application weather.frag (byte-packed decoder, unchanged)",
                )
                cases[case]["provider_requests"] = provider.requests
            return cases

        # -- 7. the application's byte-packed decoder, unchanged, on a float texture.
        def packed_decode_on_float():
            probe.set_shader("packed.frag.qsb")
            provider, _textures = probe.bind_texture_type_provider(
                "probepackedfloat", {"field": float_image(grid)}
            )
            probe.bind_texture_type_source("probepackedfloat", "field")
            values, alpha = probe.read_interpolated()
            height, width = values.shape
            oracle = bilinear_oracle(grid, height, width)
            return {
                "transport": "image_provider_texture_type_rgba32f",
                "shader": "application weather.frag (byte-packed decoder, unchanged)",
                "max_abs_error_vs_oracle": float(
                    np.max(
                        np.abs(values.astype(np.float64) - oracle.astype(np.float64))
                    )
                ),
                "decoded_min": float(np.nanmin(values)),
                "decoded_max": float(np.nanmax(values)),
                "alpha_pixels": int((alpha > 0).sum()),
                "provider_requests": provider.requests,
                "note": (
                    "the unchanged byte-packed decoder reads two texels per cell "
                    "from a float texture, so the field grid is halved and the "
                    "values are reinterpreted bytes"
                ),
            }

        # -- 8. a single-channel R32F texture: 4 bytes per float.
        def r32f_transport():
            from PySide6.QtCore import QSize, QUrl
            from PySide6.QtGui import QImage
            from PySide6.QtQuick import QQuickImageProvider, QQuickTextureFactory
            from PySide6.QtGui import (
                QRhiTexture,
                QRhiTextureSubresourceUploadDescription,
                QRhiTextureUploadDescription,
                QRhiTextureUploadEntry,
            )

            rhi = probe.window.rhi()
            values = np.ascontiguousarray(grid, dtype="<f4")
            raw = rhi.newTexture(
                QRhiTexture.Format.R32F,
                QSize(cols, rows),
                flags=QRhiTexture.Flag.UsedAsTransferSource,
            )
            if not raw.create():
                raise RuntimeError("R32F texture creation failed")
            description = QRhiTextureSubresourceUploadDescription(
                values.tobytes(), values.nbytes
            )
            description.setDataStride(cols * 4)
            description.setSourceSize(QSize(cols, rows))
            entry = QRhiTextureUploadEntry(0, 0, description)
            updates = rhi.nextResourceUpdateBatch()
            updates.uploadTexture(raw, QRhiTextureUploadDescription(entry))
            texture = probe.window.createTextureFromRhiTexture(raw)
            texture.commitTextureOperations(rhi, updates)
            provider = ImageProvider.build(QImage())
            outcome = {
                "case": "r32f",
                "upload_bytes": int(values.nbytes),
                "bytes_per_float": 4,
                "rhi_format": (
                    None
                    if texture.rhiTexture() is None
                    else texture.rhiTexture().format().name
                ),
            }

            class Factory(QQuickTextureFactory):
                def createTexture(self, window):
                    return texture

                def textureSize(self):
                    return texture.textureSize()

                def textureByteCount(self):
                    size = texture.textureSize()
                    return size.width() * size.height() * 4

            factory = Factory()

            class Provider(QQuickImageProvider):
                def __init__(self):
                    super().__init__(QQuickImageProvider.ImageType.Texture)
                    self.requests = 0

                def requestTexture(self, identifier, size, requested_size):
                    self.requests += 1
                    return factory

            texture_provider = Provider()
            probe.quick.engine().addImageProvider("prober32f", texture_provider)
            probe.clear_texture_provider()
            url = QUrl("image://prober32f/field")
            for name in ("u0Source", "v0Source", "u1Source", "v1Source"):
                probe.root.setProperty(name, url)
            probe.settle(0.4)
            probe.set_shader("weather.frag.qsb")
            measured = measure(
                probe,
                grid,
                "r32f_texture",
                "application weather.frag (float decoder)",
                cells=cells,
                probe_positions=PROBE_POSITIONS,
            )
            measured.update(outcome)
            return measured

        stages = {
            "control_packed": control_packed,
            "float_through_image_provider": float_through_image_provider,
            "float_through_texture_provider": float_through_texture_provider,
            "float_through_texture_type_provider": float_through_texture_type_provider,
            "simple_texture_node_float": lambda: simple_texture_node(
                "rgba32f", float_image(grid)
            ),
            "simple_texture_node_packed": lambda: simple_texture_node(
                "rgba8888_packed", pack(grid)
            ),
            "missing_all_missing": lambda: missing_case("all_missing"),
            "missing_partially_missing": lambda: missing_case("partially_missing"),
            "missing_finite_control": lambda: missing_case("finite_control"),
            "missing_data_packed": missing_data_packed,
            "r32f_transport": r32f_transport,
            "packed_decode_on_float_texture": packed_decode_on_float,
        }

        if args.isolated:
            name = args.isolated
            if name not in stages:
                raise SystemExit(f"unknown stage {name}")
            record(name, stages[name])
            report["status"] = "completed"
            flush()
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)

        report["api_facts"] = record("api_facts", lambda: api_facts(probe)) or {}
        report["measurement_instrument"] = (
            record("calibration", lambda: calibrate(probe)) or {}
        )
        if not report["measurement_instrument"].get("framebuffer_linear"):
            report["status"] = "instrument-invalid"
            report["errors"].append(
                "the framebuffer transfer is not linear; the byte-exact readback "
                "is invalid and no numerical comparison can be evaluated"
            )
            flush()
            return 1

        for name in (
            "control_packed",
            "float_through_image_provider",
            "float_through_texture_provider",
            "float_through_texture_type_provider",
        ):
            record(name, stages[name])

        # The missing-data stage swaps the provider's texture several times, and
        # the float transport stage is the one this issue is about; both run in
        # their own process so that an abort inside Qt cannot take the rest of
        # the evidence with it.
        for isolated_stage in (
            "r32f_transport",
            "simple_texture_node_float",
            "simple_texture_node_packed",
            "missing_all_missing",
            "missing_partially_missing",
            "missing_finite_control",
            "missing_data_packed",
            "packed_decode_on_float_texture",
            "float_through_texture_type_provider",
        ):
            isolated_output = work / f"isolated-{isolated_stage}.json"
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--output",
                        str(isolated_output),
                        "--isolated",
                        isolated_stage,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    env=dict(
                        __import__("os").environ,
                        PYTHONHASHSEED="0",
                        QT_SCALE_FACTOR="1",
                    ),
                )
            except subprocess.TimeoutExpired:
                report["transports"][isolated_stage] = {
                    "error": "the isolated stage exceeded its time budget"
                }
                continue
            if isolated_output.is_file():
                try:
                    isolated = json.loads(isolated_output.read_text())
                    report["transports"][f"{isolated_stage}__isolated"] = isolated.get(
                        "transports", {}
                    ).get(isolated_stage, {"error": "no result"})
                except json.JSONDecodeError:
                    report["transports"][f"{isolated_stage}__isolated"] = {
                        "error": "the isolated stage wrote an unreadable report"
                    }
            else:
                report["transports"][f"{isolated_stage}__isolated"] = {
                    "error": f"the isolated stage exited {completed.returncode}",
                    "stderr": completed.stderr[-2000:],
                }
            flush()
        report["missing_data"] = {
            case: (report["transports"].get(f"missing_{case}__isolated") or {})
            for case in ("all_missing", "partially_missing", "finite_control")
        }
        report["missing_data_packed"] = (
            report["transports"].get("missing_data_packed__isolated") or {}
        )
        flush()

        # ------------------------------------------------------------- the verdict
        def numeric_of(transport_id, key="numerical"):
            value = report["transports"].get(transport_id) or {}
            metric = value.get(key)
            if isinstance(metric, dict):
                return metric.get("max_abs_error")
            return metric

        control_numeric = numeric_of("control_packed")
        control_measured = numeric_of("control_packed", "probe_position_max_abs_error")
        image_numeric = numeric_of("float_through_image_provider")
        provider_numeric = numeric_of("float_through_texture_provider")
        float_numeric = numeric_of("float_through_texture_type_provider")
        float_measured = numeric_of(
            "float_through_texture_type_provider", "probe_position_max_abs_error"
        )
        replicated = (
            report["transports"].get("float_through_texture_type_provider__isolated")
            or {}
        )
        all_missing = (report.get("missing_data") or {}).get("all_missing") or {}
        partial = (report.get("missing_data") or {}).get("partially_missing") or {}
        finite_control = (report.get("missing_data") or {}).get("finite_control") or {}
        api = report.get("api_facts") or {}
        node_float = (
            report["transports"].get("simple_texture_node_float__isolated") or {}
        )
        node_packed = (
            report["transports"].get("simple_texture_node_packed__isolated") or {}
        )
        float_transport = (
            report["transports"].get("float_through_texture_type_provider") or {}
        )

        report["summary"] = {
            "control_packed_max_abs_error": control_numeric,
            "control_packed_probe_position_max_abs_error": control_measured,
            "float_through_image_provider_max_abs_error": image_numeric,
            "qsg_texture_provider_max_abs_error": provider_numeric,
            "float_through_texture_type_provider_max_abs_error": float_numeric,
            "float_through_texture_type_provider_probe_position_max_abs_error": (
                float_measured
            ),
            "float_transport_sampled_cells_exact": float_transport.get(
                "sampled_cells_exact"
            ),
            "float_transport_isolated_replication_error": (
                replicated.get("numerical") or {}
            ).get("max_abs_error"),
            "float_transport_isolated_replication_probe_position_error": (
                replicated.get("probe_position_max_abs_error")
            ),
            "all_missing_alpha_pixels": all_missing.get("alpha_pixels"),
            "all_missing_significant_alpha_pixels": all_missing.get(
                "significant_alpha_pixels"
            ),
            "partially_missing_alpha_pixels": partial.get("alpha_pixels"),
            "partially_missing_significant_alpha_pixels": partial.get(
                "significant_alpha_pixels"
            ),
            "finite_control_max_abs_error": (finite_control.get("numerical") or {}).get(
                "max_abs_error"
            ),
            "finite_control_alpha_pixels": finite_control.get("alpha_pixels"),
            "simple_texture_node_rgba32f": node_float.get("rhi_format"),
            "simple_texture_node_rgba32f_flags": node_float.get("rhi_flags"),
            "simple_texture_node_rgba32f_error": node_float.get("error"),
            "simple_texture_node_rgba8888_packed": node_packed.get("rhi_format"),
            "simple_texture_node_rgba8888_packed_error": node_packed.get("error"),
            "create_texture_from_image_rgba32f": (
                api.get("create_texture_from_image_rgba32f") or {}
            ).get("rhi_format"),
            "qimage_format_r32f_available": api.get("qimage_format_r32f_available"),
            "float_transport_via_qsg": bool(
                float_measured is not None and float_measured <= NUMERICAL_LIMIT
            ),
            "float_transport_via_image_provider": bool(
                image_numeric is not None and image_numeric <= NUMERICAL_LIMIT
            ),
            "missing_data_zero_alpha": bool(
                all_missing.get("alpha_pixels") == 0
                and all_missing.get("significant_alpha_pixels") == 0
            ),
        }
        report["status"] = "completed"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report["harness_sha256"] = hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest()
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(json.dumps(report["summary"], indent=2, default=str))
        sys.stdout.flush()
        sys.stderr.flush()
        # Tearing down the embedded Metal context aborts the process on this
        # host.  The report is already on disk, so the probe leaves with a
        # clean exit code instead of a signal that would be misread as a
        # failed measurement.
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
