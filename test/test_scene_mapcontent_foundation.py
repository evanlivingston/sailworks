import os
import unittest
from pathlib import Path

import sailworks.qt.gribviewport as gribviewport


def _shaders():
    return Path(gribviewport.__file__).parent / "shaders"


def _block_for(text, object_name):
    marker = 'objectName: "%s"' % object_name
    idx = text.index(marker)
    start = text.rfind("Item {", 0, idx)
    assert start != -1, "no enclosing Item for %s" % object_name
    brace_idx = text.index("{", start)
    depth = 0
    i = brace_idx
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise AssertionError("unterminated block for %s" % object_name)


class TestSceneMapContentFoundation(unittest.TestCase):
    def test_both_scenes_expose_mapclip_and_mapcontent(self):
        shaders = _shaders()
        for name in ("WeatherScene.qml", "ViewportChartScene.qml"):
            scene = (shaders / name).read_text()
            self.assertIn('objectName: "mapClip"', scene)
            self.assertIn('objectName: "mapContent"', scene)

    def test_mapclip_is_stationary_and_contains_mapcontent(self):
        shaders = _shaders()
        for name in ("WeatherScene.qml", "ViewportChartScene.qml"):
            scene = (shaders / name).read_text()
            clip_block = _block_for(scene, "mapClip")
            content_block = _block_for(scene, "mapContent")
            # The weather scene retains its original root anchoring; the
            # chart scene's clip fills its root via parent anchoring.
            if name == "WeatherScene.qml":
                self.assertIn("anchors.fill: root", clip_block)
            else:
                self.assertIn("anchors.fill: parent", clip_block)
            self.assertIn("clip: true", clip_block)
            # mapContent is the direct child of the stationary clip.
            self.assertIn(content_block, clip_block)
            self.assertEqual(clip_block.count('objectName: "mapContent"'), 1)
            # Zero-offset child, sized to the clip.
            self.assertIn("x: 0", content_block)
            self.assertIn("y: 0", content_block)
            if name == "WeatherScene.qml":
                self.assertIn("width: parent.width", content_block)
                self.assertIn("height: parent.height", content_block)
                self.assertEqual(content_block.count("objectName:"), 1)
            else:
                self.assertIn("width: mapClip.width", content_block)
                self.assertIn("height: mapClip.height", content_block)

    def test_existing_layers_keep_their_parents(self):
        """Existing geographic layers are not reparented into mapClip/mapContent."""
        shaders = _shaders()
        scene = (shaders / "ViewportChartScene.qml").read_text()
        content_block = _block_for(scene, "mapContent")
        for name in (
            "baseBand",
            "chartLayer",
            "weatherBand",
            "navigationOverlay",
            "routeOverlay",
        ):
            self.assertIn('objectName: "%s"' % name, content_block)
        for name in ("baseLayer", "environmentLayer", "weatherFill", "weatherBarbs"):
            self.assertIn('objectName: "%s"' % name, content_block)
        ordered_layers = (
            "baseBand",
            "chartLayer",
            "weatherBand",
            "navigationOverlay",
            "routeOverlay",
        )
        positions = [
            content_block.index('objectName: "%s"' % name) for name in ordered_layers
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('objectName: "dashboardOverlay"', content_block)
        self.assertIn('objectName: "dashboardOverlay"', scene)

    def test_weather_scene_existing_layers_keep_parents(self):
        shaders = _shaders()
        scene = (shaders / "WeatherScene.qml").read_text()
        clip_block = _block_for(scene, "mapClip")
        content_block = _block_for(scene, "mapContent")
        for name in (
            "weatherFill",
            "weatherFillBack",
            "weatherBarbs",
            "weatherBarbsBack",
            "weatherStreamlines",
        ):
            self.assertIn('objectName: "%s"' % name, scene)
            self.assertNotIn('objectName: "%s"' % name, clip_block)
            self.assertNotIn('objectName: "%s"' % name, content_block)
        self.assertEqual(content_block.count("objectName:"), 1)


class TestSyntheticMapContentStationarity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QGuiApplication

        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def test_markers_move_together_clip_and_sibling_stationary(self):
        from PySide6.QtCore import QPointF, QUrl
        from PySide6.QtGui import QGuiApplication  # noqa: F401
        from PySide6.QtQml import QQmlComponent, QQmlEngine
        from PySide6.QtQuick import QQuickItem

        qml = """
import QtQuick
Item {
    id: root
    width: 100
    height: 100
    Item {
        objectName: "mapClip"
        anchors.fill: root
        clip: true
        Item {
            objectName: "mapContent"
            x: 0
            y: 0
            width: parent.width
            height: parent.height
            Rectangle { objectName: "markerA"; x: 10; y: 20; width: 5; height: 5; color: "red" }
            Rectangle { objectName: "markerB"; x: 30; y: 40; width: 5; height: 5; color: "blue" }
        }
    }
    Rectangle { objectName: "fixedSibling"; x: 5; y: 5; width: 5; height: 5; color: "green" }
}
"""
        engine = QQmlEngine()
        component = QQmlComponent(engine)
        component.setData(qml.encode("utf-8"), QUrl())
        if component.isError():
            self.fail("; ".join(e.toString() for e in component.errors()))
        root = component.create()
        self.assertIsNotNone(root)

        map_clip = root.findChild(QQuickItem, "mapClip")
        map_content = root.findChild(QQuickItem, "mapContent")
        marker_a = root.findChild(QQuickItem, "markerA")
        marker_b = root.findChild(QQuickItem, "markerB")
        fixed = root.findChild(QQuickItem, "fixedSibling")
        self.assertIsNotNone(map_clip)
        self.assertIsNotNone(map_content)
        self.assertIsNotNone(marker_a)
        self.assertIsNotNone(marker_b)
        self.assertIsNotNone(fixed)

        def scene_pos(item):
            return item.mapToItem(root, QPointF(0, 0))

        clip_before = scene_pos(map_clip)
        fixed_before = scene_pos(fixed)
        a_before = scene_pos(marker_a)
        b_before = scene_pos(marker_b)

        map_content.setX(map_content.x() + 12)
        map_content.setY(map_content.y() + 7)
        self.app.processEvents()

        a_after = scene_pos(marker_a)
        b_after = scene_pos(marker_b)
        clip_after = scene_pos(map_clip)
        fixed_after = scene_pos(fixed)

        self.assertAlmostEqual(a_after.x() - a_before.x(), 12, places=3)
        self.assertAlmostEqual(a_after.y() - a_before.y(), 7, places=3)
        self.assertAlmostEqual(b_after.x() - b_before.x(), 12, places=3)
        self.assertAlmostEqual(b_after.y() - b_before.y(), 7, places=3)
        self.assertAlmostEqual(clip_after.x() - clip_before.x(), 0, places=3)
        self.assertAlmostEqual(clip_after.y() - clip_before.y(), 0, places=3)
        self.assertAlmostEqual(fixed_after.x() - fixed_before.x(), 0, places=3)
        self.assertAlmostEqual(fixed_after.y() - fixed_before.y(), 0, places=3)


class TestRealSceneMapContentStructure(unittest.TestCase):
    def test_real_scenes_exact_clip_content_structure(self):
        shaders = _shaders()
        weather_expected = " ".join("""Item {
                objectName: "mapClip"
                anchors.fill: root
                clip: true
                Item {
                    objectName: "mapContent"
                    x: 0
                    y: 0
                    width: parent.width
                    height: parent.height
                }
            }""".split())
        weather_scene = (shaders / "WeatherScene.qml").read_text()
        self.assertEqual(
            " ".join(_block_for(weather_scene, "mapClip").split()), weather_expected
        )
        chart_scene = (shaders / "ViewportChartScene.qml").read_text()
        clip_block = _block_for(chart_scene, "mapClip")
        content_block = _block_for(chart_scene, "mapContent")
        self.assertIn("anchors.fill: parent", clip_block)
        self.assertIn(content_block, clip_block)
        self.assertIn("width: mapClip.width", content_block)
        self.assertIn("height: mapClip.height", content_block)
        self.assertIn('objectName: "baseBand"', content_block)
        self.assertIn('objectName: "weatherBand"', content_block)


class TestRealSceneMapContentStationarity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QGuiApplication

        cls.app = QGuiApplication.instance() or QGuiApplication([])
        import sailworks.qt.gribviewport as gribviewport

        cls.shaders = Path(gribviewport.__file__).parent / "shaders"

    def _load_scene(self, name):
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlComponent, QQmlEngine

        engine = QQmlEngine()
        engine.addImportPath(str(self.shaders))
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(self.shaders / name)))
        if component.isError():
            self.fail("; ".join(e.toString() for e in component.errors()))
        root = component.create()
        self.assertIsNotNone(root)
        root.setWidth(100)
        root.setHeight(100)
        self.app.processEvents()
        return engine, component, root

    def test_markers_move_together_clip_and_sibling_stationary_in_real_scenes(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtQuick import QQuickItem

        for name in ("WeatherScene.qml", "ViewportChartScene.qml"):
            engine, component, root = self._load_scene(name)
            try:
                map_clip = root.findChild(QQuickItem, "mapClip")
                map_content = root.findChild(QQuickItem, "mapContent")
                self.assertIsNotNone(map_clip)
                self.assertIsNotNone(map_content)

                marker_a = QQuickItem(map_content)
                marker_a.setX(10)
                marker_a.setY(20)
                marker_a.setWidth(5)
                marker_a.setHeight(5)
                marker_b = QQuickItem(map_content)
                marker_b.setX(30)
                marker_b.setY(40)
                marker_b.setWidth(5)
                marker_b.setHeight(5)
                fixed = QQuickItem(root)
                fixed.setX(5)
                fixed.setY(5)
                fixed.setWidth(5)
                fixed.setHeight(5)
                self.app.processEvents()

                def scene_pos(item):
                    return item.mapToItem(root, QPointF(0, 0))

                clip_before = scene_pos(map_clip)
                fixed_before = scene_pos(fixed)
                a_before = scene_pos(marker_a)
                b_before = scene_pos(marker_b)

                map_content.setX(map_content.x() + 12)
                map_content.setY(map_content.y() + 7)
                self.app.processEvents()

                a_after = scene_pos(marker_a)
                b_after = scene_pos(marker_b)
                clip_after = scene_pos(map_clip)
                fixed_after = scene_pos(fixed)

                self.assertAlmostEqual(a_after.x() - a_before.x(), 12, places=3)
                self.assertAlmostEqual(a_after.y() - a_before.y(), 7, places=3)
                self.assertAlmostEqual(b_after.x() - b_before.x(), 12, places=3)
                self.assertAlmostEqual(b_after.y() - b_before.y(), 7, places=3)
                self.assertAlmostEqual(clip_after.x() - clip_before.x(), 0, places=3)
                self.assertAlmostEqual(clip_after.y() - clip_before.y(), 0, places=3)
                self.assertAlmostEqual(fixed_after.x() - fixed_before.x(), 0, places=3)
                self.assertAlmostEqual(fixed_after.y() - fixed_before.y(), 0, places=3)
            finally:
                component.deleteLater()
                engine.deleteLater()

    def test_real_chart_layers_share_and_compensate_map_content_motion(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtQuick import QQuickItem

        engine, component, root = self._load_scene("ViewportChartScene.qml")
        try:
            names = (
                "mapClip",
                "mapContent",
                "baseBand",
                "chartLayer",
                "weatherBand",
                "weatherBarbs",
                "navigationOverlay",
                "routeOverlay",
                "dashboardOverlay",
            )
            items = {name: root.findChild(QQuickItem, name) for name in names}
            for name, item in items.items():
                self.assertIsNotNone(item, name)

            self.assertIs(items["mapContent"].parentItem(), items["mapClip"])
            for name in (
                "baseBand",
                "chartLayer",
                "weatherBand",
                "navigationOverlay",
                "routeOverlay",
            ):
                self.assertIs(items[name].parentItem(), items["mapContent"], name)
            self.assertIs(items["weatherBarbs"].parentItem(), items["weatherBand"])
            self.assertIs(items["dashboardOverlay"].parentItem(), root)

            def scene_pos(item):
                return item.mapToItem(root, QPointF(0, 0))

            stationary = ("baseBand", "weatherBarbs", "dashboardOverlay", "mapClip")
            moving = ("chartLayer", "navigationOverlay", "routeOverlay")
            before = {name: scene_pos(items[name]) for name in stationary + moving}
            items["mapContent"].setX(items["mapContent"].x() + 12)
            items["mapContent"].setY(items["mapContent"].y() + 7)
            self.app.processEvents()

            for name in moving:
                after = scene_pos(items[name])
                self.assertAlmostEqual(
                    after.x() - before[name].x(), 12, places=3, msg=name
                )
                self.assertAlmostEqual(
                    after.y() - before[name].y(), 7, places=3, msg=name
                )
            for name in stationary:
                after = scene_pos(items[name])
                self.assertAlmostEqual(
                    after.x() - before[name].x(), 0, places=3, msg=name
                )
                self.assertAlmostEqual(
                    after.y() - before[name].y(), 0, places=3, msg=name
                )
        finally:
            component.deleteLater()
            engine.deleteLater()


if __name__ == "__main__":
    unittest.main()
