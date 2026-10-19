"""E2c native two-resource indirection/readback; correctness, never map FPS."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np

SPEC = importlib.util.spec_from_file_location(
    "persistent_model", Path(__file__).with_name("experiment_persistent_weather.py")
)
model = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = model
SPEC.loader.exec_module(model)
SPEC = importlib.util.spec_from_file_location(
    "float_support", Path(__file__).with_name("experiment_float_textures.py")
)
floats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(floats)

FRAGMENT = """#version 440
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
layout(binding = 0) uniform sampler2D field;
layout(binding = 1) uniform sampler2D selection;
void main() {
    ivec2 size = textureSize(selection, 0);
    ivec2 cell = clamp(ivec2(uv * vec2(size)), ivec2(0), size - 1);
    vec4 pick = texelFetch(selection, cell, 0);
    vec2 wind = vec2(uintBitsToFloat(0x7fc00000u));
    if (pick.x >= 0.0) {
        int id = int(pick.x);
        int width = textureSize(field, 0).x;
        vec4 endpoints = texelFetch(field, ivec2(id % width, id / width), 0);
        if (pick.w <= 0.0) wind = endpoints.xy;
        else if (pick.w >= 1.0) wind = endpoints.zw;
        else wind = endpoints.xy + (endpoints.zw - endpoints.xy) * pick.w;
    }
    color = vec4(wind, pick.yz);
}
"""


def cases_from_payload(path, metadata):
    with np.load(path, allow_pickle=False) as data:
        field = model.Field(
            metadata["generation"],
            model.frozen(data["field"]),
            metadata["retained_nodes"],
        )
        cases = []
        for i, row in enumerate(metadata["views"]):
            selection = model.Selection(
                field.generation, model.frozen(data[f"selection_{i}"])
            )
            model.validate_binding(field, selection)
            cases.append(
                (
                    field,
                    selection,
                    np.array(data[f"expected_{i}"], copy=True),
                    "forecast",
                )
            )
    # A new generation exercises endpoint/missing semantics and binding replacement,
    # without interpreting missing data as a speed win on the real scene.
    first = np.array([[1, 2], [np.nan, np.nan], [3, 4], [np.nan, np.nan]], dtype="<f4")
    second = np.array([[np.nan, np.nan], [5, 6], [7, 8], [np.nan, np.nan]], dtype="<f4")
    control = model.Field.create("generated-missing-control", first, second)
    for weight in (0.0, 0.25, 1.0):
        values = np.array(cases[0][1].values, copy=True)
        values[..., 0] = -1
        values[..., 3] = weight
        values.reshape(-1, 4)[:4, 0] = np.arange(4)
        selection = model.Selection(control.generation, model.frozen(values))
        cases.append(
            (
                control,
                selection,
                model.reconstruct(control, selection),
                "generated-missing",
            )
        )
    return cases


def compare_pixels(actual, expected, flip):
    if flip:
        actual = actual[::-1]
    if not np.array_equal(np.isnan(actual), np.isnan(expected)):
        raise ValueError("GPU missing mask differs")
    if not np.array_equal(actual[..., 2:], expected[..., 2:]):
        raise ValueError("GPU anchors differ")
    finite = np.isfinite(expected[..., :2])
    error = float(
        np.max(np.abs(actual[..., :2][finite] - expected[..., :2][finite]), initial=0)
    )
    if error > 2e-5:
        raise ValueError(f"GPU vector error exceeds declared gate: {error}")
    return error


def run(cases, shaders):
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

    errors, resources, results = [], [], []
    counters = dict(
        field_uploads=0,
        field_upload_bytes=0,
        selection_uploads=0,
        selection_upload_bytes=0,
        texture_creations=0,
        resource_destroys=0,
    )
    height, width, _ = cases[0][1].values.shape
    readback = QRhiReadbackResult()

    class Widget(QRhiWidget):
        def initialize(self, _cb):
            try:
                rhi = self.rhi()
                if rhi.backend() != QRhi.Implementation.Metal:
                    raise ValueError(
                        "E2c requires native Metal for this bounded attempt"
                    )
                self.backend = rhi.backend().name
                self.number, self.waiting, self.generation = 0, False, None
                self.flip = None
                # Both field generations fit in a single reserved field allocation.
                field_height = max(c[0].values.shape[0] for c in cases)
                self.field = rhi.newTexture(
                    QRhiTexture.Format.RGBA32F, QSize(64, field_height)
                )
                self.selection = rhi.newTexture(
                    QRhiTexture.Format.RGBA32F, QSize(width, height)
                )
                self.output = rhi.newTexture(
                    QRhiTexture.Format.RGBA32F,
                    QSize(width, height),
                    flags=QRhiTexture.Flag.RenderTarget
                    | QRhiTexture.Flag.UsedAsTransferSource,
                )
                reservation = 2 * (64 * field_height * 16 + width * height * 32)
                if reservation > model.LIMIT:
                    raise ValueError("GPU reservation exceeds recorded limit")
                self.reservation = reservation
                for texture in (self.field, self.selection, self.output):
                    if not texture.create():
                        raise RuntimeError("Texture creation failed")
                    resources.append(texture)
                    counters["texture_creations"] += 1
                self.target = rhi.newTextureRenderTarget(
                    QRhiTextureRenderTargetDescription(QRhiColorAttachment(self.output))
                )
                self.pass_descriptor = self.target.newCompatibleRenderPassDescriptor()
                self.target.setRenderPassDescriptor(self.pass_descriptor)
                resources.extend((self.pass_descriptor, self.target))
                if not self.target.create():
                    raise RuntimeError("Indirection target creation failed")
                self.sampler = rhi.newSampler(
                    QRhiSampler.Filter.Nearest,
                    QRhiSampler.Filter.Nearest,
                    QRhiSampler.Filter.None_,
                    QRhiSampler.AddressMode.ClampToEdge,
                    QRhiSampler.AddressMode.ClampToEdge,
                )
                resources.append(self.sampler)
                if not self.sampler.create():
                    raise RuntimeError("Nearest sampler creation failed")
                self.bindings = rhi.newShaderResourceBindings()
                self.bindings.setBindings(
                    [
                        QRhiShaderResourceBinding.sampledTexture(
                            i,
                            QRhiShaderResourceBinding.StageFlag.FragmentStage,
                            texture,
                            self.sampler,
                        )
                        for i, texture in enumerate((self.field, self.selection))
                    ]
                )
                resources.append(self.bindings)
                if not self.bindings.create():
                    raise RuntimeError("Indirection bindings failed")
                self.pipeline = rhi.newGraphicsPipeline()
                self.pipeline.setShaderStages(
                    [
                        QRhiShaderStage(kind, QShader.fromSerialized(shader))
                        for kind, shader in zip(
                            (
                                QRhiShaderStage.Type.Vertex,
                                QRhiShaderStage.Type.Fragment,
                            ),
                            shaders,
                        )
                    ]
                )
                self.pipeline.setShaderResourceBindings(self.bindings)
                self.pipeline.setRenderPassDescriptor(self.pass_descriptor)
                resources.append(self.pipeline)
                if not self.pipeline.create():
                    raise RuntimeError("Indirection pipeline failed")
            except Exception as exc:
                errors.append(str(exc))

        def render(self, cb):
            try:
                if not errors and not self.waiting and self.number < len(cases):
                    field, selection, _expected, _kind = cases[self.number]
                    model.validate_binding(field, selection)
                    batch = self.rhi().nextResourceUpdateBatch()
                    uploads = [(self.selection, selection.values, "selection")]
                    if self.generation != field.generation:
                        payload = np.full(
                            (self.field.pixelSize().height(), 64, 4),
                            np.nan,
                            dtype="<f4",
                        )
                        payload[: field.values.shape[0]] = field.values
                        uploads.insert(0, (self.field, payload, "field"))
                    # Validated field and selection updates precede ONE draw in
                    # the same command-buffer transaction. No CPU symbol layer.
                    for texture, values, kind in uploads:
                        desc = QRhiTextureSubresourceUploadDescription(values.tobytes())
                        batch.uploadTexture(
                            texture,
                            QRhiTextureUploadDescription(
                                QRhiTextureUploadEntry(0, 0, desc)
                            ),
                        )
                        counters[kind + "_uploads"] += 1
                        counters[kind + "_upload_bytes"] += values.nbytes
                    cb.beginPass(
                        self.target,
                        QColor(0, 0, 0, 0),
                        QRhiDepthStencilClearValue(1, 0),
                        batch,
                    )
                    cb.setGraphicsPipeline(self.pipeline)
                    cb.setViewport(QRhiViewport(0, 0, width, height))
                    cb.setShaderResources(self.bindings)
                    cb.draw(3)
                    cb.endPass()
                    transfer = self.rhi().nextResourceUpdateBatch()
                    transfer.readBackTexture(self.output, readback)
                    cb.resourceUpdate(transfer)
                    self.waiting = True
                    self.generation = field.generation
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
                counters["resource_destroys"] += 1

    app = QApplication([])
    widget = Widget()
    widget.setApi(QRhiWidget.Api.Metal)
    widget.resize(160, 100)
    widget.show()
    began = time.perf_counter()
    timer = QTimer()
    timer.setInterval(5)

    def poll():
        try:
            if errors:
                app.quit()
                return
            if widget.waiting and readback.data.size():
                actual = (
                    np.frombuffer(readback.data.data(), dtype="<f4")
                    .reshape(height, width, 4)
                    .copy()
                )
                _field, _selection, expected, kind = cases[widget.number]
                if widget.flip is None:
                    widget.flip = not np.array_equal(actual[..., 2:], expected[..., 2:])
                error = compare_pixels(actual, expected, widget.flip)
                results.append(
                    dict(
                        case=widget.number,
                        kind=kind,
                        max_vector_error=error,
                        masks_match=True,
                        anchors_match=True,
                    )
                )
                readback.data.clear()
                widget.waiting = False
                widget.number += 1
                if widget.number == len(cases):
                    app.quit()
                else:
                    widget.update()
            if time.perf_counter() - began > 30:
                raise RuntimeError("Persistent GPU readback timed out")
        except Exception as exc:
            errors.append(str(exc))
            app.quit()

    timer.timeout.connect(poll)
    timer.start()
    app.exec()
    timer.stop()
    try:
        if errors:
            raise RuntimeError("; ".join(errors))
        if len(results) != len(cases) or counters["field_uploads"] != 2:
            raise ValueError("Missing cases or unexpected field replacement")
        result = dict(
            protocol="persistent-field-indirection-e2c-v1",
            backend=widget.backend,
            qt=PySide6.__version__,
            qt_platform=app.platformName(),
            cases=results,
            reserved_texture_bytes=widget.reservation,
            texture_byte_limit=model.LIMIT,
            framebuffer_y_flipped=widget.flip,
            vector_error_limit=2e-5,
            map_integration_tested=False,
            app_atomic_handoff_tested=False,
            presentation_measured=False,
            end_to_end_speedup_measured=False,
        )
    finally:
        widget.hide()
        widget.releaseResources()
        widget.close()
    result["counters"] = counters
    result["resources_released"] = not resources
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metadata = json.loads(args.prepared.read_text())
    payload = args.prepared.with_suffix(".npz")
    if hashlib.sha256(payload.read_bytes()).hexdigest() != metadata["payload_sha256"]:
        raise ValueError("Prepared payload changed")
    cases = cases_from_payload(payload, metadata)
    import PySide6

    with tempfile.TemporaryDirectory(prefix="gwr-persistent-shaders-") as temporary:
        shaders = []
        for suffix, source in (("vert", floats.VERTEX), ("frag", FRAGMENT)):
            path = Path(temporary) / ("persistent." + suffix)
            path.write_text(source)
            baked = path.with_suffix(path.suffix + ".qsb")
            subprocess.run(
                [
                    str(floats.find_qsb(Path(PySide6.__file__).parent)),
                    "--msl",
                    "12",
                    "-o",
                    str(baked),
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=20,
            )
            shaders.append(baked.read_bytes())
        result = run(cases, shaders)
    result.update(
        snapshot_sha256=metadata["snapshot_sha256"],
        payload_sha256=metadata["payload_sha256"],
        harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
