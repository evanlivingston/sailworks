"""One ENC cell painted headlessly into an image for a refused plan clip.

The GPU scene can refuse a cell's plan clip; the fallback beneath it needs
that one cell painted at the camera's own projection.  This module paints it:
a cairo surface over a ``QImage`` buffer, the application's own
``S57ChartDrawer``, and the scene's published projection -- the same callable
``projection_for`` hands the ENC meshes, which maps lon/lat degrees to the
viewport's logical pixels.  No QML and no GL context is involved, so the paint
is safe to run off the GUI thread.

The module also owns the layer that presents those images: ``ENCFallbackItem``
is one ``QQuickPaintedItem`` the viewport parents beneath its mesh item, and
``ENCFallbackOverlay`` is the controller that renders on a daemon worker and
hands the image back on the GUI thread.  Neither the item nor the controller
is constructed unless the ENC scene layer is enabled.
"""

import os
import threading
import time
from collections import OrderedDict

import cairo
import numpy as np
import shapely
from osgeo import ogr
from PySide6.QtCore import QObject, QRectF, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPainter
from PySide6.QtQuick import QQuickPaintedItem

from sailworks.qt import enc_trace
from sailworks.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

#: The palette the drawer uses when the caller does not name one.
DEFAULT_PALETTE = "cm93"


class _HeadlessSettings:
    """The settings the drawers read, without the application's manager."""

    def __init__(self, palette):
        self.chartPalette = palette

    def register_on_change(self, key, callback):
        """The palette does not change while a fallback is being painted."""
        return None


class _ProjectedCanvas:
    """The canvas API the drawers expect, over the scene's projection.

    ``S57ChartDrawer`` projects through its canvas: it asks for the allocated
    size, the display scale and the screen coordinates of lon/lat points.  The
    scene publishes exactly the last of those, as a callable; the size is the
    image's own and the scale is derived from that same callable.
    """

    def __init__(self, projection, width, height, latitude):
        self._projection = projection
        self._width = width
        self._height = height
        # The metre scale is only read as a display gate by the drawer, and
        # Mercator's scale depends on latitude; the clip's own latitude is the
        # one the fallback is painted at.
        self._latitude = latitude

    def get_allocated_width(self):
        return self._width

    def get_allocated_height(self):
        return self._height

    def get_scale_factor(self):
        return 1.0

    def project(self, points):
        """The scene's projection, as an ``(N, 2)`` array."""
        points = np.asarray(points, dtype=np.float64)
        if points.shape[0] == 0:
            return np.empty((0, 2), dtype=np.float64)
        return np.asarray(self._projection(points), dtype=np.float64)

    def convert_geographic_to_screen_batch(self, lats, lons):
        screen = self.project(np.column_stack((lons, lats)))
        return screen[:, 0], screen[:, 1]

    def convert_geographic_to_screen(self, map_point):
        lat, lon = map_point.get_degrees()
        x, y = self.convert_geographic_to_screen_batch(np.array([lat]), np.array([lon]))
        return float(x[0]), float(y[0])

    def get_scale(self):
        """Meters per pixel, from the projection's own longitude scale.

        This is ``MercatorProjection.get_scale``'s formula with the pixels per
        degree measured through the published projection, so it describes the
        projection the fallback is painted at rather than a second one.
        """
        equator = self.project(np.array([[0.0, self._latitude], [1.0, self._latitude]]))
        per_degree = abs(float(equator[1, 0] - equator[0, 0]))
        if per_degree <= 0.0:
            return float("inf")
        circumference = 2 * np.pi * 6378137
        return np.cos(np.radians(self._latitude)) * circumference / (360.0 * per_degree)

    def render_cancelled(self):
        return False

    def queue_draw(self):
        return None


def _clip(clip_wkb):
    """The clip polygon, or ``None`` when it cannot enclose any ground."""
    try:
        clip = shapely.from_wkb(clip_wkb) if clip_wkb else None
    except Exception:
        return None
    if clip is None or clip.is_empty or not clip.is_valid or clip.area <= 0.0:
        return None
    if not clip.geom_type.endswith("Polygon"):
        return None
    return clip


