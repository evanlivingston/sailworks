"""Opt-in GPU ENC scene layer for the viewport chart scene.

This layer assembles the installed and enabled ENC cells into one
:class:`sailworks.rendering.encview.ViewScene` on its own worker thread, with
its own GDAL handles and its own single-owner :class:`encscene.SceneCache`.
The GUI thread only receives the finished scene and hands it to an
:class:`sailworks.qt.encgpu.EncMeshItem`; no OGR or shapely object ever crosses
the thread boundary.

It is deliberately independent of the raster chart layer: when the viewport is
started with ``--enc-scene-layer`` the raster ENC source is switched off, so a
cell is never rasterised and triangulated at the same time.  The default chart
path does not construct this layer at all.

The scene carries no labels or symbols yet.  Every published scene therefore
records that incompleteness explicitly instead of presenting the shape-only
frame as a complete chart.  A scene published while cells are still being
assembled carries a second, explicit marker saying which partial cells it
holds, so progressive publication can be faster without being mistaken for a
finished view.
"""

from __future__ import annotations

import time
from pathlib import Path
from threading import Condition, Event, Thread

import numpy as np

from sailworks.rendering import encplan, encview
from sailworks.rendering.vectordrawer import encscene

STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_UNAVAILABLE = "unavailable"

#: The one incompleteness every assembled scene carries until labels and
#: symbols are drawn.  It is data, not a comment: the GUI reports it.
INCOMPLETE_LABELS = "labels/symbols not yet drawn"


def partial_incompleteness(done, total):
    """The explicit marker of a scene published before every cell resolved."""
    return f"partial cells ({done} of {total} drawn); {INCOMPLETE_LABELS}"


#: Bounded whole-cell scene cache, owned by this worker only.
SCENE_CACHE_BYTES = 64 * 1024 * 1024

#: Bounded per-cell mesh cache, a separate budget owned by this worker only.
#: A warm view change reuses these meshes instead of re-tessellating.  It must
#: hold one view's resident cells: each mesh is clipped to its cell's own
#: coverage, so it is larger than the visible slice.
MESH_CACHE_BYTES = 256 * 1024 * 1024

#: Screen-constant stroke width in logical pixels.
STROKE_PIXELS = 1.0

#: Keep the diagnostic history bounded, as the raster chart layer does.
HISTORY_LIMIT = 64


def resolve_enc_modes(
    chart_layer=False, enc_chart_layer=False, enc_scene_layer=False
) -> tuple[bool, bool, bool]:
    """Resolve the three ENC-related viewport flags.

    The scene layer implies the chart layer and supersedes the raster ENC
    source: a cell is drawn either as a raster overlay or as a GPU scene,
    never both.  The raster ENC layer has always implied the chart layer too,
    so ``--enc-chart-layer`` alone still enables the chart.  Returns
    ``(chart_layer, enc_chart_layer, enc_scene_layer)``.
    """
    scene = bool(enc_scene_layer)
    raster = bool(enc_chart_layer) and not scene
    return bool(chart_layer) or raster or scene, raster, scene


def mesh_scale_for(zoom):
    """The factor that puts a view's projection in the fixed mesh frame.

    ``projection_for`` maps degrees to world pixels at ``zoom``; Mercator world
    pixels scale linearly with ``2**zoom`` about a fixed origin, so multiplying
    by this factor gives the same projection at
    :data:`encview.MESH_REFERENCE_ZOOM`.  Meshes built there do not depend on
    the view, so a zoom reuses them instead of re-tessellating every cell.
    """
    return 2.0 ** (encview.MESH_REFERENCE_ZOOM - float(zoom))


def projection_for(viewport):
    """A projection from ENC degrees to the viewport's logical pixels.

    The ENC meshes carry lon/lat positions.  The viewport is built with
    ``dpr=1`` so the scene lands in QQuickItem logical coordinates; Qt applies
    the device pixel ratio when it composes the item.
    """
    batch = viewport.convert_geographic_to_screen_batch

    def project(points) -> np.ndarray:
        points = np.asarray(points, dtype=np.float64)
        if points.shape[0] == 0:
            return np.empty((0, 2), dtype=np.float64)
        # The item is a child of the map content, so its coordinates must be
        # local to the viewport.  ``_lon_to_x``/``_lat_to_y`` are world pixels:
        # using them directly put every vertex tens of thousands of pixels
        # outside the item, which drew nothing at all.
        xs, ys = batch(points[:, 1], points[:, 0])
        return np.column_stack(
            (np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64))
        )

    return project


