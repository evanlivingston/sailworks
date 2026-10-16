"""Experimental retained scene and pixel diagnostics; never imported by the app."""

import ctypes
import math
import time
from types import SimpleNamespace

import numpy as np


def stroke_mesh(lines, dots, circle_segments=64):
    """One-logical-pixel flat-cap strokes; calm rings have bounded chord error."""
    chunks = []
    if lines:
        segments = np.asarray(lines, dtype=np.float64).reshape(-1, 2, 2)
        direction = segments[:, 1] - segments[:, 0]
        length = np.linalg.norm(direction, axis=1)
        segments, direction, length = (
            segments[length > 0],
            direction[length > 0],
            length[length > 0],
        )
        normal = np.column_stack((-direction[:, 1], direction[:, 0])) * (
            0.5 / length[:, None]
        )
        a, b = segments[:, 0] + normal, segments[:, 0] - normal
        c, d = segments[:, 1] + normal, segments[:, 1] - normal
        chunks.append(np.stack((a, b, c, c, b, d), axis=1).reshape(-1, 2))
    if dots:
        angles = np.arange(circle_segments + 1) * (2 * np.pi / circle_segments)
        directions = np.column_stack((np.cos(angles), np.sin(angles)))
        centers = np.asarray(dots)[:, None, :]
        outer, inner = centers + 2.5 * directions, centers + 1.5 * directions
        a, b, c, d = outer[:, :-1], inner[:, :-1], outer[:, 1:], inner[:, 1:]
        chunks.append(np.stack((a, b, c, c, b, d), axis=2).reshape(-1, 2))
    return np.ascontiguousarray(
        np.concatenate(chunks) if chunks else np.empty((0, 2)), dtype=np.float32
    )


def geometry_digest(geometry):
    import hashlib

    digest = hashlib.sha256()
    for bucket, groups in sorted(geometry.items()):
        digest.update(str(bucket).encode())
        for group in groups:
            data = np.asarray(group, dtype=np.float64)
            digest.update(str(data.shape).encode())
            digest.update(data.tobytes())
    return digest.hexdigest()


def unit_scale_texture(image_width, image_height, target_width, target_height, dpr):
    """Whether one source texel maps to one device pixel, not one logical pixel."""
    return math.isclose(
        target_width * dpr, image_width, rel_tol=1e-12
    ) and math.isclose(target_height * dpr, image_height, rel_tol=1e-12)


def dilate(mask):
    padded = np.pad(mask, 1)
    h, w = mask.shape
    return np.logical_or.reduce(
        [padded[y : y + h, x : x + w] for y in range(3) for x in range(3)]
    )


def pixel_metrics(reference, candidate):
    """Premultiplied RGBA arrays, alpha-support diagnostics, no background dilution."""
    if reference.shape != candidate.shape:
        raise ValueError("Image sizes differ")
    a, b = reference[..., 3] >= 32, candidate[..., 3] >= 32
    support = (reference[..., 3] > 8) | (candidate[..., 3] > 8)
    error = np.abs(reference.astype(np.int16) - candidate.astype(np.int16))
    ref_coverage = float(reference[..., 3].sum())
    precision = float((b & dilate(a)).sum() / max(1, b.sum()))
    recall = float((a & dilate(b)).sum() / max(1, a.sum()))
    ratio = float(candidate[..., 3].sum() / ref_coverage) if ref_coverage else 1.0
    mae = float(error[support].mean()) if support.any() else 0.0
    return {
        "reference_support_pixels": int(a.sum()),
        "candidate_support_pixels": int(b.sum()),
        "silhouette_precision_within_1px": precision,
        "silhouette_recall_within_1px": recall,
        "alpha_coverage_ratio": ratio,
        "foreground_premultiplied_rgba_mae": mae,
        # Predeclared experimental screen-space gate, NOT pixel identity.
        "within_experimental_tolerance": precision >= 0.99
        and recall >= 0.99
        and 0.95 <= ratio <= 1.05
        and mae <= 12,
    }


def image_array(image):
    from PySide6.QtGui import QImage

    image = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    return (
        np.frombuffer(image.constBits(), dtype=np.uint8)
        .reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4]
        .reshape(image.height(), image.width(), 4)
        .copy()
    )