def _clip_path(canvas, clip):
    """The clip polygon's rings, in image pixels.

    The rings keep the orientation they were stored with, because a WKB
    polygon's interior ring is not required to wind against its exterior.  The
    path is clipped with the even-odd fill rule, which cuts a hole whatever its
    winding: the holes of a plan clip are the ground a higher-priority cell
    already owns, so filling one would draw that ground twice.
    """
    rings = []
    polygons = clip.geoms if clip.geom_type == "MultiPolygon" else (clip,)
    for polygon in polygons:
        rings.append(shapely.get_coordinates(polygon.exterior))
        for interior in polygon.interiors:
            rings.append(shapely.get_coordinates(interior))
    return [canvas.project(ring) for ring in rings]


def _paint(image, canvas, clip, clip_wkb, cell_path, palette, offset=(0, 0)):
    """Paint the cell into *image*; raises whatever the paint path raises.

    *offset* is the device-pixel position of the image's top-left corner in
    the frame the canvas projects into.  The drawing commands stay in frame
    coordinates and cairo translates them, so a bounded render rasterizes
    exactly as a full-frame one does: the offset is integral.
    """
    buffer = image.bits()
    surface = cairo.ImageSurface.create_for_data(
        buffer,
        cairo.FORMAT_ARGB32,
        image.width(),
        image.height(),
        image.bytesPerLine(),
    )
    vector_file = None
    try:
        cr = cairo.Context(surface)
        cr.save()
        cr.translate(-float(offset[0]), -float(offset[1]))
        for ring in _clip_path(canvas, clip):
            if len(ring) == 0:
                continue
            cr.move_to(float(ring[0][0]), float(ring[0][1]))
            for x, y in ring[1:]:
                cr.line_to(float(x), float(y))
            cr.close_path()
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.clip()

        # The bounding is also the drawer's simplification tolerance, so it
        # must be the clip and never the world: the clip is a plan's own,
        # already intersected with the viewport.
        bounding = ogr.CreateGeometryFromWkb(bytes(clip_wkb))
        if bounding is None:
            return
        vector_file = ogr.Open(cell_path)
        if vector_file is None:
            raise FileNotFoundError("cannot open ENC cell %s" % (cell_path,))
        drawer = S57ChartDrawer(_HeadlessSettings(palette or DEFAULT_PALETTE))
        drawer.draw(canvas, cr, vector_file, bounding)
        del drawer
        cr.restore()
        surface.flush()
    finally:
        # The chart handle and the prepared geometry live exactly as long as
        # the paint: nothing the caller handed in outlives this call.
        del vector_file
        del surface, buffer


def _clip_rect(canvas, clip):
    """The clip's projected extent, or ``None`` when it has no usable extent.

    ``None`` means the extent cannot be established: no ring survives the
    projection, or one of them projects to a non-finite coordinate.  The rect
    is exact and unclamped, so it describes where the clip lands even when
    that is outside the frame.
    """
    rings = [np.asarray(ring, dtype=np.float64) for ring in _clip_path(canvas, clip)]
    rings = [ring for ring in rings if ring.shape[0] > 0]
    if not rings:
        return None
    points = np.concatenate(rings)
    if not np.all(np.isfinite(points)):
        return None
    return (
        float(points[:, 0].min()),
        float(points[:, 1].min()),
        float(points[:, 0].max()),
        float(points[:, 1].max()),
    )


def overlay_clip_rect(clip_wkb, projection, width, height):
    """The clip's projected rect in the frame *projection* describes.

    This is the one geometry the overlay's image and its presentation must
    both agree on: the renderer sizes its bounded image from it, and a caller
    that has to say where the ground lands in the frame -- the item that
    presents it, and the tests that pin it -- reads it here rather than
    re-deriving the projection.  *projection* and *width*/*height* are in the
    same pixel grid: pass the scene's logical projection with the logical
    frame, or :func:`device_projection` with the device frame.

    ``None`` when the clip encloses no ground or its projection is not finite.
    """
    clip = _clip(clip_wkb)
    if clip is None:
        return None
    min_lon, min_lat, max_lon, max_lat = clip.bounds
    canvas = _ProjectedCanvas(projection, width, height, (min_lat + max_lat) / 2.0)
    return _clip_rect(canvas, clip)


