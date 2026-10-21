"""#64 compatibility probe: a real QQuickItem supplies owned packed textures.

Experiment only. No app renderer imports this module. One synthetic scene checks
Python virtual dispatch, A/B/A native retention, finite/missing pixels, callback
threads and explicit retirement/invalidation. LLDB observes real QRhi upload
submissions; framebuffer readback is correctness evidence, never frame timing.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import RLock, get_ident
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

QML = """import QtQuick
Item {
    property var fieldItem: null
    property bool compiled: effect.status === ShaderEffect.Compiled
    property string shaderError: effect.log
    ShaderEffect {
        id: effect
        anchors.fill: parent
        visible: fieldItem !== null
        property variant field: fieldItem
        fragmentShader: "field.frag.qsb"
    }
}
"""

FRAGMENT = """#version 440
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf { mat4 qt_Matrix; float qt_Opacity; };
layout(binding = 1) uniform sampler2D field;
void main() {
    // Same opaque two-texel float32 packing as gpuweather.encode_component.
    uvec3 low = uvec3(round(texelFetch(field, ivec2(0, 0), 0).rgb * 255.0));
    uint high = uint(round(texelFetch(field, ivec2(1, 0), 0).r * 255.0));
    uint bits = low.r | (low.g << 8) | (low.b << 16) | (high << 24);
    if ((bits & 0x7f800000u) == 0x7f800000u) discard;
    float value = uintBitsToFloat(bits);
    fragColor = vec4(value / 8.0, 0.25, 0.5, 1.0) * qt_Opacity;
}
"""


def pixel_check(pixels, key):
    import numpy as np

    if not pixels.size:
        return False
    if key == "missing":
        return bool(np.count_nonzero(pixels[..., 3]) == 0)
    expected = np.array([255 if key == "A" else 128, 64, 128, 255], dtype=int)
    return bool(np.max(np.abs(pixels.astype(int) - expected)) <= 1)


def trace_checks(events, phases):
    uploads = [event for event in events if event["event"] == "uploadTexture"]
    counts = []
    for phase in phases:
        counts.append(
            sum(
                event["texture"] == phase["rhi"]
                and phase["began"] <= event["time"] <= phase["ended"]
                for event in uploads
            )
        )
    return {
        "configured": any(event["event"] == "configured" for event in events),
        "phase_field_upload_submissions": counts,
        "warm_positive": len(counts) >= 2 and counts[0] >= 1 and counts[1] >= 1,
        "return_to_A_zero": len(counts) >= 3 and counts[2] == 0,
    }


def adopt_texture(texture):
    """Detach PySide's factory-return parenting through the public QObject API.

    createTextureFromImage's generated binding associates the returned wrapper
    with its window, even when QObject.parent() is null. The explicit owner must
    outlive window-wrapper invalidation long enough to delete on the render
    thread. Never revive an invalid wrapper or assume a Python reference does it.
    """
    import shiboken6

    def snapshot():
        valid = shiboken6.isValid(texture)
        return {
            "valid": valid,
            "python_owned": shiboken6.ownedByPython(texture) if valid else False,
            "native_parent_is_none": texture.parent() is None if valid else False,
            "wrapper_dump": shiboken6.dump(texture),
        }

    before = snapshot()
    if not before["valid"]:
        raise RuntimeError("cannot adopt an invalid native texture")
    texture.setParent(None)
    after = snapshot()
    if not all(
        after[name] for name in ("valid", "python_owned", "native_parent_is_none")
    ):
        raise RuntimeError(
            "public QObject unparenting did not establish texture ownership"
        )
    return {"before": before, "after": after}


def build_scene(directory):
    import PySide6

    package = Path(PySide6.__file__).parent
    candidates = [package / name for name in ("qsb", "Qt/libexec/qsb", "Qt/bin/qsb")]
    qsb = next((candidate for candidate in candidates if candidate.is_file()), None)
    if qsb is None:
        raise RuntimeError("installed shader baker not available; do not install one")
    shader = directory / "field.frag"
    shader.write_text(FRAGMENT)
    subprocess.run(
        [str(qsb), "--msl", "12", "-o", str(shader) + ".qsb", str(shader)],
        capture_output=True,
        check=True,
        timeout=20,
    )
    qml = directory / "scene.qml"
    qml.write_text(QML)
    return qml


def run_scene(directory):
    import numpy as np
    import shiboken6
    from PySide6.QtCore import (
        QCoreApplication,
        QEvent,
        QEventLoop,
        Qt,
        QTimer,
        QUrl,
        qVersion,
    )
    from PySide6.QtGui import QImage
    from PySide6.QtQuick import (
        QQuickItem,
        QQuickWindow,
        QSGNode,
        QSGTexture,
        QSGTextureProvider,
    )
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QApplication

    from gweatherrouting.qt.gpuweather import encode_component

    class Bank:
        def __init__(self):
            self.lock = RLock()
            self.entries = {}
            self.current = None
            self.provider = None
            self.events = []
            self.errors = []
            self.destroyed = set()
            self.created = 0
            self.render_thread = None
            self.invalidations = 0
            self.frames = 0
            self.provider_queries = 0
            self.texture_queries = 0
            self.retire_requested = False
            self.ownership_adoptions = []
            self.retirements = []

        def note(self, operation):
            thread = get_ident()
            self.events.append({"operation": operation, "thread": thread})
            if self.render_thread is None or thread != self.render_thread:
                self.errors.append(
                    f"{operation} outside established render callback thread"
                )

        def synchronize(self):
            self.render_thread = get_ident()

        def ensure_provider(self):
            if self.provider is None:
                self.note("create_provider")
                self.provider = Provider()
            return self.provider

        def apply(self, key):
            with self.lock:
                self.note("apply")
                provider = self.ensure_provider()
                if key is not None and key not in self.entries:
                    values = np.full(
                        (2, 2),
                        {"A": 8.0, "B": 4.0, "missing": np.nan}[key],
                        dtype=np.float32,
                    )
                    image = encode_component(values)
                    texture = window.createTextureFromImage(
                        image, QQuickWindow.CreateTextureOption.TextureIsOpaque
                    )
                    if texture is None:
                        raise RuntimeError("packed native texture allocation failed")
                    self.created += 1
                    token = self.created
                    texture.destroyed.connect(
                        lambda *_args, t=token: self.on_destroyed(t),
                        Qt.ConnectionType.DirectConnection,
                    )
                    self.entries[key] = {
                        "texture": texture,
                        "token": token,
                        "rhi": None,
                    }
                    self.ownership_adoptions.append(
                        {"token": token, **adopt_texture(texture)}
                    )
                    texture.setFiltering(QSGTexture.Filtering.Nearest)
                    self.note("create_texture")
                self.current = key
                provider.textureChanged.emit()

        def on_destroyed(self, token):
            self.note("destroy_texture")
            self.destroyed.add(token)

        def observe(self):
            with self.lock:
                self.frames += 1
                for entry in self.entries.values():
                    texture = entry["texture"]
                    if not shiboken6.isValid(texture):
                        self.errors.append(
                            "Qt destroyed a bank-owned texture while it was resident"
                        )
                        continue
                    entry["atlas"] = texture.isAtlasTexture()
                    if not shiboken6.ownedByPython(texture):
                        self.errors.append(
                            "resident bank-owned texture lost Python ownership"
                        )
                    native = texture.rhiTexture()
                    if native is not None:
                        entry["rhi"] = shiboken6.getCppPointer(native)[0]

        def retire(self, invalidation=False):
            with self.lock:
                if not invalidation and self.current is not None:
                    self.errors.append(
                        "refused retirement of a currently bound texture"
                    )
                    return
                self.note("invalidate" if invalidation else "retire")
                if invalidation:
                    self.invalidations += 1
                self.current = None
                if self.provider is not None and not invalidation:
                    self.provider.textureChanged.emit()
                for entry in self.entries.values():
                    texture = entry["texture"]
                    valid = shiboken6.isValid(texture)
                    self.retirements.append(
                        {
                            "token": entry["token"],
                            "boundary": (
                                "scene_invalidation" if invalidation else "detach"
                            ),
                            "valid_before_delete": valid,
                            "python_owned": (
                                shiboken6.ownedByPython(texture) if valid else False
                            ),
                        }
                    )
                    if valid:
                        shiboken6.delete(texture)
                    else:
                        self.errors.append(
                            "resident texture ownership was lost before explicit retirement"
                        )
                self.entries.clear()
                if invalidation and self.provider is not None:
                    if shiboken6.isValid(self.provider):
                        shiboken6.delete(self.provider)
                    self.provider = None

        def invalidated(self):
            self.retire(invalidation=True)

        def after_sync(self):
            # QQuickWidget uses render control: a scheduled render-stage job
            # need not execute during an empty offscreen frame. Consume an
            # explicit request after SG synchronization, when the detached
            # consumer's changes have reached the render graph.
            if self.retire_requested:
                self.retire_requested = False
                self.retire()

    bank = Bank()

    class Provider(QSGTextureProvider):
        def texture(self):
            with bank.lock:
                bank.note("texture_virtual")
                bank.texture_queries += 1
                entry = bank.entries.get(bank.current)
                if entry is None:
                    return None
                if not shiboken6.isValid(entry["texture"]):
                    bank.errors.append("provider refused a destroyed resident texture")
                    return None
                return entry["texture"]

    class Source(QQuickItem):
        def __init__(self, parent):
            super().__init__(parent)
            self.setFlag(QQuickItem.Flag.ItemHasContents, True)
            self.setWidth(1)
            self.setHeight(1)
            self.pending = None
            self.applied = "not-applied"
            self.node = None

        def isTextureProvider(self):  # noqa: N802 - Qt virtual
            return True

        def textureProvider(self):  # noqa: N802 - Qt virtual
            with bank.lock:
                bank.note("provider_virtual")
                bank.provider_queries += 1
                return bank.ensure_provider()

        def updatePaintNode(self, old, _data):  # noqa: N802 - Qt virtual
            try:
                bank.apply(self.pending)
                self.applied = self.pending
                self.node = old if old is not None else QSGNode()
                return self.node
            except Exception as exc:
                bank.errors.append(str(exc))
                return old

        def activate(self, key):
            self.pending = key
            self.update()

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    quick = QQuickWidget()
    quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    quick.setClearColor(Qt.GlobalColor.transparent)
    quick.resize(64, 48)
    window = quick.quickWindow()
    window.beforeSynchronizing.connect(
        bank.synchronize, Qt.ConnectionType.DirectConnection
    )
    window.afterSynchronizing.connect(
        bank.after_sync, Qt.ConnectionType.DirectConnection
    )
    window.afterRendering.connect(bank.observe, Qt.ConnectionType.DirectConnection)
    window.sceneGraphInvalidated.connect(
        bank.invalidated, Qt.ConnectionType.DirectConnection
    )
    quick.setSource(QUrl.fromLocalFile(str(build_scene(directory))))
    root = quick.rootObject()
    if root is None:
        raise RuntimeError(str(quick.errors()))
    source = Source(root)
    root.setProperty("fieldItem", source)
    quick.show()

    def pump(milliseconds=30):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def grab(key):
        source.activate(key)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            quick.update()
            pump()
            if source.applied == key and root.property("compiled"):
                break
        image = quick.grabFramebuffer().convertToFormat(QImage.Format.Format_RGBA8888)
        if image.isNull():
            raise RuntimeError("native item framebuffer readback failed")
        pixels = (
            np.frombuffer(image.constBits(), dtype=np.uint8)
            .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
            .reshape(image.height(), image.width(), 4)
            .copy()
        )
        with bank.lock:
            entry = bank.entries.get(key)
            return pixels, (
                None
                if entry is None
                else {
                    "token": entry["token"],
                    "rhi": entry["rhi"],
                    "atlas": entry.get("atlas", True),
                }
            )

    report = {"phases": [], "checks": {}}
    try:
        hashes = []
        for key in ("A", "B", "A", "missing", "A"):
            began = time.time()
            pixels, identity = grab(key)
            report["phases"].append(
                {
                    "key": key,
                    "began": began,
                    "ended": time.time(),
                    "rhi": None if identity is None else identity["rhi"],
                    "identity": identity,
                    "pixel_control": pixel_check(pixels, key),
                    "alpha_pixels": int(np.count_nonzero(pixels[..., 3])),
                    "center_pixel": pixels[
                        pixels.shape[0] // 2, pixels.shape[1] // 2
                    ].tolist(),
                    "created_textures": bank.created,
                }
            )
            hashes.append(hashlib.sha256(pixels.tobytes()).hexdigest())
            if identity is None or bank.errors:
                break
        phases = report["phases"]
        report["checks"].update(
            shader_compiled=bool(root.property("compiled")),
            normal_global_atlas_settings=all(
                os.environ.get(name) is None
                for name in (
                    "QSG_ATLAS_SIZE_LIMIT",
                    "QSG_ATLAS_WIDTH",
                    "QSG_ATLAS_HEIGHT",
                )
            ),
            provider_dispatch=bank.provider_queries > 0 and bank.texture_queries > 0,
            complete_sequence=len(phases) == 5,
            pixel_controls=len(phases) == 5 and all(p["pixel_control"] for p in phases),
            non_atlased=len(phases) == 5
            and all(
                p["identity"] is not None and not p["identity"]["atlas"] for p in phases
            ),
            same_A_identity=len(phases) == 5
            and phases[0]["identity"] == phases[2]["identity"] == phases[4]["identity"],
            same_A_pixels=len(hashes) == 5 and hashes[0] == hashes[2] == hashes[4],
            no_recreation_on_return=len(phases) == 5
            and phases[2]["created_textures"] == 2,
        )
        root.setProperty("fieldItem", None)
        source.activate(None)
        quick.update()
        pump(100)
        quick.grabFramebuffer()
        bank.retire_requested = True
        source.update()
        deadline = time.monotonic() + 2
        while bank.retire_requested and time.monotonic() < deadline:
            quick.update()
            pump()
            quick.grabFramebuffer()
        report["detach"] = {
            "retirement_request_consumed": not bank.retire_requested,
            "current": bank.current,
            "live_entries": len(bank.entries),
            "created_textures": bank.created,
            "destroyed_textures": len(bank.destroyed),
        }
        report["checks"]["detached_resources_released"] = (
            not bank.entries and len(bank.destroyed) == bank.created
        )
        root.setProperty("fieldItem", source)
        pixels, _identity = grab("A")
        report["checks"]["reload_after_detach"] = pixel_check(pixels, "A")
        report["reload"] = {
            "identity": _identity,
            "center_pixel": pixels[pixels.shape[0] // 2, pixels.shape[1] // 2].tolist(),
            "created_textures": bank.created,
        }
        report["host"] = {
            "platform": app.platformName(),
            "qt_version": qVersion(),
            "graphics_api": window.rendererInterface().graphicsApi().name,
            "device_pixel_ratio": quick.devicePixelRatioF(),
            "global_atlas_size_limit": os.environ.get("QSG_ATLAS_SIZE_LIMIT"),
        }
    finally:
        quick.close()
        quick.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        report["checks"]["scene_invalidated"] = bank.invalidations > 0
        report["checks"]["all_native_resources_released"] = (
            not bank.entries
            and len(bank.destroyed) == bank.created
            and bank.provider is None
        )
        report["checks"]["callback_threads_and_ownership"] = not bank.errors
        report["checks"]["explicit_wrapper_ownership"] = len(
            bank.ownership_adoptions
        ) == bank.created > 0 and all(
            record["after"]["python_owned"] for record in bank.ownership_adoptions
        )
        report["checks"]["valid_at_retirement"] = len(
            bank.retirements
        ) == bank.created > 0 and all(
            record["valid_before_delete"] and record["python_owned"]
            for record in bank.retirements
        )
        report["ownership_adoptions"] = bank.ownership_adoptions
        report["retirements"] = bank.retirements
        report["callback_events"] = bank.events
        report["created_textures"] = bank.created
        report["destroyed_texture_count"] = len(bank.destroyed)
        report["scene_invalidations"] = bank.invalidations
        report["render_thread"] = bank.render_thread
        report["gui_thread"] = get_ident()
        report["errors"] = bank.errors
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("GWR_NATIVE_ITEM_CHILD"):
        trace_path = args.output.with_suffix(".uploads.jsonl")
        trace_path.write_text("")
        result = subprocess.run(
            [
                "xcrun",
                "lldb",
                "--no-lldbinit",
                "--batch",
                "-o",
                "settings set target.disable-aslr false",
                "-o",
                "settings set target.process.stop-on-exec false",
                "-o",
                f"command script import {ROOT / 'tools/qrhi_upload_trace.py'}",
                "-o",
                "run",
                "-o",
                "script qrhi_upload_trace.write_process_status(lldb.debugger)",
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                "--output",
                str(args.output),
            ],
            env=dict(
                os.environ,
                GWR_NATIVE_ITEM_CHILD="1",
                GWR_UPLOAD_TRACE_OUTPUT=str(trace_path),
            ),
            timeout=100,
            check=False,
        )
        if result.returncode or not args.output.is_file():
            raise RuntimeError("native item probe did not produce a report")
        report = json.loads(args.output.read_text())
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        native_exit = next((e for e in events if e["event"] == "process_status"), {})
        report["native_exit"] = native_exit
        trace = trace_checks(events, report["phases"])
        report["upload_trace"] = trace
        report["checks"].update(
            clean_native_exit=native_exit.get("exited") is True
            and native_exit.get("exit_code") == 0,
            upload_trace_positive=trace["configured"] and trace["warm_positive"],
            no_field_upload_on_return=trace["configured"]
            and trace["warm_positive"]
            and trace["return_to_A_zero"],
        )
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        return
    with tempfile.TemporaryDirectory(prefix="gwr-native-field-item-") as tmp:
        report = run_scene(Path(tmp))
    report["presentation_latency_measured"] = False
    report["physical_vram_measured"] = False
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
