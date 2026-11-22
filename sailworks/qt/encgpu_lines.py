"""Native GPU line layer for the ENC scene viewport.

The layer draws the packed ENC line payload (:data:`encmesh.GPU_LINE_CLASSES`
plus solid ``DEPCNT``) with a ``GpuLineItem`` from the optional
``SailworksRouting`` QML module.  That module is a build output, so everything
here is optional by design: an explicit environment opt-out, a missing module
and a failed component all leave the viewport on its CPU stroke path with a
single mesh item carrying every kind.

The viewport owns exactly one :class:`EncGpuLineLayer` and calls it; nothing
else holds the ``GpuLineItem`` or sets the mesh-side availability flag.
"""

from pathlib import Path
import os

from sailworks.rendering.vectordrawer import encmesh

#: Switch for the packed ENC line payload drawn by ``GpuLineItem``.  It is the
#: mesh-side flag, reused so the payload and the drawing cannot disagree: with
#: it off no scene publishes ``line_segments`` and this layer is never even
#: looked up.  It is on only once a native line item was created: the default
#: install has no built module, so the CPU stroke stays the default.
ENV_ENC_GPU_LINES = encmesh.GPU_LINES_ENV


def requested():
    """Whether construction should try to create the native line item.

    The attempt cannot be gated on :func:`enabled`: that reads the
    availability flag the attempt itself is about to set.  Only an explicit
    environment opt-out is honoured here, and it returns ``False`` before any
    module lookup; an unset variable still attempts creation so a machine with
    the built module gets the native layer by default.
    """
    if encmesh.gpu_lines_forced_off():
        return False
    return True


def forced_off():
    """Whether the environment explicitly opted out of the native layer."""
    return encmesh.gpu_lines_forced_off()


def enabled():
    """Whether the layer may be written to: available and not opted out."""
    return encmesh.gpu_lines_enabled()


def qml_dir():
    """The built ``SailworksRouting`` module's import parent, or ``None``.

    Mirrors the retained overlay's lookup: an explicit override first, then
    the build outputs.  The directory must actually hold the module's
    ``qmldir``, so the layer never adds an import path that cannot resolve.
    """
    configured = os.environ.get("SAILWORKS_ROUTING_QML_PATH")
    if configured:
        path = Path(configured)
        if (path / "SailworksRouting" / "qmldir").is_file():
            return path
        return None
    build = Path(__file__).resolve().parents[2] / "build"
    for candidate in (build / "routing-qsg-module", build / "routing-qt" / "qml"):
        if (candidate / "SailworksRouting" / "qmldir").is_file():
            return candidate
    return None


def payload(scene):
    """The packed payload bytes for ``scene``, or ``b""`` when it has none.

    ``LineSegments.to_bytes()`` is the exact layout ``GpuLineItem.segmentData``
    documents: ``N`` float32 records of ``lon0, lat0, lon1, lat1, width,
    r, g, b, a, dash_on, dash_off, phase``.  A scene without the payload keeps
    the CPU stroke, which is drawn by the scene item itself.
    """
    segments = getattr(scene, "line_segments", None)
    if segments is None or bool(getattr(segments, "is_empty", True)):
        return b""
    to_bytes = getattr(segments, "to_bytes", None)
    if not callable(to_bytes):
        return b""
    return bytes(to_bytes())


def compensate_pan(item, map_content):
    """Cancel the shared ``mapContent`` pan on the native line layer.

    The item is parented to ``encSceneLayer``, which lives inside the panned
    ``mapContent``.  Its matrix already projects degrees through the camera, so
    the inherited translation is a double pan: mirror ``mapContent``'s x/y and
    keep following it, exactly as ``streamlineBand`` does in QML.
    """
    if item is None or map_content is None:
        return False

    def sync():
        item.setX(-float(map_content.x()))
        item.setY(-float(map_content.y()))

    sync()
    map_content.xChanged.connect(sync)
    map_content.yChanged.connect(sync)
    return True


def _qbytearray(data):
    """Wrap packed bytes for the item's ``QByteArray`` property.

    Imported here for the same reason the ENC items are: this module's Qt Core
    imports stay at the top, and the path may never run at all.
    """
    from PySide6.QtCore import QByteArray

    return QByteArray(data)


