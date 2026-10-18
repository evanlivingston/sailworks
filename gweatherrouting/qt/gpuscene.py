"""Qt Quick scene graph for the opt-in map presenter. Imported lazily."""

import ctypes
from pathlib import Path

import numpy as np
import shiboken6
from PySide6.QtCore import QRectF, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuick import (
    QQuickItem,
    QSGFlatColorMaterial,
    QSGGeometry,
    QSGGeometryNode,
    QSGNode,
    QSGSimpleTextureNode,
    QSGTexture,
)

from gweatherrouting.rendering.windgeometry import unit_scale_texture
from gweatherrouting.rendering.windpainter import barb_bucket_colors

_POINT_ATTRIBUTES = QSGGeometry.defaultAttributes_Point2D()


class ImageItem(QQuickItem):
    failed = Signal(str)

    def __init__(self, parent, widget, counters):
        super().__init__(parent)
        self.setFlag(QQuickItem.Flag.ItemHasContents, True)
        self.widget, self.counters = widget, counters
        self.layer = None
        self.changed = True
        self.dpr = 1.0
        self.resources = None

    def present(self, layer, dpr):
        self.changed = (
            self.changed
            or self.layer is None
            or self.layer.image.cacheKey() != layer.image.cacheKey()
        )
        moved = (
            self.layer is None or self.layer.target != layer.target or self.dpr != dpr
        )
        self.layer, self.dpr = layer, dpr
        if self.changed or moved:
            self.update()

    def updatePaintNode(self, old, _):  # noqa: N802
        if self.layer is None:
            return None
        try:
            node = old or QSGSimpleTextureNode()
            if old is None or self.changed:
                previous = node.texture()
                texture = self.widget.quickWindow().createTextureFromImage(
                    self.layer.image
                )
                if texture is None:
                    raise RuntimeError("Unable to allocate map texture")
                node.setTexture(texture)
                node.setOwnsTexture(True)
                if previous is not None and shiboken6.isValid(previous):
                    shiboken6.delete(previous)
                self.resources = (node, texture)
                self.changed = False
                self.counters["texture_uploads"] += 1
                self.counters["texture_upload_bytes"] += self.layer.image.sizeInBytes()
            target = QRectF(*self.layer.target)
            node.setRect(target)
            image = self.layer.image
            node.setFiltering(
                QSGTexture.Filtering.Nearest
                if not self.layer.smooth
                or unit_scale_texture(
                    image.width(),
                    image.height(),
                    target.width(),
                    target.height(),
                    self.dpr,
                )
                else QSGTexture.Filtering.Linear
            )
            return node
        except Exception as exc:
            self.failed.emit(str(exc))
            return None


class WindItem(QQuickItem):
    failed = Signal(str)

    def __init__(self, parent, bucket, counters):
        super().__init__(parent)
        self.setFlag(QQuickItem.Flag.ItemHasContents, True)
        self.bucket, self.counters = bucket, counters
        self.vertices = np.empty((0, 2), np.float32)
        self.resources = None

    def updatePaintNode(self, old, _):  # noqa: N802
        if not len(self.vertices):
            return None
        try:
            node = old or QSGGeometryNode()
            if old is None:
                attributes = _POINT_ATTRIBUTES
                geometry = QSGGeometry(attributes, len(self.vertices))
                geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
                geometry.setVertexDataPattern(QSGGeometry.DataPattern.DynamicPattern)
                material = QSGFlatColorMaterial()
                material.setColor(QColor.fromRgbF(*barb_bucket_colors()[self.bucket]))
                node.setGeometry(geometry)
                node.setMaterial(material)
                node.setFlag(QSGNode.Flag.OwnsGeometry)
                node.setFlag(QSGNode.Flag.OwnsMaterial)
                self.resources = (node, attributes, geometry, material)
            geometry = node.geometry()
            geometry.allocate(len(self.vertices))
            ctypes.memmove(
                int(geometry.vertexData()),
                self.vertices.ctypes.data,
                self.vertices.nbytes,
            )
            node.markDirty(QSGNode.DirtyStateBit.DirtyGeometry)
            self.counters["vertex_upload_bytes"] += self.vertices.nbytes
            return node
        except Exception as exc:
            self.failed.emit(str(exc))
            return None