def assemble_scene(
    plan,
    viewport,
    scene_cache,
    palette,
    *,
    cancelled=None,
    mesh_cache=None,
    on_cell=None,
    workers=None,
    mesh_scale=None,
):
    """Assemble ``plan`` for ``viewport``; ``None`` when cancelled.

    Kept as a module function so the assembly can be exercised without a
    worker thread or a GUI.  ``mesh_cache`` is the worker-owned
    :class:`sailworks.rendering.encview.MeshCache`; ``None`` disables reuse.
    ``on_cell(snapshot, done, total)`` is passed through to
    :func:`encview.build_view_scene` for progressive publication; returning
    ``False`` from it refuses progress and yields ``None``.  ``workers`` is
    the per-cell mesh concurrency bound, also passed through; ``None`` uses
    :func:`encview._default_workers`.  ``mesh_scale`` is the fixed mesh frame
    from :func:`mesh_scale_for`; production supplies the identity factor, so
    the mesh stays in the view's own frame (see the call site for the
    measurement that rejected the reference frame).  The meshes are in source
    degrees, so the frame needs no view origin: what the factor encodes is the
    reference zoom's tolerance and stroke width, not a position.
    """
    return encview.build_view_scene(
        plan,
        scene_cache,
        projection=projection_for(viewport),
        stroke_pixels=STROKE_PIXELS,
        palette=palette,
        cancelled=cancelled,
        bounding=viewport,
        mesh_cache=mesh_cache,
        on_cell=on_cell,
        workers=workers,
        mesh_scale=mesh_scale,
    )


def _scene_fields(scene):
    """The measurements every publication reports for one scene."""
    return {
        "cells": len(scene.cells),
        "vertices": scene.vertex_count,
        "triangles": scene.triangle_count,
        "skipped": tuple((cell.path, cell.reason) for cell in scene.skipped),
        "unsupported": len(scene.unsupported),
        "accounted_bytes": scene.accounted_bytes,
        # A separate, bounded tessellation budget; hits are reuse, not redraws.
        "mesh_cache_bytes": scene.mesh_bytes,
        "mesh_cache_hits": scene.mesh_hits,
        "mesh_cache_misses": scene.mesh_misses,
        "mesh_cache_built": scene.mesh_built,
    }


def scene_report(scene):
    """``(state, report)`` for one assembled scene.

    ``unavailable`` is explicit: nothing is presented when the scene drew no
    geometry, and a partially assembled scene never claims completeness.
    """
    if scene is None:
        return STATE_UNAVAILABLE, {
            "complete": False,
            "reason": "scene assembly returned no result",
            "incomplete": INCOMPLETE_LABELS,
        }
    report = _scene_fields(scene)
    # Never claim a complete chart while labels and symbols are missing.
    report["complete"] = False
    report["incomplete"] = INCOMPLETE_LABELS
    if scene.is_empty:
        report["reason"] = "no ENC geometry for this view"
        return STATE_UNAVAILABLE, report
    return STATE_READY, report