class Camera:
    def __init__(self, metadata):
        from gweatherrouting.core.mapviewport import MercatorProjection

        class Projection(MercatorProjection):
            def get_allocated_width(self):
                return metadata["width"]

            def get_allocated_height(self):
                return metadata["height"]

        self.projection = Projection()
        self.metadata = metadata
        self.set_frame("stationary", 0, 1)

    def set_frame(self, scenario, index, frames):
        m, p = self.metadata, self.projection
        p._center_lat, p._center_lon, p._zoom = m["latitude"], m["longitude"], m["zoom"]
        phase = 2 * math.pi * index / max(1, frames - 1)
        if scenario == "pan":
            center = p.convert_screen_to_geographic(
                m["width"] / 2 + 96 * math.sin(phase),
                m["height"] / 2 + 24 * math.sin(phase),
            )
            p._center_lat, p._center_lon = center.get_degrees()
        elif scenario == "zoom":
            p._zoom += 0.2 * math.sin(phase)
        elif scenario != "stationary":
            raise ValueError("Unknown camera trajectory")

    def texture_rect(self, image, layer):
        from PySide6.QtCore import QRectF

        p, m = self.projection, self.metadata
        ratio = 2 ** (p._zoom - layer["zoom"])
        dx = p._lon_to_x(layer["longitude"]) - p._lon_to_x(p._center_lon)
        dy = p._lat_to_y(layer["latitude"]) - p._lat_to_y(p._center_lat)
        width, height = (
            image.width() / m["dpr"] * ratio,
            image.height() / m["dpr"] * ratio,
        )
        target = QRectF(
            m["width"] / 2 + dx - width / 2,
            m["height"] / 2 + dy - height / 2,
            width,
            height,
        )
        if not target.contains(QRectF(0, 0, m["width"], m["height"])):
            raise ValueError(
                "Replay exhausted retained image coverage; refusing blank-content speedup"
            )
        return target


class Payloads:
    def __init__(self, metadata, flat, base, navigation):
        from gweatherrouting.rendering.windpainter import barb_density_parameters

        self.metadata, self.flat = metadata, flat
        self.base, self.navigation = base, navigation
        self.camera = Camera(metadata)
        self.step, self.max_count = barb_density_parameters(metadata["density"])
        self.cached_view, self.cached_geometry, self.cached_count = None, None, 0

    def frame(self, scenario, index, frames):
        from gweatherrouting.rendering.windpainter import WindPainter

        self.camera.set_frame(scenario, index, frames)
        p, m = self.camera.projection, self.metadata
        view = (p._center_lat, p._center_lon, p._zoom, m["width"], m["height"])
        if view != self.cached_view:
            self.cached_geometry, self.cached_count = (
                WindPainter.live_geometry_for_view(
                    self.flat, *view, step=self.step, max_count=self.max_count
                )
            )
            self.cached_view = view
        return {
            "geometry": self.cached_geometry,
            "selected_samples": self.cached_count,
            "base_rect": self.camera.texture_rect(self.base, m["base"]),
            "navigation_rect": self.camera.texture_rect(
                self.navigation, m["navigation"]
            ),
        }


def paint_wind(painter, geometry, metadata, mode):
    from PySide6.QtCore import QLineF, QPointF, Qt
    from PySide6.QtGui import QColor, QPainterPath, QPen
    from gweatherrouting.rendering.windpainter import barb_bucket_colors

    if mode == "cpu_current":
        # Invoke the actual production painting method, not a copied approximation.
        from gweatherrouting.qt.mapcanvas import TrackMap

        proxy = SimpleNamespace(
            wind_visible=True,
            wind=SimpleNamespace(
                live_geometry_for_view=lambda *_args, **_kwargs: (
                    geometry,
                    metadata["opacity"],
                )
            ),
            width=lambda: metadata["width"],
            height=lambda: metadata["height"],
            _center_lat=metadata["latitude"],
            _center_lon=metadata["longitude"],
            _zoom=metadata["zoom"],
            controller=SimpleNamespace(
                settings=SimpleNamespace(gribBarbDensity=metadata["density"])
            ),
        )
        TrackMap._paint_wind_barbs(proxy, painter)
        return
    colors = barb_bucket_colors()
    for bucket, (lines, triangles, dots) in sorted(geometry.items()):
        color = QColor.fromRgbF(*colors[bucket], metadata["opacity"])
        pen = QPen(color, 1)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if lines:
            painter.drawLines([QLineF(*line) for line in lines])
        if dots:
            path = QPainterPath()
            for x, y in dots:
                path.addEllipse(QPointF(x, y), 2, 2)
            painter.drawPath(path)
        if triangles:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.WindingFill)
            for ax, ay, bx, by, cx, cy in triangles:
                path.moveTo(ax, ay)
                path.lineTo(bx, by)
                path.lineTo(cx, cy)
                path.closeSubpath()
            painter.drawPath(path)


