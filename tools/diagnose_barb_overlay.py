"""A0k isolated transparent barb readbacks and independent CPU geometry oracle."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import experiment_app_persistent_weather as shaders
from persistent_coverage import atomic_json


def rgba(image):
    image = image.convertToFormat(image.Format.Format_RGBA8888)
    return (
        np.frombuffer(image.constBits(), np.uint8)
        .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
        .reshape(image.height(), image.width(), 4)
        .copy()
    )


def dilate(mask, radius=3):
    padded = np.pad(mask, radius)
    result = np.zeros_like(mask)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            result |= padded[dy : dy + mask.shape[0], dx : dx + mask.shape[1]]
    return result


def support_error(gpu, cpu):
    # Three physical pixels at DPR 2 = 1.5 logical pixels of rasterization
    # allowance. No arbitrary global fraction can excuse off-geometry flooding.
    a, b = gpu[..., 3] > 25, cpu[..., 3] > 25
    return dict(
        gpu_ink_pixels=int(a.sum()),
        cpu_ink_pixels=int(b.sum()),
        gpu_outside_cpu_support=int((a & ~dilate(b)).sum()),
        cpu_outside_gpu_support=int((b & ~dilate(a)).sum()),
    )


def color_error(gpu, cpu, radius=3):
    """Compare opaque glyph colors to nearby CPU ink, allowing AA edge placement."""
    yy, xx = np.nonzero(gpu[..., 3] > 127)
    matched = np.zeros(len(yy), dtype=bool)
    colors = gpu[yy, xx, :3].astype(np.int16)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            y = np.clip(yy + dy, 0, cpu.shape[0] - 1)
            x = np.clip(xx + dx, 0, cpu.shape[1] - 1)
            matched |= (cpu[y, x, 3] > 64) & (
                np.abs(colors - cpu[y, x, :3].astype(np.int16)).max(axis=1) <= 8
            )
    return dict(opaque_gpu_pixels=len(yy), color_mismatch_pixels=int((~matched).sum()))


def cpu_image(field, selection, blend, width, height, dpr, opacity):
    import cairo
    from PySide6.QtGui import QImage
    from gweatherrouting.rendering.windpainter import WindPainter

    picks = selection.reshape(-1, 3)
    picks = picks[picks[:, 0] >= 0]
    values = field.reshape(-1, 4)[picks[:, 0].astype(int)]
    uv = (
        values[:, :2]
        if blend <= 0
        else (
            values[:, 2:]
            if blend >= 1
            else values[:, :2] + (values[:, 2:] - values[:, :2]) * blend
        )
    )
    valid = np.isfinite(uv).all(axis=1) & np.isfinite(picks[:, 1:]).all(axis=1)
    picks, uv = picks[valid], uv[valid]
    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32, round(width * dpr), round(height * dpr)
    )
    cr = cairo.Context(surface)
    cr.scale(dpr, dpr)
    dirs = np.degrees(np.arctan2(-uv[:, 0], -uv[:, 1]))
    geometry = WindPainter.build_barb_geometry(
        picks[:, 1], picks[:, 2], dirs, np.hypot(uv[:, 0], uv[:, 1])
    )
    WindPainter.paint_barb_geometry(cr, geometry, opacity)
    surface.flush()
    return QImage(
        surface.get_data(),
        surface.get_width(),
        surface.get_height(),
        surface.get_stride(),
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()


def controls():
    field = np.zeros((1, 64, 4), dtype="<f4")
    field[0, :4] = [[1, 0, 1, 0], [0, 5, 0, 5], [-10, 0, -10, 0], [0, -30, 0, -30]]
    selection = np.array(
        [[[0, 48, 48], [1, 128, 48]], [[2, 48, 112], [3, 128, 112]]], dtype="<f4"
    )
    result = [("finite", field, selection, 0.0, 192, 160)]
    missing = selection.copy()
    missing[..., 0] = -1
    result.append(("all-unselected", field, missing, 0.0, 192, 160))
    mixed = selection.copy()
    mixed[0, 0, 0] = mixed[1, 1, 0] = -1
    result.append(("mixed-unselected", field, mixed, 0.0, 192, 160))
    empty_field = np.full_like(field, np.nan)
    result.append(("all-nan-endpoints", empty_field, selection, 0.0, 192, 160))
    endpoint = field.copy()
    endpoint[..., 2:] = np.nan
    result.append(("valid-first-missing-second", endpoint, selection, 0.0, 192, 160))
    result.append(("missing-second-interior", endpoint, selection, 0.5, 192, 160))
    return result


def render(app, case, directory, output):
    from PySide6.QtCore import QUrl, Qt
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtQml import QQmlComponent
    from PySide6.QtQuickWidgets import QQuickWidget
    from gweatherrouting.qt.gpuweather import WeatherImageProvider, encode_component

    name, field, selection, blend, width, height = case
    quick = QQuickWidget()
    fmt = QSurfaceFormat()
    fmt.setSamples(4)
    fmt.setAlphaBufferSize(8)
    quick.setFormat(fmt)
    quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    quick.resize(width, height)
    quick.setClearColor(Qt.GlobalColor.transparent)
    providers = [WeatherImageProvider(), WeatherImageProvider()]
    for label, provider in zip(("diagfield", "diagselection"), providers):
        quick.engine().addImageProvider(label, provider)
    component = QQmlComponent(
        quick.engine(), QUrl.fromLocalFile(str(directory / "WeatherBarbs.qml"))
    )
    root = component.create()
    if root is None:
        raise ValueError(str(component.errors()))
    quick.setContent(QUrl(), component, root)
    root.setWidth(width)
    root.setHeight(height)
    root.setOpacity(0.85)
    for values, names, provider, label in (
        (
            field,
            ("u0Source", "v0Source", "u1Source", "v1Source"),
            providers[0],
            "diagfield",
        ),
        (
            selection,
            ("nodeIdSource", "anchorXSource", "anchorYSource"),
            providers[1],
            "diagselection",
        ),
    ):
        images = tuple(encode_component(values[..., i]) for i in range(len(names)))
        pair = SimpleNamespace(
            images=images, nbytes=sum(i.sizeInBytes() for i in images)
        )
        for prop, url in zip(names, provider.publish(pair)):
            root.setProperty(
                prop, QUrl(url.replace("image://weather/", f"image://{label}/"))
            )
    root.setProperty("rows", selection.shape[0])
    root.setProperty("columns", selection.shape[1])
    root.setProperty("blend", blend)
    quick.show()
    deadline = time.perf_counter() + 10
    # ShaderEffect's Compiled status is not reliably re-emitted when the engine
    # reuses a compiled pipeline in a later window. Require images, then force a
    # native readback; the positive controls and CPU support gates validate ink.
    while not root.property("imagesReady"):
        app.processEvents()
        if time.perf_counter() > deadline:
            quick.grabFramebuffer().save(str(output / f"{name}-timeout.png"))
            raise TimeoutError(
                f"{name}: imagesReady={root.property('imagesReady')}, shaderError={root.property('shaderError')}, status={quick.status()}, size={quick.size()}"
            )
        time.sleep(0.005)
    for _ in range(20):
        quick.update()
        app.processEvents()
        time.sleep(0.01)
    image = quick.grabFramebuffer()
    if root.property("shaderError"):
        raise ValueError(str(root.property("shaderError")))
    if image.isNull() or not image.save(str(output / f"{name}-gpu.png")):
        raise ValueError("Diagnostic readback failed")
    dpr = quick.devicePixelRatioF()
    cpu = cpu_image(field, selection, blend, width, height, dpr, 0.85)
    cpu.save(str(output / f"{name}-cpu.png"))
    pixels, expected = rgba(image), rgba(cpu)
    stats = support_error(pixels, expected)
    stats["any_alpha_pixels"] = int((pixels[..., 3] > 0).sum())
    stats.update(
        name=name,
        dpr=dpr,
        backend=quick.quickWindow().rendererInterface().graphicsApi().name,
        ready_property=bool(root.property("ready")),
    )
    quick.close()
    quick.deleteLater()
    app.processEvents()
    return stats, pixels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--field-directory", type=Path)
    parser.add_argument("--compare-safe", action="store_true")
    args = parser.parse_args()
    args.app_root = args.app_root.resolve()
    sys.path.insert(0, str(args.app_root))
    args.directory.mkdir(parents=True, exist_ok=False)
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    directory = args.directory / "original"
    directory.mkdir()
    shaders.shader_sources(args.app_root, directory)
    cases = controls()
    if args.field_directory:
        meta = json.loads((args.field_directory / "field.json").read_text())
        with np.load(args.field_directory / "field.npz", allow_pickle=False) as payload:
            weight = (
                0
                if meta["before"] == meta["after"]
                else (
                    datetime.fromisoformat(meta["clock"]).timestamp() - meta["before"]
                )
                / (meta["after"] - meta["before"])
            )
            cases.append(
                (
                    "current-real",
                    payload["field"].copy(),
                    payload["selection"][..., :3].copy(),
                    weight,
                    meta["view"]["width"],
                    meta["view"]["height"],
                )
            )
    rows, originals, failures = [], {}, []
    for case in cases:
        row, pixels = render(app, case, directory, args.directory)
        originals[case[0]] = pixels
        rows.append(row)
        print(json.dumps(row), flush=True)
    if args.compare_safe:
        safe_directory = args.directory / "safe"
        safe_directory.mkdir()
        shaders.shader_sources(args.app_root, safe_directory, safe_missing=True)
        for case in cases:
            row, pixels = render(app, case, safe_directory, safe_directory)
            row["variant"] = "safe"
            if (
                case[0]
                in ("all-unselected", "all-nan-endpoints", "missing-second-interior")
                and row["any_alpha_pixels"]
            ):
                failures.append(case[0] + ": missing layer not transparent")
            if row["gpu_outside_cpu_support"] or row["cpu_outside_gpu_support"]:
                failures.append(case[0] + ": outside CPU raster support tolerance")
            if case[0] == "finite":
                row["original_finite_pixels_exact"] = bool(
                    np.array_equal(pixels, originals["finite"])
                )
                if not row["original_finite_pixels_exact"]:
                    failures.append("finite control pixels changed")
            rows.append(row)
            print(json.dumps(row), flush=True)
    atomic_json(
        args.directory / "result.json",
        dict(protocol="barb-overlay-diagnostic-a0k-v1", cases=rows, failures=failures),
    )
    print(json.dumps(rows, indent=2))
    if failures:
        raise ValueError(str(failures))


if __name__ == "__main__":
    main()