class MapScene:
    def __init__(self, widget, fail, request=None):
        self.widget = widget
        self.fail = fail
        self.request = request or widget.update
        self.layers = {}
        self.frame = None
        self.counters = dict(
            texture_uploads=0, texture_upload_bytes=0, vertex_upload_bytes=0
        )
        root_component = QQmlComponent(widget.engine())
        root_component.setData(b"import QtQuick\nItem { clip: true }", QUrl())
        self.root = root_component.create()
        if self.root is None:
            raise RuntimeError(str(root_component.errors()))
        self.root_component = root_component
        widget.setContent(QUrl(), root_component, self.root)
        self.component = QQmlComponent(widget.engine())
        self.component.setData(
            b"import QtQuick\nItem { layer.enabled: true; layer.samples: 4 }", QUrl()
        )
        self.base = ImageItem(self.root, widget, self.counters)
        self.navigation = ImageItem(self.root, widget, self.counters)
        self.raster_wind = ImageItem(self.root, widget, self.counters)
        self.raster_wind.setZ(1)
        self.navigation.setZ(10000)
        for item in (self.base, self.navigation, self.raster_wind):
            item.failed.connect(fail)
        self.weather = None
        self.weather_atlas = None
        self.weather_provider = None
        self.weather_component = None

    def present_weather(self, numerical, frame):
        if numerical is None:
            if self.weather is not None:
                self.weather.setVisible(False)
                for name in (
                    "u0Source",
                    "v0Source",
                    "u1Source",
                    "v1Source",
                    "anchorXSource",
                    "anchorYSource",
                ):
                    self.weather.setProperty(name, QUrl())
                self.weather_atlas = None
                self.weather_provider.clear()
            return
        atlas, blend = numerical
        if self.weather is None:
            from .gpuweather import WeatherImageProvider

            self.weather_provider = WeatherImageProvider()
            self.widget.engine().addImageProvider("weather", self.weather_provider)
            url = QUrl.fromLocalFile(
                str(Path(__file__).parent / "shaders/WeatherBarbs.qml")
            )
            self.weather_component = QQmlComponent(self.widget.engine(), url)
            self.weather = self.weather_component.create()
            if self.weather is None:
                raise RuntimeError(str(self.weather_component.errors()))
            self.weather.setParent(self.root)
            self.weather.setParentItem(self.root)
            self.weather.setZ(100)
            self.weather.shaderErrorChanged.connect(
                lambda: (
                    self.fail(self.weather.property("shaderError"))
                    if self.weather.property("shaderError")
                    else None
                )
            )
            self.weather.readyChanged.connect(self.request)
        if atlas is not self.weather_atlas:
            urls = self.weather_provider.publish(atlas)
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
                self.weather.setProperty(name, QUrl(url))
            rows, cols = atlas.pair.shape
            self.weather.setProperty("columns", cols)
            self.weather.setProperty("rows", rows)
            self.weather_atlas = atlas
            self.counters["texture_uploads"] += 6
            self.counters["texture_upload_bytes"] += atlas.nbytes
        self.weather.setWidth(frame.width)
        self.weather.setHeight(frame.height)
        self.weather.setOpacity(frame.opacity)
        self.weather.setProperty("blend", blend)
        self.weather.setVisible(True)

    def present(self, frame):
        if self.frame is not None and frame.sequence <= self.frame.sequence:
            return
        self.root.setWidth(frame.width)
        self.root.setHeight(frame.height)
        self.widget.setClearColor(QColor.fromRgbF(*frame.sea_color))
        self.base.present(frame.base, frame.dpr)
        self.navigation.present(frame.navigation, frame.dpr)
        self.present_weather(frame.numerical_wind, frame)
        if frame.raster_wind is not None:
            self.raster_wind.setVisible(True)
            self.raster_wind.present(frame.raster_wind, frame.dpr)
        else:
            self.raster_wind.setVisible(False)
            self.raster_wind.layer = None
            self.raster_wind.update()
        previous = {batch.key: batch for batch in self.frame.wind} if self.frame else {}
        active = set()
        for batch in frame.wind:
            key = batch.key
            active.add(key)
            if key not in self.layers:
                layer = self.component.create()
                if layer is None:
                    raise RuntimeError(str(self.component.errors()))
                layer.setParent(self.root)
                layer.setParentItem(self.root)
                layer.setZ(1 + batch.bucket * 2 + batch.kind)
                item = WindItem(layer, batch.bucket, self.counters)
                item.failed.connect(self.fail)
                self.layers[key] = layer, item
            layer, item = self.layers[key]
            layer.setWidth(frame.width)
            layer.setHeight(frame.height)
            layer.setOpacity(frame.opacity)
            if key not in previous or batch.vertices is not previous[key].vertices:
                item.vertices = batch.vertices
                item.update()
        for key in set(self.layers) - active:
            layer, _item = self.layers.pop(key)
            layer.setVisible(False)
            layer.setParentItem(None)
            layer.deleteLater()
        self.frame = frame

    def clear(self):
        self.frame = None
        if self.weather is not None:
            self.weather.setVisible(False)
            for name in (
                "u0Source",
                "v0Source",
                "u1Source",
                "v1Source",
                "anchorXSource",
                "anchorYSource",
            ):
                self.weather.setProperty(name, QUrl())
            self.weather_atlas = None
            self.weather_provider.clear()
        for item in (self.base, self.navigation, self.raster_wind):
            item.layer = None
            item.changed = True
            item.update()
        for layer, _item in self.layers.values():
            layer.setVisible(False)
            layer.setParentItem(None)
            layer.deleteLater()
        self.layers.clear()