def _clip_pixel_bounds(canvas, clip, width, height, pad=2):
    """The frame box the clip can touch, in device pixels.

    The box is the clip's projected extent widened by *pad* pixels, so the
    antialiased edge of the clip path itself always has a pixel to land on,
    and then clamped to the frame.  ``None`` means the extent cannot be
    established (an empty or non-finite projection), in which case the caller
    must paint the whole frame; a box with no pixels means the clip lies
    outside this frame and paints nothing at all.
    """
    rect = _clip_rect(canvas, clip)
    if rect is None:
        return None
    left = int(np.floor(rect[0])) - pad
    top = int(np.floor(rect[1])) - pad
    right = int(np.ceil(rect[2])) + pad + 1
    bottom = int(np.ceil(rect[3])) + pad + 1
    return (
        max(0, left),
        max(0, top),
        min(width, right),
        min(height, bottom),
    )


def render_cell_overlay(cell_path, clip_wkb, projection, width, height, palette=None):
    """Paint one cell into a transparent image, clipped to *clip_wkb*.

    ``projection`` is the scene's published projection: a callable mapping an
    ``(N, 2)`` array of lon/lat degrees to the viewport's pixel coordinates.
    ``clip_wkb`` is the refused plan clip's polygon in chart coordinates.  The
    image is ``width`` x ``height`` device pixels, transparent everywhere the
    clip does not reach.

    The paint itself is bounded to the clip's projected box: an image of that
    box is painted with an offset transform and then composited at its place
    in the frame.  The clip is a few percent of the viewport, so this is the
    difference between painting the whole frame and painting the ground that
    was actually asked for.  The composited result is what a full-frame paint
    would have produced inside the clip.

    Raises ``FileNotFoundError`` when *cell_path* does not exist.  A clip
    without area, and a cell that cannot be opened or drawn, paint nothing:
    the transparent image is returned, so a fallback failure never reaches the
    worker as an exception.
    """
    if not os.path.exists(cell_path):
        raise FileNotFoundError("no ENC cell at %s" % (cell_path,))

    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    if image.isNull():
        return image

    clip = _clip(clip_wkb)
    if clip is None:
        return image

    min_lon, min_lat, max_lon, max_lat = clip.bounds
    canvas = _ProjectedCanvas(projection, width, height, (min_lat + max_lat) / 2.0)

    bounds = _clip_pixel_bounds(canvas, clip, width, height)
    if bounds is None:
        target, offset = image, (0, 0)
    else:
        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            # The clip touches no pixel of this frame.
            return image
        target = QImage(right - left, bottom - top, image.format())
        if target.isNull():
            target, offset = image, (0, 0)
        else:
            target.fill(Qt.GlobalColor.transparent)
            offset = (left, top)

    try:
        _paint(target, canvas, clip, clip_wkb, cell_path, palette, offset)
    except Exception:
        # A fallback that cannot paint leaves the ground to whatever is above
        # it; it must not fail the worker that asked for it.
        target.fill(Qt.GlobalColor.transparent)

    if target is not image:
        painter = QPainter(image)
        try:
            painter.drawImage(offset[0], offset[1], target)
        finally:
            painter.end()
    return image


def render_clips_overlay(clips, projection, width, height, palette=None):
    """Composite several refused cells into one transparent image.

    *clips* has the shape the layer publishes as ``unavailable_clips``: an
    iterable of mappings with ``path`` and ``clip_wkb``.  Each cell is painted
    on its own and then composited in plan order, so a cell that cannot be
    painted leaves the ones around it untouched -- the same one-cell
    guarantee ``render_cell_overlay`` gives.  ``QPainter`` over a ``QImage``
    runs on the raster engine, which Qt documents as safe outside the GUI
    thread.
    """
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    if image.isNull():
        return image
    painter = QPainter(image)
    try:
        for entry in clips or ():
            if not entry or not entry.get("path"):
                continue
            try:
                overlay = render_cell_overlay(
                    entry["path"],
                    entry.get("clip_wkb"),
                    projection,
                    width,
                    height,
                    palette,
                )
            except FileNotFoundError:
                # A cell that vanished since the plan is already the scene's
                # business; the overlay simply has no ground for it.
                continue
            painter.drawImage(0, 0, overlay)
    finally:
        painter.end()
    return image


