"""N1 gate: the production native store in two widgets sharing a small budget.

Known packed synthetic fields isolate ownership/admission from GRIB preparation.
Actual QRhi submissions are traced in one fresh native application process.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

QML = """import QtQuick
Item {
    id: root
    property var source0: null
    property var source1: null
    property var source2: null
    property var source3: null
    property bool fieldReady: false
    property bool compiled: effect.status === ShaderEffect.Compiled
    property int shaderStatus: effect.status
    property string shaderError: effect.log
    property bool shaderFailed: effect.status === ShaderEffect.Error
    property string fragmentFile: "field.frag.qsb"
    property bool plainControl: false
    Rectangle {
        anchors.fill: parent
        visible: root.plainControl
        color: "#2050c0"
    }
    ShaderEffect {
        id: effect
        objectName: "fieldEffect"
        anchors.fill: parent
        visible: fieldReady && !root.plainControl
        property variant u0: source0
        property variant v0: source1
        property variant u1: source2
        property variant v1: source3
        fragmentShader: root.fragmentFile
    }
}
"""

FRAGMENT = """#version 440
layout(location=0) in vec2 qt_TexCoord0;
layout(location=0) out vec4 fragColor;
layout(std140,binding=0) uniform buf { mat4 qt_Matrix; float qt_Opacity; };
layout(binding=1) uniform sampler2D u0;
layout(binding=2) uniform sampler2D v0;
layout(binding=3) uniform sampler2D u1;
layout(binding=4) uniform sampler2D v1;
uint readBits(sampler2D field) {
    uvec3 lo = uvec3(round(texelFetch(field,ivec2(0,0),0).rgb*255.0));
    uint hi = uint(round(texelFetch(field,ivec2(1,0),0).r*255.0));
    return lo.r | (lo.g<<8) | (lo.b<<16) | (hi<<24);
}
void main() {
    uvec4 bits = uvec4(readBits(u0), readBits(v0), readBits(u1), readBits(v1));
    // Match the existing A0k pattern: reject missing bits before arithmetic.
    if (any(equal(bits & uvec4(0x7f800000u), uvec4(0x7f800000u)))) discard;
    vec4 values = uintBitsToFloat(bits);
    float value = (values.x+values.y+values.z+values.w)/32.0;
    fragColor = vec4(value,0.25,0.5,1.0)*qt_Opacity;
}
"""


def ready_for_readback(endpoint_ready, group, shader_failed):
    """A cached ShaderEffect may draw while its status remains Uncompiled.

    Do not certify pixels here: the caller must still compare the framebuffer
    against known content. Missing native endpoints or explicit errors fail.
    """
    return bool(
        endpoint_ready
        and group is not None
        and len(group["handles"]) == 4
        and all(group["handles"])
        and not shader_failed
    )


def run(directory):
    import numpy as np
    import PySide6
    import shiboken6
    from PySide6.QtCore import (
        QCoreApplication,
        QEvent,
        QEventLoop,
        QTimer,
        Qt,
        QUrl,
        qVersion,
    )
    from PySide6.QtGui import QImage
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

    from gweatherrouting.qt.gpuweather import encode_component
    from gweatherrouting.qt.gpuweathertextures import (
        NativeWeatherBudget,
        native_texture_context,
    )

    baker = next(
        (
            Path(PySide6.__file__).parent / n
            for n in ("qsb", "Qt/libexec/qsb", "Qt/bin/qsb")
            if (Path(PySide6.__file__).parent / n).is_file()
        ),
        None,
    )
    if baker is None:
        raise RuntimeError("Installed shader baker unavailable")
    (directory / "field.frag").write_text(FRAGMENT)
    subprocess.run(
        [
            str(baker),
            "--msl",
            "12",
            "-o",
            str(directory / "field.frag.qsb"),
            str(directory / "field.frag"),
        ],
        timeout=20,
        capture_output=True,
        check=True,
    )
    (directory / "scene.qml").write_text(QML)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    host = QWidget()
    layout = QHBoxLayout(host)
    groups = {
        name: tuple(
            encode_component(np.full((2, 2), value, np.float32)) for _ in range(4)
        )
        for name, value in {"A": 8, "B": 4, "C": 2, "missing": np.nan}.items()
    }
    group_bytes = sum(image.sizeInBytes() for image in groups["A"])
    budget = NativeWeatherBudget(group_bytes * 3)
    widgets, contexts, roots = [], [], []
    report = {
        "checks": {
            "normal_global_atlas_settings": all(
                os.environ.get(name) is None
                for name in (
                    "QSG_ATLAS_SIZE_LIMIT",
                    "QSG_ATLAS_WIDTH",
                    "QSG_ATLAS_HEIGHT",
                )
            )
        },
        "phases": [],
        "group_bytes": group_bytes,
    }

    def pump():
        loop = QEventLoop()
        QTimer.singleShot(30, loop.quit)
        loop.exec()

    def group_snapshot(index, name):
        return next(
            (
                g
                for g in contexts[index].snapshot().get("groups", [])
                if g["identity"] == repr(name)
            ),
            None,
        )

    def settle(index, name):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            widgets[index].update()
            pump()
            # This is a correctness/readback gate, not presentation timing.
            # Force the widget's offscreen frame before waiting for uploaded
            # resource handles: an occluded/idle second widget may otherwise
            # never paint the frame that would satisfy this readiness check.
            widgets[index].grabFramebuffer()
            group = group_snapshot(index, name)
            if ready_for_readback(
                contexts[index].ready, group, roots[index].property("shaderFailed")
            ):
                return group
        return None

    def pixels(index):
        image = (
            widgets[index]
            .grabFramebuffer()
            .convertToFormat(QImage.Format.Format_RGBA8888)
        )
        if image.isNull():
            raise RuntimeError("Native-store framebuffer unavailable")
        return (
            np.frombuffer(image.constBits(), np.uint8)
            .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
            .reshape(image.height(), image.width(), 4)
            .copy()
        )

    def valid_pixels(index, name):
        data = pixels(index)
        if name == "missing":
            return bool(np.count_nonzero(data[..., 3]) == 0)
        red = {"A": 255, "B": 128, "C": 64}[name]
        return bool(
            np.max(np.abs(data.astype(int) - np.array([red, 64, 128, 255]))) <= 1
        )

    def diagnostic_state(index):
        from PySide6.QtCore import QObject

        root, quick = roots[index], widgets[index]
        effect = root.findChild(QObject, "fieldEffect")
        data = pixels(index)
        return {
            "widget_size": [quick.width(), quick.height()],
            "widget_visible": quick.isVisible(),
            "root_size": [root.width(), root.height()],
            "root_visible": root.isVisible(),
            "effect_size": [effect.width(), effect.height()],
            "effect_visible": effect.isVisible(),
            "shader_status": root.property("shaderStatus"),
            "shader_error": root.property("shaderError"),
            "compiled": bool(root.property("compiled")),
            "field_ready": bool(root.property("fieldReady")),
            "center_pixel": data[data.shape[0] // 2, data.shape[1] // 2].tolist(),
            "context": contexts[index].snapshot(),
        }

    def diagnose_second_widget():
        records = [{"control": "original", **diagnostic_state(1)}]
        root, quick = roots[1], widgets[1]
        # Independent controls, all in the existing two-widget process. No
        # renderer replacement, pixel-gate weakening or new texture transport.
        root.setWidth(quick.width())
        root.setHeight(quick.height())
        settle(1, "A")
        records.append({"control": "explicit_root_geometry", **diagnostic_state(1)})
        root.setProperty("plainControl", True)
        for _ in range(3):
            quick.update()
            pump()
            quick.grabFramebuffer()
        plain = diagnostic_state(1)
        plain["known_plain_pixel"] = all(
            abs(a - b) <= 1 for a, b in zip(plain["center_pixel"], [32, 80, 192, 255])
        )
        records.append({"control": "plain_rectangle", **plain})
        root.setProperty("plainControl", False)
        settle(1, "A")
        records.append(
            {"control": "reactivate_original_shader_url", **diagnostic_state(1)}
        )
        original = directory / "field.frag.qsb"
        unique = directory / "field-second.frag.qsb"
        unique.write_bytes(original.read_bytes())
        root.setProperty("fragmentFile", unique.name)
        settle(1, "A")
        records.append(
            {
                "control": "identical_shader_distinct_url",
                "shader_bytes_equal": original.read_bytes() == unique.read_bytes(),
                **diagnostic_state(1),
            }
        )
        return records

    def phase(index, name):
        began = time.time()
        warm = contexts[index].request(name, groups[name])
        roots[index].setProperty("fieldReady", warm)
        group = settle(index, name)
        correct = group is not None and valid_pixels(index, name)
        record = {
            "map": index,
            "field": name,
            "began": began,
            "ended": time.time(),
            "warm_claim": warm,
            "group": group,
            "pixels_correct": correct,
            "ready": contexts[index].ready,
            "qml_field_ready": bool(roots[index].property("fieldReady")),
            "shader_compiled": bool(roots[index].property("compiled")),
            "context_snapshot": contexts[index].snapshot(),
            "budget": budget.statistics(),
        }
        report["phases"].append(record)
        return record

    try:
        for _ in range(2):
            quick = QQuickWidget(host)
            quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
            quick.setClearColor(Qt.GlobalColor.transparent)
            quick.setFixedSize(64, 48)
            quick.setSource(QUrl.fromLocalFile(str(directory / "scene.qml")))
            root = quick.rootObject()
            if root is None:
                raise RuntimeError(str(quick.errors()))
            context = native_texture_context(quick.quickWindow(), root, budget)
            for index, source in enumerate(context.sources):
                root.setProperty(f"source{index}", source)
            context.stateChanged.connect(
                lambda status, item=root: (
                    item.setProperty("fieldReady", status["ready"])
                    if shiboken6.isValid(item)
                    else None
                )
            )
            widgets.append(quick)
            roots.append(root)
            contexts.append(context)
            layout.addWidget(quick)
        host.show()
        first = phase(0, "A")
        sibling = phase(1, "A")
        report["checks"]["two_maps_draw"] = (
            first["pixels_correct"] and sibling["pixels_correct"]
        )
        if not report["checks"]["two_maps_draw"]:
            report["stopped_at"] = (
                "two-map known-pixel readiness; later cases not exercised"
            )
            report["host"] = {
                "platform": app.platformName(),
                "graphics_api": widgets[0]
                .quickWindow()
                .rendererInterface()
                .graphicsApi()
                .name,
                "qt_version": qVersion(),
                "device_pixel_ratio": widgets[0].devicePixelRatioF(),
            }
            if os.environ.get("GWR_NATIVE_STORE_DIAGNOSTICS") == "1":
                report["two_widget_diagnostics"] = diagnose_second_widget()
            return report
        report["checks"]["context_handles_separate"] = not set(
            first["group"]["handles"]
        ) & set(sibling["group"]["handles"])
        before_b = phase(0, "B")
        returned = phase(0, "A")
        report["checks"]["warm_A_reused"] = (
            returned["warm_claim"]
            and returned["group"] == first["group"]
            and returned["pixels_correct"]
        )
        pressure = phase(0, "C")
        report["checks"]["inactive_B_evicted"] = (
            pressure["pixels_correct"] and group_snapshot(0, "B") is None
        )
        reloaded = phase(0, "B")
        report["checks"]["evicted_B_reloaded"] = (
            reloaded["pixels_correct"]
            and not reloaded["warm_claim"]
            and reloaded["group"]["serial"] != before_b["group"]["serial"]
        )
        missing = phase(0, "missing")
        report["checks"]["missing_zero_alpha"] = missing["pixels_correct"]
        report["checks"]["sibling_stays_current"] = (
            valid_pixels(1, "A") and group_snapshot(1, "A") == sibling["group"]
        )
        report["checks"]["budget_respected"] = (
            budget.statistics()["peak_bytes"] <= budget.byte_limit
            and budget.statistics()["evictions"] > 0
        )
        report["checks"]["non_atlased_fields"] = all(
            not p["group"]["atlas"] for p in report["phases"] if p["group"] is not None
        )
        report["host"] = {
            "platform": app.platformName(),
            "graphics_api": widgets[0]
            .quickWindow()
            .rendererInterface()
            .graphicsApi()
            .name,
            "qt_version": qVersion(),
            "device_pixel_ratio": widgets[0].devicePixelRatioF(),
            "global_atlas_size_limit": os.environ.get("QSG_ATLAS_SIZE_LIMIT"),
        }
    finally:
        for context in contexts:
            context.close()
        for quick in widgets:
            quick.close()
            quick.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        report["contexts_after_close"] = [context.snapshot() for context in contexts]
        report["final_budget"] = budget.statistics()
        report["checks"]["resources_released"] = all(
            s.get("created", 0) > 0
            and s.get("created") == s.get("destroyed")
            and not s.get("groups")
            and not s.get("errors")
            for s in report["contexts_after_close"]
        )
        report["checks"]["budget_empty"] = (
            budget.statistics()["current_bytes"] == 0
            and budget.statistics()["contexts"] == 0
        )
        for context in contexts:
            context.deleteLater()
        host.close()
    return report


def classify_trace(events, report):
    uploads = [e for e in events if e["event"] == "uploadTexture"]
    counts = []
    for phase in report["phases"]:
        handles = set(phase["group"]["handles"]) if phase["group"] else set()
        counts.append(
            sum(
                e["texture"] in handles
                and phase["began"] <= e["time"] <= phase["ended"]
                for e in uploads
            )
        )
    return {
        "counts": counts,
        "warm_positive": len(counts) >= 4 and counts[0] == counts[1] == counts[2] == 4,
        "warm_return_zero": len(counts) >= 4 and counts[3] == 0,
        "reload_positive": len(counts) >= 6 and counts[5] == 4,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("GWR_NATIVE_STORE_CHILD"):
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
                GWR_NATIVE_STORE_CHILD="1",
                GWR_UPLOAD_TRACE_OUTPUT=str(trace_path),
            ),
            timeout=100,
            check=False,
        )
        if result.returncode or not args.output.is_file():
            raise RuntimeError("Native store gate did not produce a report")
        report = json.loads(args.output.read_text())
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        report["upload_trace"] = classify_trace(events, report)
        status = next((e for e in events if e["event"] == "process_status"), {})
        report["native_exit"] = status
        report["checks"].update(
            clean_native_exit=status.get("exited") is True
            and status.get("exit_code") == 0,
            warm_upload_positive=report["upload_trace"]["warm_positive"],
            warm_return_zero_upload=report["upload_trace"]["warm_return_zero"],
            reload_upload_positive=report["upload_trace"]["reload_positive"],
        )
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        return
    with tempfile.TemporaryDirectory(prefix="gwr-native-store-") as tmp:
        report = run(Path(tmp))
    report["presentation_latency_measured"] = False
    report["physical_vram_measured"] = False
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