def _create_item(host, container):
    """Load one ``GpuLineItem`` from QML; ``(None, None)`` on any failure.

    The item is deliberately not declared in ``ViewportChartScene.qml``: an
    unconditional ``import SailworksRouting`` makes every start depend on a
    built C++ module, and the default install has none.  It is created only
    when the environment has not opted out *and* the module is on disk.
    """
    if container is None or not requested():
        return None, None
    # Local, like the QML imports below: the default path never loads this.
    from PySide6.QtQuick import QQuickItem

    module_dir = qml_dir()
    if module_dir is None:
        return None, None
    engine = host.engine() if hasattr(host, "engine") else None
    if engine is None:
        # A host that is only the root item cannot name the engine; the item's
        # own QML context can, and that is the engine that must load the type.
        try:
            from PySide6.QtQml import QQmlEngine

            context = QQmlEngine.contextForObject(container)
            engine = None if context is None else context.engine()
        except Exception:
            engine = None
    if engine is None:
        return None, None
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlComponent

        engine.addImportPath(str(module_dir))
        component = QQmlComponent(engine)
        component.setData(
            b"import QtQuick\nimport SailworksRouting 1.0\nGpuLineItem {}\n",
            QUrl.fromLocalFile(str(module_dir / "SailworksRouting") + "/"),
        )
        if component.isError():
            return None, None
        item = component.create()
    except Exception:
        # A missing module, a plugin built for another Qt, a QML error: the
        # layer is optional, so the default drawing path simply continues.
        return None, None
    if not isinstance(item, QQuickItem):
        return None, None
    item.setParentItem(container)
    item.setWidth(container.width())
    item.setHeight(container.height())
    container.widthChanged.connect(lambda: item.setWidth(container.width()))
    container.heightChanged.connect(lambda: item.setHeight(container.height()))
    compensate_pan(item, container.parentItem())
    item.setVisible(False)
    return item, component


class EncGpuLineLayer:
    """The viewport's native line item and the state the mesh side reads.

    ``create`` runs once while the scene layer comes up and records
    availability on the mesh side before any worker builds a scene, so the
    payload and the drawing cannot disagree.  ``update`` uploads a published
    scene, ``set_camera`` moves an already-published payload with the live
    camera between publications, and ``clear`` withdraws it with its scene.
    Every method is a no-op before ``create`` succeeded.
    """

    def __init__(self, item=None):
        # ``item`` lets a caller (or a test) adopt an already-built item; the
        # viewport always starts empty and builds through ``create``.
        self._item = item
        # PySide6 ties the created object to its component: dropping the
        # component deletes the item, so the layer owns both.
        self._component = None

    @property
    def item(self):
        """The ``GpuLineItem``, or ``None`` when the layer was never built."""
        return self._item

    @property
    def available(self):
        """Whether a native item exists and may draw the packed lines."""
        return self._item is not None

    def create(self, host, container):
        """Build the item from QML, record availability, and return it.

        Records ``None`` availability on every failure path, which is what
        keeps the CPU stroke and the single all-kinds mesh item in place.
        """
        item, component = _create_item(host, container)
        self._item = item
        self._component = component
        encmesh.set_gpu_lines_available(item is not None)
        return item

    def set_camera(self, camera):
        """Point the item's camera matrix at the live view, not a composed frame.

        The payload is geographic, so pan and zoom are uniforms: the matrix
        follows the current camera between publications, which is what lets
        one published payload stay correct while the map moves.  The device
        pixel ratio is not set here: the item reads its window's ratio itself
        and only uses it for the one-pixel hairline floor.  ``lineWidthScale``
        stays 1 because the published widths are already screen pixels.
        """
        item = self._item
        if item is None or camera is None:
            return False
        item.setProperty("centerLatitude", float(camera.center_lat))
        item.setProperty("centerLongitude", float(camera.center_lon))
        item.setProperty("zoom", float(camera.zoom))
        return True

    def update(self, scene, camera=None):
        """Upload ``scene``'s packed lines; ``False`` when the layer is unbuilt.

        The flag gates *every* write, so the default process never touches the
        item; a scene without a payload clears the layer instead of leaving a
        stale one on screen, and the scene item's own CPU stroke keeps drawing
        those classes.
        """
        item = self._item
        if item is None or not enabled():
            return False
        data = payload(scene)
        if not data:
            self.clear()
            return False
        item.setProperty("segmentData", _qbytearray(data))
        self.set_camera(camera)
        item.setVisible(True)
        return True

    def clear(self):
        """Drop the payload and hide the layer; a no-op when it is unbuilt.

        Called on every withdrawal, so a layer never outlives the scene it was
        drawn for.  An empty ``segmentData`` is what makes the item allocate
        no geometry at all.
        """
        item = self._item
        if item is None:
            return False
        item.setProperty("segmentData", _qbytearray(b""))
        item.setVisible(False)
        return True