#: How many composed frames the fallback keeps memoized at most.
RENDER_CACHE_LIMIT = 8
#: How much composed pixel data the fallback keeps memoized.  A frame is a
#: full-size ARGB image, so a count alone would bound very different amounts:
#: at 1920x1386 and DPR 2 one entry is ~42 MB.
RENDER_CACHE_BUDGET = 64 * 1024 * 1024

_render_cache = OrderedDict()
_render_cache_bytes = 0
_render_cache_lock = threading.Lock()


def _image_nbytes(image):
    """The approximate pixel cost of one cached frame."""
    return image.width() * image.height() * 4


def _cell_stamp(path):
    """A cell's content stand-in: its real path, size and modification time."""
    try:
        info = os.stat(path)
    except OSError:
        return (str(path), None, None)
    return (os.path.realpath(str(path)), int(info.st_size), int(info.st_mtime_ns))


def render_cache_key(clips, camera, width, height, dpr, palette=None):
    """Everything the composed pixels depend on, as a hashable key.

    The cell's path, size and modification time stand in for its content; the
    clip's WKB is the exact geometry the paint clips to; the camera's own
    latitude, longitude and zoom determine the projection; and the device
    frame plus ratio fix the pixel grid.  Anything that changes the pixels
    changes the key, so the cache invalidates itself and never needs an
    explicit flush.
    """
    cells = []
    for entry in clips or ():
        path = entry.get("path") if entry else None
        if not path:
            continue
        cells.append((_cell_stamp(path), bytes(entry.get("clip_wkb") or b"")))
    return (
        "enc-fallback",
        1,
        tuple(cells),
        getattr(camera, "center_lat", None),
        getattr(camera, "center_lon", None),
        getattr(camera, "zoom", None),
        int(width),
        int(height),
        float(dpr or 1.0),
        palette or DEFAULT_PALETTE,
    )


def clear_render_cache():
    """Drop every memoized compose."""
    global _render_cache_bytes
    with _render_cache_lock:
        _render_cache.clear()
        _render_cache_bytes = 0


def render_clips_overlay_cached(
    clips, projection, width, height, camera=None, dpr=1.0, palette=None
):
    """``render_clips_overlay`` memoized: ``(image, hit)``.

    A repeat compose for an unchanged key hands back the image already
    painted, so a republish or a re-place of the same ground costs no drawer.
    The cache is a byte-bounded LRU: a pan walks through keys and the least
    recently used frame falls out first until the resident pixels fit
    :data:`RENDER_CACHE_BUDGET` (and at most :data:`RENDER_CACHE_LIMIT`
    entries).  The frame just painted is always kept, so an entry larger than
    the whole budget still memoizes instead of thrashing.  The key changes with
    the cells' content, the clip, the camera and the device frame, which is
    also the whole of the invalidation policy.
    """
    key = render_cache_key(clips, camera, width, height, dpr, palette)
    with _render_cache_lock:
        cached = _render_cache.get(key)
        if cached is not None:
            _render_cache.move_to_end(key)
            return cached, True

    image = render_clips_overlay(clips, projection, width, height, palette)
    if image is not None and not image.isNull():
        global _render_cache_bytes
        with _render_cache_lock:
            previous = _render_cache.pop(key, None)
            if previous is not None:
                _render_cache_bytes -= _image_nbytes(previous)
            _render_cache[key] = image
            _render_cache_bytes += _image_nbytes(image)
            while len(_render_cache) > 1 and (
                _render_cache_bytes > RENDER_CACHE_BUDGET
                or len(_render_cache) > RENDER_CACHE_LIMIT
            ):
                _, evicted = _render_cache.popitem(last=False)
                _render_cache_bytes -= _image_nbytes(evicted)
    return image, False


def device_projection(projection, scale):
    """*projection* at device-pixel scale.

    The overlay image is allocated at device-pixel size and drawn into the
    item's logical rect, so the scene's logical-pixel projection has to be
    scaled by the device pixel ratio for the two to line up one to one.
    """
    if scale == 1.0:
        return projection

    def project(points):
        projected = np.asarray(projection(points), dtype=np.float64)
        if projected.shape[0] == 0:
            return projected
        return projected * scale

    return project