def cpu_image(metadata, base, navigation, payload, mode, overlay_only=False):
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter

    dpr = metadata["dpr"]
    image = QImage(
        round(metadata["width"] * dpr),
        round(metadata["height"] * dpr),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.setDevicePixelRatio(dpr)
    image.fill(
        Qt.GlobalColor.transparent
        if overlay_only
        else QColor.fromRgbF(*metadata["sea_color"])
    )
    painter = QPainter(image)
    try:
        if not overlay_only:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(
                payload["base_rect"], base, QRectF(0, 0, base.width(), base.height())
            )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_wind(painter, payload["geometry"], metadata, mode)
        if not overlay_only:
            painter.drawImage(
                payload["navigation_rect"],
                navigation,
                QRectF(0, 0, navigation.width(), navigation.height()),
            )
    finally:
        painter.end()
    return image


def make_scene(view, metadata, base, navigation, mode):
    """Python QSG nodes with retained lifetimes; actual RHI geometry, no painted item."""
    from PySide6.QtCore import QRectF, QUrl
    from PySide6.QtGui import QColor
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from PySide6.QtQuick import (
        QQuickItem,
        QSGFlatColorMaterial,
        QSGGeometry,
        QSGGeometryNode,
        QSGNode,
        QSGSimpleTextureNode,
        QSGTexture,
    )
    import shiboken6
    from gweatherrouting.rendering.windpainter import barb_bucket_colors

    counters = {
        "texture_uploads": 0,
        "texture_upload_bytes": 0,
        "vertex_upload_bytes": 0,
        "node_update_ms": 0.0,
    }

    class Texture(QQuickItem):
        def __init__(self, parent, image):
            super().__init__(parent)
            self.setFlag(QQuickItem.Flag.ItemHasContents, True)
            self.image, self.target, self.changed = image, QRectF(), True
            self.resources = None

        def set_image(self, image):
            self.image, self.changed = image, True
            self.update()

        def set_target(self, target):
            if target != self.target:
                self.target = target
                self.update()

        def updatePaintNode(self, old, _):
            started = time.perf_counter()
            node = old or QSGSimpleTextureNode()
            if self.changed:
                previous = node.texture()
                texture = view.createTextureFromImage(self.image)
                node.setTexture(texture)
                node.setOwnsTexture(True)
                if previous is not None and shiboken6.isValid(previous):
                    shiboken6.delete(previous)
                self.resources = (node, texture)
                self.changed = False
                counters["texture_uploads"] += 1
                counters["texture_upload_bytes"] += (
                    self.image.width() * self.image.height() * 4
                )
            node.setRect(self.target)
            # Raster QPainter blits 1:1 images without subpixel interpolation,
            # even with SmoothPixmapTransform. Linear sampling here softened
            # translated chart pixels and failed the fractional-pan regression.
            # Keep linear filtering for actual scaling (including zoom).
            filtering = (
                QSGTexture.Filtering.Nearest
                if unit_scale_texture(
                    self.image.width(),
                    self.image.height(),
                    self.target.width(),
                    self.target.height(),
                    metadata["dpr"],
                )
                else QSGTexture.Filtering.Linear
            )
            node.setFiltering(filtering)
            counters["node_update_ms"] += (time.perf_counter() - started) * 1000
            return node

    class Mesh(QQuickItem):
        def __init__(self, parent, color):
            super().__init__(parent)
            self.setFlag(QQuickItem.Flag.ItemHasContents, True)
            self.vertices = np.empty((0, 2), np.float32)
            self.color, self.resources = color, None

        def updatePaintNode(self, old, _):
            started = time.perf_counter()
            node = old or QSGGeometryNode()
            if old is None:
                attributes = QSGGeometry.defaultAttributes_Point2D()
                geometry = QSGGeometry(attributes, len(self.vertices))
                geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
                geometry.setVertexDataPattern(QSGGeometry.DataPattern.DynamicPattern)
                node.setGeometry(geometry)
                node.setFlag(QSGNode.Flag.OwnsGeometry)
                material = QSGFlatColorMaterial()
                material.setColor(self.color)
                node.setMaterial(material)
                node.setFlag(QSGNode.Flag.OwnsMaterial)
                # The attribute set AND geometry/material wrappers must outlive
                # the C++ node; otherwise PySide can leave dangling attributes.
                self.resources = (node, attributes, geometry, material)
            geometry = node.geometry()
            geometry.allocate(len(self.vertices))
            if self.vertices.nbytes:
                ctypes.memmove(
                    int(geometry.vertexData()),
                    self.vertices.ctypes.data,
                    self.vertices.nbytes,
                )
            node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)
            counters["vertex_upload_bytes"] += self.vertices.nbytes
            counters["node_update_ms"] += (time.perf_counter() - started) * 1000
            return node

    class Scene:
        def __init__(self):
            self.counters = counters
            self.layers, self.last_geometry = {}, None
            self.engine = QQmlEngine() if mode == "gpu_mesh" else None
            if self.engine:
                self.component = QQmlComponent(self.engine)
                # Per-bucket stroke/fill layers preserve the reference painter's
                # union-before-opacity behavior at overlapping stroke segments.
                self.component.setData(
                    b"import QtQuick\n Item { layer.enabled: true; layer.samples: 4 }",
                    QUrl(),
                )
            self.base_item = Texture(view.contentItem(), base)
            self.nav_item = Texture(view.contentItem(), navigation)
            self.nav_item.setZ(10000)
            if mode != "gpu_mesh":
                self.base_item.setVisible(False)
                self.nav_item.setVisible(False)
                self.cpu_item = Texture(view.contentItem(), base)

        def frame(self, payload, overlay_only=False):
            width, height = metadata["width"], metadata["height"]
            if mode != "gpu_mesh":
                image = cpu_image(
                    metadata, base, navigation, payload, mode, overlay_only
                )
                self.cpu_item.set_image(image)
                self.cpu_item.set_target(QRectF(0, 0, width, height))
                return
            self.base_item.setVisible(not overlay_only)
            self.nav_item.setVisible(not overlay_only)
            self.base_item.set_target(payload["base_rect"])
            self.nav_item.set_target(payload["navigation_rect"])
            geometry = payload["geometry"]
            if geometry is self.last_geometry:
                return
            self.last_geometry = geometry
            present = set()
            colors = barb_bucket_colors()
            for bucket, (lines, triangles, dots) in sorted(geometry.items()):
                for kind, vertices in (
                    (0, stroke_mesh(lines, dots)),
                    (
                        1,
                        np.ascontiguousarray(
                            np.asarray(triangles, dtype=np.float32).reshape(-1, 2)
                        ),
                    ),
                ):
                    key = bucket, kind
                    present.add(key)
                    if key not in self.layers:
                        layer = self.component.create()
                        if layer is None:
                            raise RuntimeError(str(self.component.errors()))
                        layer.setParentItem(view.contentItem())
                        layer.setWidth(width)
                        layer.setHeight(height)
                        layer.setOpacity(metadata["opacity"])
                        layer.setZ(1 + bucket * 2 + kind)
                        mesh = Mesh(layer, QColor.fromRgbF(*colors[bucket]))
                        mesh.setWidth(width)
                        mesh.setHeight(height)
                        self.layers[key] = (layer, mesh)
                    layer, mesh = self.layers[key]
                    layer.setVisible(bool(len(vertices)))
                    mesh.vertices = vertices
                    mesh.update()
            for key, (layer, _mesh) in self.layers.items():
                if key not in present:
                    layer.setVisible(False)

    return Scene()