class EncSceneLayer:
    """The viewport's opt-in GPU ENC scene: state, generation and worker.

    Requested from the GUI thread and published on the GUI thread; the plan
    and the mesh assembly run on this layer's own worker.  It yields to the
    weather pipeline through ``worker_idle`` and it never claims the field
    worker.
    """

    def __init__(
        self,
        data_dir,
        palette,
        worker_idle=None,
        current_camera=None,
        on_publish=None,
        on_state=None,
        dpr=1,
        catalog_factory=None,
    ):
        self._data_dir = Path(data_dir)
        self._palette = palette
        # The ENC catalog is the caller's: ``catalog_factory(data_dir,
        # palette)`` returns the source object this layer refreshes and reads
        # (``refresh()``, ``manifest.class_enabled`` and ``sources``).  The
        # layer itself never reaches for the viewport's raster ENC source, so
        # it stays importable and testable without it.
        self._catalog_factory = catalog_factory
        self._worker_idle = worker_idle or (lambda: True)
        self._current_camera = current_camera
        self._on_publish = on_publish
        self._on_state = on_state
        self._condition = Condition()
        self._pending = None
        self._active = None
        self._generation = 0
        self._published = None
        self._state = STATE_IDLE
        self._error = ""
        self._incomplete = ""
        self._thread = None
        self._closed = False
        # Worker-owned; created on the worker thread and released with it.
        self._catalog = None
        self._scene_cache = None
        self._mesh_cache = None
        # The device pixel ratio only invalidates a published scene; the
        # projection stays in logical pixels.
        self._dpr = max(1, int(round(float(dpr))))
        self.counters = dict(
            requests=0,
            reuses=0,
            assemblies=0,
            publications=0,
            partial_publications=0,
            superseded_rejected=0,
            cancelled=0,
            failures=0,
            yielded=0,
            cells=0,
            skipped=0,
            unsupported=0,
        )
        #: Cells resolved by the published scene, and the planned total.  A
        #: partial publication has ``done < total``; the final one has
        #: ``done == total``.
        self._progress_done = 0
        self._progress_total = 0

    # ------------------------------------------------------------- state
    @property
    def state(self):
        with self._condition:
            return self._state

    @property
    def error(self):
        with self._condition:
            return self._error

    @property
    def incomplete(self):
        """The explicit incompleteness marker of the published scene."""
        with self._condition:
            return self._incomplete

    @property
    def published(self):
        with self._condition:
            return self._published

    @property
    def _progress(self):
        """``(done, total)`` cells of the published scene.

        ``done < total`` means the published scene is a partial one; the final
        publication sets ``done == total``.
        """
        with self._condition:
            return (self._progress_done, self._progress_total)

    def observations(self):
        with self._condition:
            return {
                "state": self._state,
                "error": self._error,
                "incomplete": self._incomplete,
                "generation": self._generation,
                "progress": (self._progress_done, self._progress_total),
                "partial_publications": self.counters["partial_publications"],
                "counters": dict(self.counters),
            }

    # ----------------------------------------------------------- requests
    def request(self, camera, replace=False):
        """Ask for a scene at this camera (GUI thread).

        A published scene that still describes this camera, forecast instant
        and pixel ratio is reused.  Only the pending slot is replaced; active
        work finishes and is rejected if superseded.
        """
        with self._condition:
            if self._closed:
                return None
            reused = self._reuse(camera, replace)
            if reused is not None:
                return reused
            self._generation += 1
            generation = self._generation
            if self._pending is not None:
                self._pending[2].set()
                self.counters["cancelled"] += 1
            self._pending = (generation, camera, Event())
            self._state = STATE_LOADING
            self._error = ""
            self._incomplete = ""
            self._progress_done = 0
            self._progress_total = 0
            self.counters["requests"] += 1
            if self._thread is None:
                self._thread = Thread(
                    target=self._run, name="grib-viewport-enc-scene", daemon=True
                )
                self._thread.start()
        self._notify_state(STATE_LOADING)
        with self._condition:
            self._condition.notify_all()
        return generation

    def _reuse(self, camera, replace):
        if replace:
            return None
        if self._pending is not None:
            if self._pending[0] == self._generation and self._pending[1] == camera:
                return self._pending[0]
            return None
        if (
            self._active is not None
            and self._active[0] == self._generation
            and self._active[1] == camera
        ):
            return self._active[0]
        if self._published is not None and self._reusable(camera):
            self.counters["reuses"] += 1
            return self._published[0]
        return None

    def _reusable(self, camera):
        """True only for a ready scene that still describes this camera.

        A partial publication leaves the layer ``loading``: it is progress so
        far, never the current view, so it must not be reused as if it were the
        finished scene.
        """
        if self._state != STATE_READY or self._published is None:
            return False
        _generation, _scene, metadata = self._published
        if metadata.get("time") != camera.stamp():
            return False
        if metadata.get("dpr", 1) != self._dpr:
            return False
        viewport = metadata.get("camera")
        return _same_view(viewport, camera)

    def set_device_pixel_ratio(self, dpr):
        """Adopt a new ratio; the published scene is withdrawn."""
        factor = max(1, int(round(float(dpr))))
        notify = None
        with self._condition:
            if factor == self._dpr:
                return False
            self._dpr = factor
            was_published = self._published is not None
            self._published = None
            self._incomplete = ""
            self._progress_done = 0
            self._progress_total = 0
            if was_published and self._state == STATE_READY:
                # Nothing is published any more, so READY would be a stale
                # claim; the next request composes at the new ratio.
                self._state = STATE_IDLE
                notify = STATE_IDLE
        if notify is not None:
            self._notify_state(notify)
        return True

    def close(self):
        """Stop the worker and release anything it owns."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._published = None
            self._incomplete = ""
            self._progress_done = 0
            self._progress_total = 0
            for request in (self._pending, self._active):
                if request is not None:
                    request[2].set()
                    self.counters["cancelled"] += 1
            self._pending = None
            self._condition.notify_all()

    # ------------------------------------------------------------ worker
    def _notify_state(self, state):
        if self._on_state is not None:
            self._on_state(state)

    def _notify_publish(self, payload):
        if self._on_publish is not None:
            self._on_publish(payload)

    # -------------------------------------------------------- publication
    def _publish_partial(self, generation, scene, done, total, cancel, metadata=None):
        """Publish the resolved subset of an in-flight generation.

        Runs on the worker, called from ``build_view_scene``'s progress
        callback.  Returning ``False`` refuses further progress, which is what
        stops assembly, and it is returned exactly when the request was
        cancelled, superseded or the layer was closed.  That check and the
        store share the lock the GUI thread uses, so a superseded partial can
        never overwrite a newer publication.

        The stored scene is explicitly incomplete: ``complete`` is ``False``
        and ``incomplete`` names the partial cells.  The layer stays
        ``loading``; the final publication replaces this scene as ``ready``
        with ``done == total``.  The GUI is notified through ``on_publish``
        only: ``loading`` was already announced by :meth:`request`, and the
        payload itself carries what has been drawn so far.
        """
        if done >= total:
            # Not a partial: the finished scene is published as READY when the
            # assembly returns, and a same-content partial would only be
            # replaced immediately.
            return True
        with self._condition:
            if cancel.is_set() or self._closed or generation != self._generation:
                return False
            published = dict(metadata) if metadata is not None else {}
            published.update(_scene_fields(scene))
            published.update(
                {
                    "complete": False,
                    "incomplete": partial_incompleteness(done, total),
                    "partial": True,
                    "progress": (done, total),
                }
            )
            self._published = (generation, scene, published)
            self._state = STATE_LOADING
            self._error = ""
            self._incomplete = published["incomplete"]
            self._progress_done = done
            self._progress_total = total
            self.counters["partial_publications"] += 1
            payload = self._published
        self._notify_publish(payload)
        return True

    def _run(self):
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    request = self._pending
                    self._pending = None
                    self._active = request
                generation, camera, cancel = request
                began = time.perf_counter()
                error = ""
                scene = None
                metadata = None
                yielded = False
                withdraw = False
                try:
                    if not cancel.is_set():
                        if not self._worker_idle():
                            yielded = True
                        else:
                            scene, metadata = self._compose(camera, cancel)
                except Exception as exc:  # a scene failure is never fatal
                    error = str(exc)
                ended = time.perf_counter()
                publish = None
                notify_state = None
                with self._condition:
                    self._active = None
                    if (
                        yielded
                        and not cancel.is_set()
                        and generation == self._generation
                    ):
                        # The weather pipeline has priority: ask again later.
                        self._state = STATE_IDLE
                        notify_state = STATE_IDLE
                        self.counters["yielded"] += 1
                    elif (
                        cancel.is_set()
                        or generation != self._generation
                        or self._closed
                    ):
                        self.counters["superseded_rejected"] += 1
                        # This generation may already have published a partial
                        # scene.  It is no longer current, so withdraw it: the
                        # GUI must not keep content the next request is about
                        # to replace.
                        stale = self._published
                        if stale is not None and stale[0] == generation:
                            self._published = None
                            self._incomplete = ""
                            self._progress_done = 0
                            self._progress_total = 0
                            withdraw = not self._closed
                    elif scene is not None and metadata is not None:
                        state, report = scene_report(scene)
                        metadata = dict(metadata)
                        metadata.update(report)
                        metadata.setdefault("timings", {})["assembly_ms"] = round(
                            (ended - began) * 1000.0, 3
                        )
                        if state == STATE_READY:
                            self.counters["assemblies"] += 1
                            self.counters["publications"] += 1
                            self.counters["cells"] += report["cells"]
                            self.counters["skipped"] += len(report["skipped"])
                            self.counters["unsupported"] += report["unsupported"]
                            self._published = (generation, scene, metadata)
                            self._state = STATE_READY
                            self._incomplete = INCOMPLETE_LABELS
                            # The finished scene resolved every planned cell,
                            # which is what distinguishes it from a partial.
                            total = metadata.get("planned_cells", report["cells"])
                            self._progress_done = total
                            self._progress_total = total
                            publish = self._published
                        else:
                            self.counters["failures"] += 1
                            self._published = None
                            self._incomplete = ""
                            self._progress_done = 0
                            self._progress_total = 0
                            self._state = STATE_UNAVAILABLE
                            self._error = report.get("reason", "")
                            notify_state = STATE_UNAVAILABLE
                    else:
                        self.counters["failures"] += 1
                        self._published = None
                        self._incomplete = ""
                        self._progress_done = 0
                        self._progress_total = 0
                        self._state = STATE_UNAVAILABLE
                        # Keep the composition's own explanation: it is the
                        # difference between "ENC charts are disabled" and a
                        # generic failure.
                        reason = metadata.get("reason") if metadata else None
                        self._error = error or reason or "ENC scene is unavailable"
                        notify_state = STATE_UNAVAILABLE
                    if len(self.counters):
                        pass
                if publish is not None:
                    with self._condition:
                        if self._closed:
                            publish = None
                    if publish is not None:
                        self._notify_publish(publish)
                        self._notify_state(STATE_READY)
                elif withdraw:
                    self._notify_publish(None)
                elif notify_state is not None:
                    self._notify_state(notify_state)
        finally:
            self._release()

    # --------------------------------------------------------- composition
    def _compose(self, camera, cancel):
        """Plan and assemble the cells for ``camera`` on the worker."""
        from sailworks.core.mapviewport import MapViewport
        from sailworks.rendering.overlayrenderer import MARGIN
        from sailworks.qt.gribviewportchart import ChartCamera

        if self._catalog is None:
            if self._catalog_factory is None:
                raise RuntimeError("The ENC scene layer has no catalog source")
            # Created on the worker thread, like the scene cache below, and
            # released with it.
            self._catalog = self._catalog_factory(self._data_dir, self._palette)
        if self._scene_cache is None:
            self._scene_cache = encscene.SceneCache(SCENE_CACHE_BYTES)
        if self._mesh_cache is None:
            self._mesh_cache = encview.MeshCache(MESH_CACHE_BYTES)
        # Logical pixels: the item lives in QQuickItem coordinates and Qt
        # applies the device pixel ratio itself.
        # The same margin the raster composition uses: planning against the
        # bare view drops a cell whose envelope only reaches the padded area,
        # and the chart then loses that cell's features entirely.
        viewport = MapViewport(ChartCamera(camera, 1), MARGIN, lambda: None)
        viewport.render_cancelled = cancel.is_set
        metadata = {
            "camera": camera,
            "time": camera.stamp(),
            "dpr": self._dpr,
            "palette": self._palette,
            # The scene carries lon/lat positions; the item must project them
            # with the same view that produced this scene.  Without it the
            # item drew degrees, i.e. a chart at (-124, 48) and nothing on
            # screen.
            "projection": projection_for(viewport),
        }
        # The generation this composition belongs to: a newer request bumps the
        # layer's generation and the progress callback then refuses to publish.
        with self._condition:
            generation = self._generation
        self._catalog.refresh()
        manifest = self._catalog.manifest
        if not manifest.class_enabled:
            metadata["reason"] = "ENC charts are disabled"
            return None, metadata
        usable = tuple(
            source.descriptor for source in self._catalog.sources if not source.error
        )
        if not usable:
            metadata["reason"] = "no readable ENC cell is enabled"
            return None, metadata
        if cancel.is_set():
            return None, metadata
        plan = encplan.plan_cells(viewport, usable, cancelled=cancel.is_set)
        if plan is None or cancel.is_set():
            return None, metadata
        metadata["planned_cells"] = len(plan)
        if not plan:
            metadata["reason"] = "no ENC cell covers this view"
            return None, metadata
        scene = assemble_scene(
            plan,
            viewport,
            self._scene_cache,
            self._palette,
            cancelled=cancel.is_set,
            mesh_cache=self._mesh_cache,
            # The mesh frame is the view's own zoom: the geometry is in source
            # degrees, so it needs no origin, and the scalar only fixes the
            # tolerance and the stroke width.  Meshing at MESH_REFERENCE_ZOOM
            # instead is measurably worse - 190,689 vertices and 600 ms of
            # assembly at zoom 14.2, against 26,004 and 213 ms here - and only
            # holds stroke parity at the reference zoom itself, so production
            # passes the identity factor.  Pan reuse comes from the granule,
            # not from a coarser frame.
            mesh_scale=1.0,
            on_cell=lambda snapshot, done, total: self._publish_partial(
                generation, snapshot, done, total, cancel, metadata
            ),
        )
        if scene is None:
            return None, metadata
        return scene, metadata

    def _release(self):
        """Release worker-owned handles; runs on the worker thread."""
        self._catalog = None
        self._scene_cache = None
        self._mesh_cache = None


def _same_view(left, right):
    if left is None or right is None:
        return False
    return (
        left.center_lon,
        left.center_lat,
        left.zoom,
        left.width,
        left.height,
    ) == (
        right.center_lon,
        right.center_lat,
        right.zoom,
        right.width,
        right.height,
    )