def fallback_clips(metadata):
    """The refused cells an overlay can paint for *metadata*'s scene.

    Only entries naming a path and a clip are kept: ground the overlay cannot
    place stays the scene's business instead of becoming a transparent
    request.
    """
    if not metadata:
        return ()
    clips = []
    for entry in metadata.get("unavailable_clips") or ():
        if not entry or not entry.get("path") or not entry.get("clip_wkb"):
            continue
        clips.append(dict(entry))
    return tuple(clips)


def fallback_request(metadata):
    """``(clips, projection, camera)`` to paint for a publication, or ``None``.

    The projection travels with the scene that published it, so it describes
    exactly one camera: a publication that names none, or that refused no
    cell, paints nothing.
    """
    clips = fallback_clips(metadata)
    if not clips:
        return None
    projection = metadata.get("projection")
    if projection is None:
        return None
    return clips, projection, metadata.get("camera")


def fallback_accepts(payload, token):
    """Whether a completed render still describes the requested view.

    *payload* is ``(token, image[, fields])`` from the worker.  Only the
    newest request is current: a render whose token names an earlier request
    belongs to a camera or a scene the view has already left, and painting it
    would place the previous view's ground under this one.
    """
    if payload is None:
        return False
    job_token, image = payload[0], payload[1]
    if token is None or job_token is None or job_token != token:
        return False
    return image is not None and not image.isNull()


def fallback_compose_fields(
    generation, serial, clips, width, height, elapsed_ms, failed
):
    """The one-line record of a completed fallback render.

    Names come from the plan entry's own ``name``; a layer that named none
    still leaves the cell identifiable by its file.
    """
    names = []
    for entry in clips or ():
        name = entry.get("name")
        if not name:
            name = os.path.basename(str(entry.get("path", "")))
        names.append(name)
    return {
        "generation": generation,
        "serial": serial,
        "clips": len(clips or ()),
        "cells": names,
        "width": width,
        "height": height,
        "elapsed_ms": elapsed_ms,
        "failed": bool(failed),
    }


def _premultiplied(image):
    """*image* in a format Qt composites with alpha, never as black.

    An ``Format_ARGB32`` image carries straight (unpremultiplied) colour: a
    fully transparent pixel holds whatever RGB the producer left there, and
    Qt Quick uploads and composites the texture as premultiplied, so those
    pixels render as opaque black.  Every image the viewport paints, stores or
    presents is therefore premultiplied; anything else is converted on the way
    in rather than trusted.
    """
    if image is None or image.isNull():
        return image
    if image.format() in (
        QImage.Format.Format_ARGB32,
        QImage.Format.Format_RGB32,
        QImage.Format.Format_RGBA8888,
        QImage.Format.Format_RGBA64,
    ):
        return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    return image


class ENCFallbackItem(QQuickPaintedItem):
    """The refused cells' ground, painted beneath the GPU scene.

    The image is rendered at device-pixel size for the camera's own frame and
    drawn one image pixel to one device pixel.  The presented rect is derived
    from the painter's device scale -- the grid the window actually composes
    on -- and never from the item's size, so a container that is larger,
    smaller or resized between publications cannot stretch the ground to a
    second scale: the pixels stay where the camera's projection put them, and
    everything outside the image (and outside the clip within it) is
    transparent premultiplied black, not black paint.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = QImage()
        self.setVisible(False)

    def image(self):
        """The image on display; null when nothing is painted."""
        return self._image

    def set_image(self, image):
        """Present *image*, or clear for a null one."""
        if image is None or image.isNull():
            self.clear()
            return
        self._image = _premultiplied(image)
        self.setVisible(True)
        self.update()

    def clear(self):
        """Paint nothing; the scene above is the whole picture again."""
        self._image = QImage()
        self.setVisible(False)
        self.update()

    def _image_rect(self, painter):
        """The image's rect in item coordinates: one image pixel per device one."""
        world = painter.transform()
        scale_x = abs(float(world.m11())) or 1.0
        scale_y = abs(float(world.m22())) or 1.0
        return QRectF(
            0.0,
            0.0,
            self._image.width() / scale_x,
            self._image.height() / scale_y,
        )

    def paint(self, painter):
        image = self._image
        if image.isNull() or image.width() <= 0 or image.height() <= 0:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        # The item's texture is reused between paints, so the ground of a
        # previous frame must be cleared rather than left under this one: a
        # clipped-away region would otherwise keep showing stale chart.
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(
            QRectF(0.0, 0.0, self.width(), self.height()),
            Qt.GlobalColor.transparent,
        )
        painter.restore()
        painter.drawImage(self._image_rect(painter), image)


