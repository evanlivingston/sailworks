"""E1 native Qt/RHI float transport and sampling probe, generated data only."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tempfile
import time

VERTEX = """#version 440
layout(location = 0) out vec2 uv;
void main() {
    vec2 p = vec2(gl_VertexIndex == 1 ? 3.0 : -1.0,
                  gl_VertexIndex == 2 ? 3.0 : -1.0);
    uv = (p + 1.0) * 0.5;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""
FRAGMENT = """#version 440
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
layout(binding = 0) uniform sampler2D field;
void main() { color = texture(field, uv); }
"""
MANUAL_FRAGMENT = """#version 440
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
layout(binding = 0) uniform sampler2D field;
void main() {
    ivec2 size = textureSize(field, 0);
    vec2 p = clamp(uv * vec2(size) - 0.5, vec2(0.0), vec2(size - 1));
    ivec2 a = ivec2(floor(p));
    ivec2 b = min(a + ivec2(1), size - 1);
    vec2 w = fract(p);
    color = texelFetch(field, a, 0) * (1.0 - w.x) * (1.0 - w.y)
          + texelFetch(field, ivec2(b.x, a.y), 0) * w.x * (1.0 - w.y)
          + texelFetch(field, ivec2(a.x, b.y), 0) * (1.0 - w.x) * w.y
          + texelFetch(field, b, 0) * w.x * w.y;
}
"""


def bilinear_reference(field, width, height):
    import numpy as np

    source_height, source_width, _ = field.shape
    x = np.clip(
        (np.arange(width) + 0.5) / width * source_width - 0.5, 0, source_width - 1
    )
    y = np.clip(
        (np.arange(height) + 0.5) / height * source_height - 0.5, 0, source_height - 1
    )
    xx, yy = np.meshgrid(x, y)
    x0, y0 = xx.astype(int), yy.astype(int)
    x1, y1 = np.minimum(x0 + 1, source_width - 1), np.minimum(y0 + 1, source_height - 1)
    dx, dy = (xx - x0)[..., None], (yy - y0)[..., None]
    return (
        field[y0, x0] * (1 - dx) * (1 - dy)
        + field[y0, x1] * dx * (1 - dy)
        + field[y1, x0] * (1 - dx) * dy
        + field[y1, x1] * dx * dy
    )


def find_qsb(package):
    for suffix in ("qsb", "Qt/libexec/qsb", "Qt/bin/qsb"):
        path = package / suffix
        if path.is_file():
            return path
    raise RuntimeError(
        "Installed Qt shader baker unavailable; do not install one automatically"
    )


def compile_shaders(directory, sampling="linear"):
    import PySide6

    qsb = find_qsb(Path(PySide6.__file__).parent)
    shaders = []
    fragment = MANUAL_FRAGMENT if sampling == "manual" else FRAGMENT
    for suffix, text in (("vert", VERTEX), ("frag", fragment)):
        source, output = (
            directory / f"field.{suffix}",
            directory / f"field.{suffix}.qsb",
        )
        source.write_text(text)
        subprocess.run(
            [str(qsb), "--msl", "12", "-o", str(output), str(source)],
            check=True,
            capture_output=True,
            timeout=20,
        )
        shaders.append(output.read_bytes())
    return shaders


def run_probe(shaders, sampling="linear"):
    import numpy as np
    import PySide6
    from PySide6.QtCore import QSize, QTimer
    from PySide6.QtGui import (
        QColor,
        QRhi,
        QRhiColorAttachment,
        QRhiDepthStencilClearValue,
        QRhiReadbackResult,
        QRhiSampler,
        QRhiShaderResourceBinding,
        QRhiShaderStage,
        QRhiTexture,
        QRhiTextureRenderTargetDescription,
        QRhiTextureSubresourceUploadDescription,
        QRhiTextureUploadDescription,
        QRhiTextureUploadEntry,
        QRhiViewport,
        QShader,
    )
    from PySide6.QtWidgets import QApplication, QRhiWidget

    # Raw transport includes values that image premultiplication/normalization
    # must not corrupt. Sampling is tested separately on finite vectors.
    bits = np.array(
        [
            0,
            0x80000000,
            0x3FC00000,
            0xC0800000,
            0x7FC00001,
            0x7F800000,
            0xFF800000,
            0x3EAAAAAB,
        ],
        dtype="<u4",
    )
    field = (np.arange(24, dtype=np.float32).reshape(2, 3, 4) - 8) / 8
    errors, resources = [], []
    read_raw, read_sample = QRhiReadbackResult(), QRhiReadbackResult()
    counters = dict(texture_creations=0, uploads=0, upload_bytes=0, render_calls=0)

    class Widget(QRhiWidget):
        def initialize(self, _cb):
            try:
                rhi = self.rhi()
                self.backend = rhi.backend().name
                if rhi.backend() != QRhi.Implementation.Metal:
                    raise RuntimeError("This bounded E1 probe requires native Metal")
                self.raw = rhi.newTexture(
                    QRhiTexture.Format.R32F,
                    QSize(4, 2),
                    flags=QRhiTexture.Flag.UsedAsTransferSource,
                )
                self.field = rhi.newTexture(QRhiTexture.Format.RGBA32F, QSize(3, 2))
                self.output = rhi.newTexture(
                    QRhiTexture.Format.RGBA32F,
                    QSize(9, 6),
                    flags=QRhiTexture.Flag.RenderTarget
                    | QRhiTexture.Flag.UsedAsTransferSource,
                )
                for texture in (self.raw, self.field, self.output):
                    if not texture.create():
                        raise RuntimeError("Native float texture creation failed")
                    resources.append(texture)
                    counters["texture_creations"] += 1
                self.target = rhi.newTextureRenderTarget(
                    QRhiTextureRenderTargetDescription(QRhiColorAttachment(self.output))
                )
                self.pass_descriptor = self.target.newCompatibleRenderPassDescriptor()
                self.target.setRenderPassDescriptor(self.pass_descriptor)
                if not self.target.create():
                    raise RuntimeError("Float render target unavailable")
                filtering = (
                    QRhiSampler.Filter.Nearest
                    if sampling == "manual"
                    else QRhiSampler.Filter.Linear
                )
                self.sampler = rhi.newSampler(
                    filtering,
                    filtering,
                    QRhiSampler.Filter.None_,
                    QRhiSampler.AddressMode.ClampToEdge,
                    QRhiSampler.AddressMode.ClampToEdge,
                )
                if not self.sampler.create():
                    raise RuntimeError("Linear sampler creation failed")
                self.bindings = rhi.newShaderResourceBindings()
                self.bindings.setBindings(
                    [
                        QRhiShaderResourceBinding.sampledTexture(
                            0,
                            QRhiShaderResourceBinding.StageFlag.FragmentStage,
                            self.field,
                            self.sampler,
                        )
                    ]
                )
                if not self.bindings.create():
                    raise RuntimeError("Float sampling binding creation failed")
                self.pipeline = rhi.newGraphicsPipeline()
                self.pipeline.setShaderStages(
                    [
                        QRhiShaderStage(
                            QRhiShaderStage.Type.Vertex,
                            QShader.fromSerialized(shaders[0]),
                        ),
                        QRhiShaderStage(
                            QRhiShaderStage.Type.Fragment,
                            QShader.fromSerialized(shaders[1]),
                        ),
                    ]
                )
                self.pipeline.setShaderResourceBindings(self.bindings)
                self.pipeline.setRenderPassDescriptor(self.pass_descriptor)
                if not self.pipeline.create():
                    raise RuntimeError("Float sampling pipeline creation failed")
                resources.extend(
                    [
                        self.pass_descriptor,
                        self.target,
                        self.sampler,
                        self.bindings,
                        self.pipeline,
                    ]
                )
                self.pending = True
            except Exception as exc:
                errors.append(str(exc))

        def render(self, cb):
            counters["render_calls"] += 1
            try:
                if not errors and self.pending:
                    updates = self.rhi().nextResourceUpdateBatch()
                    for texture, payload in (
                        (self.raw, bits.tobytes()),
                        (self.field, field.tobytes()),
                    ):
                        desc = QRhiTextureSubresourceUploadDescription(payload)
                        updates.uploadTexture(
                            texture,
                            QRhiTextureUploadDescription(
                                QRhiTextureUploadEntry(0, 0, desc)
                            ),
                        )
                        counters["uploads"] += 1
                        counters["upload_bytes"] += len(payload)
                    updates.readBackTexture(self.raw, read_raw)
                    cb.beginPass(
                        self.target,
                        QColor(0, 0, 0, 0),
                        QRhiDepthStencilClearValue(1, 0),
                        updates,
                    )
                    cb.setGraphicsPipeline(self.pipeline)
                    cb.setViewport(QRhiViewport(0, 0, 9, 6))
                    cb.setShaderResources(self.bindings)
                    cb.draw(3)
                    cb.endPass()
                    readback = self.rhi().nextResourceUpdateBatch()
                    readback.readBackTexture(self.output, read_sample)
                    cb.resourceUpdate(readback)
                    self.pending = False
                cb.beginPass(
                    self.renderTarget(),
                    QColor("black"),
                    QRhiDepthStencilClearValue(1, 0),
                )
                cb.endPass()
            except Exception as exc:
                errors.append(str(exc))

        def releaseResources(self):
            while resources:
                resources.pop().destroy()

    app = QApplication([])
    widget = Widget()
    widget.setApi(QRhiWidget.Api.Metal)
    widget.resize(160, 100)
    widget.show()
    began = time.perf_counter()
    timer = QTimer()
    timer.setInterval(10)

    def poll():
        if errors or (read_raw.data.size() and read_sample.data.size()):
            app.quit()
        elif time.perf_counter() - began > 15:
            errors.append("Float texture readback timed out")
            app.quit()

    timer.timeout.connect(poll)
    timer.start()
    app.exec()
    timer.stop()
    try:
        if errors:
            raise RuntimeError("; ".join(errors))
        observed_bits = np.frombuffer(read_raw.data.data(), dtype="<u4")
        if not np.array_equal(observed_bits, bits):
            raise RuntimeError("R32F upload/readback did not preserve float32 bits")
        observed = np.frombuffer(read_sample.data.data(), dtype="<f4").reshape(6, 9, 4)
        expected = bilinear_reference(field, 9, 6)
        normal = float(np.max(np.abs(observed - expected)))
        flipped = float(np.max(np.abs(observed - expected[::-1])))
        error = min(normal, flipped)
        return dict(
            protocol="native-float-texture-e1-v1",
            sampling=sampling,
            status="passed" if error <= 2e-5 else "sampling_failed",
            sampling_error_limit=2e-5,
            qt=PySide6.__version__,
            backend=widget.backend,
            qt_platform=app.platformName(),
            python=platform.python_version(),
            texture_formats=["R32F", "RGBA32F"],
            transport_bits_match=True,
            max_sampling_error=error,
            observed_transport_bits=observed_bits.tolist(),
            sampled_values=observed.tolist(),
            framebuffer_y_flipped=flipped < normal,
            field_bytes=field.nbytes,
            equivalent_pr50_packed_bytes=field.size * 8,
            counters=counters,
            shader_integration_with_map_tested=False,
            missing_corner_policy_tested=False,
            end_to_end_speedup_measured=False,
        )
    finally:
        widget.hide()
        widget.releaseResources()
        widget.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sampling", choices=("linear", "manual"), default="linear")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="gwr-float-textures-") as temporary:
        shaders = compile_shaders(Path(temporary), args.sampling)
        result = run_probe(shaders, args.sampling)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if result["status"] != "passed":
        parser.exit(
            1,
            f"Native float {args.sampling} sampling error: {result['max_sampling_error']}\n",
        )


if __name__ == "__main__":
    main()