class ENCFallbackOverlay(QObject):
    """Paints a scene's refused cells into the item beneath the GPU scene.

    The GUI thread owns the item, the token it is waiting for and the last
    known clips; the paint runs on one daemon worker that keeps only the
    newest request, so a pan cannot queue a paint per frame.  A render whose
    token is no longer current is dropped, so a camera change or a newer
    publication never shows the previous view's ground.
    """

    rendered = Signal(object)  # noqa: N815 - Qt signal name

    def __init__(self, container, dpr=1.0, parent=None):
        super().__init__(parent)
        self._item = ENCFallbackItem(container)
        self._item.setWidth(container.width())
        self._item.setHeight(container.height())
        container.widthChanged.connect(lambda: self._item.setWidth(container.width()))
        container.heightChanged.connect(
            lambda: self._item.setHeight(container.height())
        )
        self._dpr = max(1.0, float(dpr or 1.0))
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._job = None
        self._token = None
        self._serial = 0
        self._clips = ()
        #: ``(projection, camera, generation)`` of the last publication.
        self._canvas = None
        self._thread = None
        self._stopped = False
        # The worker emits off the GUI thread: only this thread may touch the
        # item, so the hand-back is explicitly queued.
        self.rendered.connect(self._painted, Qt.ConnectionType.QueuedConnection)

    @property
    def item(self):
        """The QML item this overlay paints into."""
        return self._item

    def place(self, x, y):
        """Move the item exactly where the mesh item was just placed.

        Both items share the chart's translated parent; the viewport calls
        this with the same ``enc_scene_placement`` values it sets on the mesh,
        so a pan moves the painted ground with the cells it belongs to.
        """
        self._item.setX(float(x))
        self._item.setY(float(y))

    def publish(self, metadata, generation=None, dpr=None):
        """A scene publication: paint what it refused, or clear when final.

        A partial publication -- and a publication that names no refused cell
        while the scene is still composing -- proves nothing about the cells
        it has not resolved yet.  It therefore never discards retained state
        and never invalidates a live render.  Only a complete, non-partial
        publication without refused clips clears.
        """
        moved_dpr = self._adopt_dpr(dpr)
        request = fallback_request(metadata)
        if request is None:
            if metadata is None or metadata.get("partial"):
                return
            self.clear("no_clips")
            return
        if moved_dpr:
            # The image on screen was painted for the old ratio: drop it
            # rather than let it stretch until the new paint lands.
            self.clear("dpr_changed")
        clips, projection, camera = request
        self._start(clips, projection, camera, generation)

    def camera_changed(self, camera, dpr=None):
        """The view moved: keep the painted ground, discard stale renders.

        The mesh keeps its previous frame and shifts it with ``place()``, so
        the overlay does the same: the image painted for the old camera stays
        up.  It is never re-rendered with a projection that belongs to another
        camera; the serial is bumped instead, so a render still in flight for
        the old camera is rejected as stale, and the new camera's publication
        paints the new view.  ``dpr`` is the viewport's current device pixel
        ratio -- the same value it places the mesh with -- so a move to another
        display re-renders at the new ratio instead of stretching an image
        painted for the old one.
        """
        moved_dpr = self._adopt_dpr(dpr)
        if self._canvas is None:
            # Nothing was ever painted: drop any in-flight token.
            self.clear("no_clips")
            return
        projection, published, generation = self._canvas
        if camera is not None and published is not None and camera != published:
            self._serial += 1
            # A token no job will ever carry: whatever is in flight is stale.
            self._set_token((self._serial, camera))
            return
        if moved_dpr:
            clips = self._clips
            self.clear("dpr_changed")
            self._start(clips, projection, published, generation)
            return
        if self._item.image().isNull():
            self._start(self._clips, projection, published, generation)

    def clear(self, reason="cleared"):
        """Drop the painted ground and any render still in flight.

        ``reason`` is traced only when an image was actually on screen: a
        clear that had nothing to discard must not fill the trace.
        """
        had_image = not self._item.image().isNull()
        with self._lock:
            self._token = None
        self._clips = ()
        self._canvas = None
        self._item.clear()
        if had_image:
            enc_trace.event("fallback_clear", reason=reason)

    def stop(self):
        """End the worker; a paint in flight finishes into nothing."""
        with self._lock:
            self._stopped = True
            self._token = None
            self._job = None
            self._wake.notify_all()
        self.clear("stopped")

    def _set_token(self, token):
        """Publish the request the view is waiting for.

        The worker reads the token under the same lock, so the staleness
        decision it takes before painting cannot straddle a camera change.
        """
        with self._lock:
            self._token = token

    def _current(self, token):
        """Whether *token* is still the request the view is waiting for."""
        with self._lock:
            return (
                not self._stopped
                and token is not None
                and self._token is not None
                and self._token == token
            )

    def _adopt_dpr(self, dpr):
        """Take the viewport's current ratio; true when it actually moved."""
        if dpr is None:
            return False
        try:
            value = max(1.0, float(dpr))
        except (TypeError, ValueError):
            return False
        if value == self._dpr:
            return False
        self._dpr = value
        return True

    def _start(self, clips, projection, camera, generation=None):
        """Queue the newest demand: one job in flight, the newest wins."""
        if not clips or projection is None:
            self.clear("no_clips")
            return
        width = int(round(max(0.0, float(getattr(camera, "width", 0.0))) * self._dpr))
        height = int(round(max(0.0, float(getattr(camera, "height", 0.0))) * self._dpr))
        if width <= 0 or height <= 0:
            self.clear("no_camera")
            return
        self._serial += 1
        token = (self._serial, camera)
        job = (
            token,
            clips,
            device_projection(projection, self._dpr),
            width,
            height,
            generation,
            self._dpr,
        )
        self._clips = clips
        self._canvas = (projection, camera, generation)

        with self._lock:
            self._token = token
            self._job = job
            self._wake.notify()
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._stopped = False
                self._thread = threading.Thread(
                    target=self._run, name="enc-fallback", daemon=True
                )
                self._thread.start()

    def _run(self):
        """The worker: paint the newest job; the GUI thread presents it."""
        while True:
            with self._lock:
                while self._job is None and not self._stopped:
                    self._wake.wait()
                if self._stopped:
                    return
                job = self._job
                self._job = None
            payload = self._compose(job)
            if payload is not None:
                self.rendered.emit(payload)

    def _compose(self, job):
        """Paint one queued job, or ``None`` when it was already superseded.

        The staleness decision is the one ``fallback_accepts`` makes for a
        finished render, taken before the paint rather than after it: a frame
        whose token the view has already left would be dropped on hand-back,
        so painting it at all is work no one will see.  A skipped job is
        traced and never published.
        """
        token, clips, projection, width, height, generation, dpr = job
        if not self._current(token):
            enc_trace.event(
                "fallback_skip",
                reason="stale",
                generation=generation,
                serial=token[0] if token else None,
                clips=len(clips or ()),
            )
            return None
        started = time.perf_counter()
        try:
            image, cached = render_clips_overlay_cached(
                clips, projection, width, height, camera=token[1], dpr=dpr
            )
        except Exception:
            # A fallback that cannot paint leaves the ground to the scene
            # above it; it must not take the worker down.
            image, cached = QImage(), False
        enc_trace.event(
            "fallback_cache",
            hit=bool(cached),
            generation=generation,
            serial=token[0] if token else None,
        )
        fields = fallback_compose_fields(
            generation,
            token[0],
            clips,
            width,
            height,
            enc_trace.elapsed_ms(started),
            image is None or image.isNull(),
        )
        return (token, image, fields)

    @Slot(object)
    def _painted(self, payload):
        """Present a worker result on the GUI thread, if it is current."""
        if self._stopped:
            return
        token, image = payload[0], payload[1]
        fields = payload[2] if len(payload) > 2 else {}
        accepted = fallback_accepts(payload, self._token)
        # One line per completed render, current or dropped: a headless run
        # can prove the overlay ran even when its frame was superseded.
        enc_trace.event("fallback_compose", **fields, current=accepted)
        if not accepted:
            return
        self._item.set_image(image)
