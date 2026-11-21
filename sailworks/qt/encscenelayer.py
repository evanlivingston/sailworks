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

import os
import time
from pathlib import Path
from threading import Condition, Event, Thread

import numpy as np
import shapely

from sailworks.core.enccharts import chart_scale_rank, display_scale_denominator
from sailworks.qt import enc_trace
from sailworks.rendering import encplan, encview
from sailworks.rendering.vectordrawer import encscene, encscenestore

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

#: Complete publications kept for motion fallback and return pans, oldest
#: first.  The newest is also ``_published``; the others are the frames a
#: recent camera move drew.  Three covers the A-B-A and A-B-C-A return legs a
#: pan produces while staying a bounded set of resident scenes.
RETAINED_SCENES = 3

#: Byte budget for the retained publications.  A scene's vertex payload is
#: what actually costs memory; the count cap above normally binds first, and
#: this only guards an unusually dense view.  It is a separate budget from
#: the per-cell mesh cache above, owned by this worker only.
RETAINED_SCENE_BYTES = SCENE_CACHE_BYTES


def _retained_scene_bytes(entries):
    """The resident vertex payload of retained publications, oldest first.

    A coarse guard: only each scene's own ``positions`` array is counted,
    which is the dominant term; colours and labels scale with it.
    """
    total = 0
    for _generation, scene, _metadata in entries:
        positions = getattr(scene, "positions", None)
        if positions is not None:
            total += int(positions.nbytes)
    return total


#: Bounded per-cell mesh cache, a separate budget owned by this worker only.
#: A warm view change reuses these meshes instead of re-tessellating.  It must
#: hold one view's resident cells: each mesh is clipped to its cell's own
#: coverage, so it is larger than the visible slice.
MESH_CACHE_BYTES = 256 * 1024 * 1024

#: Bounded on-disk store of built scenes, keyed by cell identity and schema.
#: A cell is ~12.7 MB and ~309 ms to build; 256 MiB holds about twenty cells,
#: so the realistic second launch reads the two cells it plans instead of
#: re-parsing them.
SCENE_STORE_BYTES = 256 * 1024 * 1024

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


def screen_viewport_for(camera):
    """The viewport the published projection describes: the item, unpadded.

    Planning and meshing need :data:`sailworks.rendering.overlayrenderer.MARGIN`
    (a cell whose envelope only reaches the padded area still belongs to the
    view), but the QML item covers the camera view alone.  Building the
    published projection from a padded viewport put every vertex ``MARGIN``
    logical pixels right and down of where it belonged, so the drawing is
    projected from this item-sized viewport while planning keeps its margin.
    """
    from sailworks.core.mapviewport import MapViewport
    from sailworks.qt.gribviewportchart import ChartCamera

    return MapViewport(ChartCamera(camera, 1), 0, lambda: None)


def _view_key(camera):
    """The camera fields a projection depends on."""
    return (
        float(camera.center_lon),
        float(camera.center_lat),
        float(camera.zoom),
        float(camera.width),
        float(camera.height),
    )


def _stored_loader(store, timings=None):
    """A cell loader that reads the persisted store, building on a miss.

    The store is a cache: a decode failure or an IO error is a miss and the
    cell is built from GDAL exactly as before, so it can never break a scene.
    ``timings`` is the opt-in diagnostic mapping from :meth:`EncSceneLayer._compose`;
    the store and a fresh OGR build report their own stages into it, so a
    persisted hit and an OGR rebuild are distinguishable.
    """

    def loader(path, version, cancelled=None):
        return store.load_or_build(
            path,
            version,
            lambda: encscene.open_cell_scene(path, version, cancelled, timings=timings),
            timings=timings,
        )

    return loader


def _cache_counters(mesh_cache, scene_cache, scene_store):
    """Snapshot the residency counters of the worker-owned caches.

    Pure instrumentation: the caches are created on the worker thread, so any
    of them may still be ``None`` before the first composition.  The counters
    are plain ints written by the worker; reading them without a lock can at
    worst mix counts from two adjacent updates, which is acceptable for a
    diagnostic snapshot and never touches the GUI thread's lock ordering.
    """

    def counts(cache):
        if cache is None:
            return 0, 0, 0
        return (
            int(getattr(cache, "hits", 0) or 0),
            int(getattr(cache, "misses", 0) or 0),
            int(getattr(cache, "triangulations", 0) or 0),
        )

    mesh_hits, mesh_misses, mesh_triangulations = counts(mesh_cache)
    scene_hits, scene_misses, _ = counts(scene_cache)
    store_hits, store_misses, _ = counts(scene_store)
    return {
        "mesh_triangulations": mesh_triangulations,
        "mesh_hits": mesh_hits,
        "mesh_misses": mesh_misses,
        "scene_hits": scene_hits,
        "scene_misses": scene_misses,
        "store_hits": store_hits,
        "store_misses": store_misses,
    }


def display_scale_for(viewport):
    """The camera's scale denominator, or ``None`` when it cannot state one."""
    get_scale = getattr(viewport, "get_scale", None)
    try:
        # ``None`` for a viewport that cannot state its scale: the SCAMIN gate
        # is then off rather than wrong.
        return display_scale_denominator(get_scale()) if callable(get_scale) else None
    except (TypeError, ValueError):
        return None


def _view_granule(plan, viewport):
    """The current view's granule and step, shared with neighbour prefetch."""
    region = encview._view_region(plan, viewport)
    step = encview._absolute_granule_step(region)
    return (
        encview._granule_region(region, step=step) if step is not None else None,
        step,
    )


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
    loader=encscene.open_cell_scene,
    timings=None,
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

    ``loader(path, version, cancelled)`` supplies a cell on a scene-cache miss.
    Production passes one that reads the persisted scene store, so a cell built
    by an earlier process is not parsed again; ``None`` keeps the in-memory
    cache only.  ``timings`` is the opt-in diagnostic mapping forwarded to
    :func:`encview.build_view_scene`, so each assembly stage (including the
    store/OGR split inside ``cell_open``) is attributed.
    """
    display_scale = display_scale_for(viewport)
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
        loader=loader,
        timings=timings,
        display_scale=display_scale,
    )


def cell_identity(cell):
    """The ordered identity of one planned cell, as metadata records it."""
    path = os.fspath(cell.path)
    return {
        "path": path,
        "name": Path(path).name,
        "rank_key": tuple(cell.rank_key),
        "version": None if cell.version is None else tuple(cell.version),
        "coverage_known": bool(cell.coverage_known),
    }


def cell_states(planned, skipped, done=None):
    """Explicit per-cell state, in plan order.

    A planned cell the report does not name as skipped contributed geometry and
    is ``ready``.  On a partial publication ``done`` is the number of resolved
    cells, so the rest are ``loading``: a cell that is not drawn yet is never
    silently absent.  A cell the plan itself could not read is appended to the
    plan's order with its own reason, so it is reported by that reason even
    while the rest of the scene is still resolving.
    """
    reasons = {os.path.abspath(os.fspath(path)): reason for path, reason in skipped}
    states = []
    for index, record in enumerate(planned):
        path = record["path"]
        reason = reasons.get(os.path.abspath(path))
        if reason is None and done is not None and index >= done:
            state = STATE_LOADING
        elif reason is None:
            state = STATE_READY
        elif reason == "oversized":
            state = "oversized"
        elif reason.startswith("error:"):
            state = "error"
        else:
            state = STATE_UNAVAILABLE
        states.append(
            {
                "path": path,
                "name": record.get("name", os.path.basename(path)),
                "state": state,
                "reason": reason or "",
            }
        )
    return tuple(states)


def plan_unavailable(metadata):
    """The composition's unreadable planned cells as ``(path, reason)`` pairs.

    ``plan_cells`` records a cell it could not read here instead of dropping
    it silently; merging these into the scene's own ``skipped`` entries is
    what makes :func:`cell_states` report it as ``unavailable`` with its
    reason on every publication, partial or final.
    """
    if not metadata:
        return ()
    return tuple(tuple(entry) for entry in metadata.get("unavailable", ()))


def unavailable_clips(plan, scene):
    """The clip of each planned cell the assembly refused to draw.

    A cell the cache refused (``oversized``) was still planned, so its
    :class:`encplan.CellPlan` holds the ground no better-ranked drawn cell
    claims.  Publishing that already-trimmed clip lets a caller paint a
    fallback for exactly that ground instead of the cell's whole coverage.
    A cell the plan itself could not read has no ``CellPlan`` and so is not
    listed here; it stays in ``metadata["unavailable"]`` alone.

    The clip travels as WKB bytes: the module hands the GUI thread no shapely
    or OGR object (see the module docstring), and a Cairo caller converts it
    with ``ogr.CreateGeometryFromWkb``.
    """
    if scene is None or not plan:
        return ()
    planned = {}
    for cell in plan:
        key = _clip_path_key(cell.path)
        if key is not None:
            planned[key] = cell
    entries = []
    for skipped in scene.skipped:
        cell = planned.get(_clip_path_key(skipped.path))
        if cell is None or cell.clip is None:
            continue
        try:
            clip_wkb = shapely.to_wkb(cell.clip)
        except Exception:
            # One unencodable clip must not cost the other refused cells theirs.
            continue
        path = os.fspath(cell.path)
        entries.append(
            {
                "path": path,
                "name": os.path.basename(path),
                "reason": skipped.reason,
                "clip_wkb": clip_wkb,
            }
        )
    return tuple(entries)


def _clip_path_key(value):
    """A path usable as a plan/skip match key, or ``None`` when malformed."""
    try:
        return os.path.abspath(os.fspath(value))
    except TypeError:
        return None


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
        cache_dir=None,
        yield_budget_ms=None,
    ):
        self._data_dir = Path(data_dir)
        # The persisted scene store lives in the app's cache directory, never
        # in the repo or beside the fixture: co-located with the app's own data
        # so it is cleared with it, and bounded by ``SCENE_STORE_BYTES``.
        self._cache_dir = (
            Path(cache_dir) if cache_dir is not None else self._data_dir / "cache"
        )
        self._palette = palette
        # The ENC catalog is the caller's: ``catalog_factory(data_dir,
        # palette)`` returns the source object this layer refreshes and reads
        # (``refresh()``, ``manifest.class_enabled`` and ``sources``).  The
        # layer itself never reaches for the viewport's raster ENC source, so
        # it stays importable and testable without it.
        self._catalog_factory = catalog_factory
        self._worker_idle = worker_idle or (lambda: True)
        #: How long a generation may defer to the weather pipeline before it
        #: composes anyway.  ``None`` keeps the unbounded courtesy a caller with
        #: a real raster competitor relies on; the viewport's scene mode bounds
        #: it, because there the scene *is* the chart and waiting on the raster
        #: worker starves it until the next chart-source poll.
        self._yield_budget = (
            None
            if yield_budget_ms is None
            else max(0.0, float(yield_budget_ms) / 1000.0)
        )
        #: When the current demand was made, so the yield budget is measured
        #: from the request and not from a worker attempt: a retried generation
        #: must not be able to restart the courtesy.
        self._demand_at = None
        self._current_camera = current_camera
        self._on_publish = on_publish
        self._on_state = on_state
        self._condition = Condition()
        self._pending = None
        self._active = None
        #: The payload a retained-scene promotion must publish once
        #: ``request`` leaves the lock (see :meth:`_reuse`).
        self._notify_after_lock = None
        #: The demand the worker yielded for weather priority.  It stays open:
        #: ``resume`` re-posts this same generation instead of minting a newer
        #: one that would supersede the work the retry was meant to continue.
        self._yielded = None
        self._generation = 0
        self._published = None
        #: Complete publications kept across camera changes, oldest first and
        #: capped at :data:`RETAINED_SCENES` entries.  The newest is presented
        #: while its replacement assembles; a return pan into ground a
        #: retained frame already covers is answered from that frame instead
        #: of recomposing it (see :meth:`_reuse`).
        self._retained_ready = []
        #: The newest complete publication: the frame motion falls back to
        #: while its replacement assembles, and the frame a partial must not
        #: displace.  Kept separate from the return-pan cache above, which is
        #: byte-capped and may also hold older scenes.
        self._last_ready = None
        self._state = STATE_IDLE
        self._error = ""
        self._incomplete = ""
        self._thread = None
        self._closed = False
        # Worker-owned; created on the worker thread and released with it.
        self._catalog = None
        #: The palette the worker-owned catalog was built with, so a palette
        #: change recreates it on the worker instead of reading stale colours.
        self._catalog_palette = None
        self._scene_cache = None
        self._scene_store = None
        self._mesh_cache = None
        # ``(view key, projection)`` for the last published projection, so a
        # composition at an unchanged camera hands the item the same callable
        # and does not invalidate geometry that is already correct.
        self._screen_projection = None
        # The device pixel ratio only invalidates a published scene; the
        # projection stays in logical pixels.
        self._dpr = max(1, int(round(float(dpr))))
        #: Monotonic identity of the cell content a demand would read.  The
        #: manifest's own stamp (a stat of the user's settings file) is the
        #: cheap signal that installed cells, their enabled state or the ENC
        #: class flag may have changed; a palette replacement and a catalog
        #: refresh that observes a new manifest generation bump the same
        #: counter.  The viewport's demand key carries it, so a stationary
        #: camera still asks again when new cells arrive.
        self._content_generation = 0
        self._content_stamp = None
        self._content_fingerprint = None
        #: The queued neighbour prefetch: ``(cancel, camera)``, or ``None``.
        #: One at a time, and dropped the moment a real demand arrives.
        self._prefetch = None
        #: What the last current composition built, so the prefetch can warm
        #: the ground beside it: ``camera``, ``plan``, ``viewport``,
        #: ``projection``, ``display_scale``, and the ``travel`` vector since
        #: the composition before it.
        self._prefetch_basis = None
        self.counters = dict(
            requests=0,
            reuses=0,
            retained_reuses=0,
            assemblies=0,
            publications=0,
            partial_publications=0,
            superseded_rejected=0,
            cancelled=0,
            prefetch_runs=0,
            prefetch_cancelled=0,
            failures=0,
            clip_failures=0,
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
        """The scene callers may present.

        While the layer is ``ready`` this is the current complete publication.
        A camera change keeps the last complete scene so the chart is not
        blanked while its replacement assembles; that retained scene is
        returned with ``stale``/``complete`` markers so a caller can tell it
        describes an older camera.  A partial publication is never returned
        over a retained complete scene.
        """
        with self._condition:
            if self._state == STATE_READY:
                return self._published
            retained = self._last_ready
            if retained is None:
                return self._published
            generation, scene, metadata = retained
            metadata = dict(metadata) if metadata is not None else {}
            metadata["stale"] = True
            metadata["complete"] = True
            return (generation, scene, metadata)

    def _presented(self):
        """The published scene, or the retained complete one (lock held)."""
        if self._state == STATE_READY and self._published is not None:
            return self._published
        if self._last_ready is not None:
            return self._last_ready
        return self._published

    @property
    def cells_order(self):
        """The published scene's ordered cell identity records.

        One record per planned cell, best-ranked first: ``path``, ``name``,
        ``rank_key``, ``version`` and ``coverage_known``.  Empty until a
        composition has planned cells.
        """
        with self._condition:
            presented = self._presented()
            if presented is None:
                return ()
            return tuple(presented[2].get("cells_order", ()))

    @property
    def cell_states(self):
        """The published scene's per-cell state, in the same plan order.

        A skipped or not-yet-resolved cell is named explicitly (``unavailable``,
        ``oversized``, ``error`` or ``loading``) with its reason, so a missing
        cell is visible to the UI instead of silently absent.
        """
        with self._condition:
            presented = self._presented()
            if presented is None:
                return ()
            return tuple(presented[2].get("cell_states", ()))

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
                "cells_order": self.cells_order,
                "cell_states": self.cell_states,
                "partial_publications": self.counters["partial_publications"],
                "counters": dict(self.counters),
                **_cache_counters(
                    self._mesh_cache, self._scene_cache, self._scene_store
                ),
            }

    # ----------------------------------------------------------- requests
    @property
    def content_generation(self):
        """Identity of the cell content the next request would read.

        The user's settings file lists the installed cells, their enabled
        state and the ENC class flag, so its own stamp is the cheap signal
        that the effective cell set may have changed.  Observing a new stamp
        bumps the monotonic counter; the settings file is not parsed here.
        A palette replacement and a catalog refresh that sees a new manifest
        generation bump the same counter (``set_palette``/``_note_content``).
        """
        from sailworks.qt.viewportenc import manifest_stamp

        stamp = manifest_stamp(self._data_dir)
        with self._condition:
            if self._content_stamp is None:
                self._content_stamp = stamp
            elif stamp != self._content_stamp:
                self._content_stamp = stamp
                self._content_generation += 1
            return self._content_generation

    @property
    def cells_key(self):
        """Cheap identity of the cell set and portrayal a demand reads.

        A request carries only a camera; the cells come from the catalog, so
        the content generation covers installed/enabled cells, class toggles
        and palette replacements.  The data directory stays in the key for a
        layer that is ever pointed at another one.
        """
        return (str(self._data_dir), self.content_generation)

    def _note_content(self, manifest):
        """Bump the content generation when the catalog's cells changed.

        Runs on the worker after ``catalog.refresh()``: the manifest's
        generation covers the installed cells' identities and their enabled
        state, so a downloaded or replaced cell is observable here even when
        the settings file's own stamp did not change.  The first observed
        content is the baseline; it does not bump, so startup asks once.
        """
        fingerprint = (
            getattr(manifest, "generation", None),
            getattr(manifest, "class_enabled", True),
        )
        with self._condition:
            previous = self._content_fingerprint
            self._content_fingerprint = fingerprint
            if previous is not None and fingerprint != previous:
                self._content_generation += 1

    def request(self, camera, replace=False):
        """Ask for a scene at this camera (GUI thread).

        A published scene that still describes this camera, forecast instant
        and pixel ratio is reused; so is a retained complete scene whose
        composed ring covers this camera (see :meth:`_retained_covering`).
        Only the pending slot is replaced; active work finishes and is
        rejected if superseded.
        """
        notify_publish = None
        with self._condition:
            if self._closed:
                return None
            reused = self._reuse(camera, replace)
            if reused is not None:
                # A retained-scene promotion publishes the re-adopted payload
                # after the lock: the presentation must reach the viewport,
                # which an exact reuse never needs (it already shows it).
                notify_publish = self._notify_after_lock
                self._notify_after_lock = None
                if notify_publish is not None:
                    # The promotion answers this camera, so an older demand
                    # still outstanding was made for the camera the promoted
                    # frame replaces.  Supersede it exactly as a fresh demand
                    # would: a stale completion must not publish an older
                    # generation after the promotion minted a newer one.
                    self._yielded = None
                    for slot in (self._pending, self._active):
                        if slot is not None and len(slot) > 2:
                            slot[2].set()
                            self.counters["cancelled"] += 1
                    if self._prefetch is not None:
                        self._prefetch[0].set()
                        self._prefetch = None
                        self.counters["prefetch_cancelled"] += 1
            else:
                # A fresh demand supersedes any demand left open by a yield.
                self._yielded = None
                self._generation += 1
                generation = self._generation
                # Only a fresh demand restarts the courtesy clock.  A retry of
                # an outstanding (yielded) demand must keep the original
                # instant, or resetting it here would make the yield budget
                # unreachable: every retry would look like a brand-new demand
                # and defer forever.
                if self._state == STATE_READY or self._demand_at is None:
                    self._demand_at = time.perf_counter()
                if self._pending is not None:
                    self._pending[2].set()
                    self.counters["cancelled"] += 1
                # A real request always wins: anything the idle prefetch
                # holds is dropped here, and its worker re-checks the event
                # between cells.
                if self._prefetch is not None:
                    self._prefetch[0].set()
                    self._prefetch = None
                    self.counters["prefetch_cancelled"] += 1
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
        if reused is not None:
            if notify_publish is not None:
                self._notify_publish(notify_publish)
                self._notify_state(STATE_READY)
                if enc_trace.enabled():
                    enc_trace.event(
                        "scene_retained_reuse",
                        generation=reused,
                        **enc_trace.camera_fields(camera),
                    )
            return reused
        self._notify_state(STATE_LOADING)
        with self._condition:
            self._condition.notify_all()
        return generation

    def resume(self):
        """Re-post the demand the worker yielded; never mint a generation.

        A yield returns the worker to its loop because the weather pipeline
        has priority, not because the scene was refused.  The same request
        tuple goes back into the pending slot, so the retry continues the
        outstanding generation instead of superseding it.  Returns that
        generation, or ``None`` when nothing is open to resume.
        """
        notify = None
        with self._condition:
            if self._closed or self._yielded is None:
                return None
            request = self._yielded
            if request[0] != self._generation:
                self._yielded = None
                return None
            if self._pending is not None:
                # A newer demand already owns the slot; nothing to re-post.
                return self._pending[0]
            self._yielded = None
            self._pending = request
            if self._state != STATE_LOADING:
                self._state = STATE_LOADING
                notify = STATE_LOADING
            self._condition.notify_all()
        if notify is not None:
            self._notify_state(notify)
        return request[0]

    # ------------------------------------------------------------ prefetch
    def prefetch(self, camera=None):
        """Queue a neighbour-granule warm-up behind the current demand.

        GUI thread.  Best effort by construction: it only starts from a
        composed, covered view, sits behind any pending demand, and is dropped
        by :meth:`request` the moment a real one arrives.  Returns whether a
        job was queued.
        """
        with self._condition:
            if self._closed or self._state != STATE_READY:
                return False
            if self._pending is not None or self._prefetch is not None:
                return False
            basis = self._prefetch_basis
            if basis is None or camera != basis["camera"]:
                return False
            self._prefetch = (Event(), camera)
            self._condition.notify_all()
        return True

    def cancel_prefetch(self):
        """Drop a queued or running prefetch: a real demand always wins."""
        with self._condition:
            if self._prefetch is None:
                return False
            self._prefetch[0].set()
            self._prefetch = None
            self.counters["prefetch_cancelled"] += 1
        return True

    def _prefetch_run(self, cancel, camera):
        """Warm the granule beside the last composition (worker thread).

        The normal mesh path with the granule named rather than derived, so the
        entries land where a real request for that ground will look.  It
        publishes nothing, mints no generation and touches no presented item;
        the only output is mesh-cache entries for the one neighbour the camera
        is travelling towards (``encview.PREFETCH_GRANULES``), bounded by the
        cache's own byte budget and abandoned as soon as a real request lands.
        """
        traced = enc_trace.enabled()
        basis = self._prefetch_basis
        self.counters["prefetch_runs"] += 1
        if (
            basis is None
            or camera != basis["camera"]
            or cancel.is_set()
            or self._scene_cache is None
            or self._mesh_cache is None
            or self._scene_store is None
        ):
            if traced:
                enc_trace.event("enc_prefetch_skip", reason="stale")
            return
        granule, step = _view_granule(basis["plan"], basis["viewport"])
        if granule is None:
            # Unknown extent: there is no neighbour to name, so do nothing.
            if traced:
                enc_trace.event("enc_prefetch_skip", reason="granule")
            return
        candidate, skip = encview.prefetch_candidate(
            granule,
            step=step,
            travel=basis.get("travel"),
            display_scale=basis["display_scale"],
            current_scale=display_scale_for(basis["viewport"]),
        )
        if candidate is None:
            # No travel direction, a zoom step since, or no nameable
            # neighbour: all cheaper to skip than to guess at.
            if traced:
                enc_trace.event("enc_prefetch_skip", reason=skip)
            return
        if traced:
            enc_trace.event(
                "enc_prefetch_start",
                cells=len(basis["plan"]),
                granules=1,
                granule=encview._granule_key(candidate),
            )
        if cancel.is_set():
            if traced:
                enc_trace.event(
                    "enc_prefetch_cancel",
                    granule=encview._granule_key(candidate),
                    reason="demand",
                )
            return
        started = time.perf_counter() if traced else None
        report = encview.prefetch_granule(
            basis["plan"],
            self._scene_cache,
            granule=candidate,
            projection=basis["projection"],
            stroke_pixels=STROKE_PIXELS,
            palette=self._palette,
            cancelled=cancel.is_set,
            mesh_cache=self._mesh_cache,
            loader=_stored_loader(self._scene_store),
            display_scale=basis["display_scale"],
            mesh_scale=1.0,
        )
        if not traced:
            return
        fields = {
            "granule": encview._granule_key(candidate),
            "ms": round((time.perf_counter() - started) * 1000.0, 3),
            "built": report["built"],
            "resident": report["resident"],
            "skipped": report["skipped"],
        }
        if report["cancelled"]:
            enc_trace.event("enc_prefetch_cancel", reason="demand", **fields)
        elif report["reason"] is not None:
            enc_trace.event("enc_prefetch_skip", reason=report["reason"], **fields)
        else:
            enc_trace.event("enc_prefetch_finish", **fields)

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
        retained = self._retained_covering(camera)
        if retained is not None:
            self.counters["retained_reuses"] += 1
            self._notify_after_lock = self._promote_retained(retained)
            return self._published[0]
        return None

    def _retained_covering(self, camera):
        """A retained complete scene whose composed ring covers this camera.

        A return pan asks for ground an earlier composition already drew at
        the same scale.  The ring is scanned newest first, and an entry
        answers only when the demanded camera is inside the ring that entry
        actually composed (its viewport grown by ``compose_margin``), carries
        this demand's content generation and was made for this exact scale,
        pixel ratio and forecast instant.  ``None`` when nothing qualifies:
        the demand then composes as before, with the newest retained frame
        still presented while it assembles.
        """
        for entry in reversed(self._retained_ready):
            if self._retained_covers(entry, camera):
                return entry
        return None

    def _retained_covers(self, entry, camera):
        """Whether one retained publication still serves this camera."""
        _generation, _scene, metadata = entry
        if metadata is None or metadata.get("partial"):
            return False
        if metadata.get("time") != camera.stamp():
            return False
        if metadata.get("dpr", 1) != self._dpr:
            return False
        # A publication without a content marker cannot prove it was drawn
        # with the current palette, so it does not answer rather than risk
        # promoting stale content.
        if metadata.get("content") != self.content_generation:
            return False
        view = metadata.get("camera")
        if view is None:
            return False
        if (
            float(view.zoom),
            float(view.width),
            float(view.height),
        ) != (
            float(camera.zoom),
            float(camera.width),
            float(camera.height),
        ):
            # A different scale is different content: stroke widths, the
            # sounding gate and the mesh frame all belong to one zoom.
            return False
        projection = metadata.get("projection")
        margin = metadata.get("compose_margin")
        if projection is None or margin is None:
            return False
        try:
            point = projection(
                np.array(
                    [[float(camera.center_lon), float(camera.center_lat)]],
                    dtype=np.float64,
                )
            )
        except Exception:  # noqa: BLE001 - a dead projection is only a miss
            return False
        if point.shape != (1, 2) or not np.isfinite(point).all():
            return False
        # The projection is the retained frame's own: its viewport centre is
        # (w/2, h/2), and equal scales make the demanded viewport the same
        # size, so the demanded view fits the composed ring exactly when the
        # demanded centre projects within ``margin`` of the retained centre
        # on both axes.
        margin = float(margin)
        dx = abs(float(point[0, 0]) - float(view.width) / 2.0)
        dy = abs(float(point[0, 1]) - float(view.height) / 2.0)
        return dx <= margin and dy <= margin

    def _promote_retained(self, entry):
        """Re-publish a retained complete scene as the current publication.

        Runs under the lock from :meth:`_reuse`.  The scene is published
        unchanged - its own camera, projection and composed ring travel with
        it, which is what makes the item adopt its already-built GPU
        coverage - and the payload is copied so stale markers a fallback read
        added never leak into the current publication.

        The re-publication carries a *fresh* monotonic generation, never the
        older one the entry was first published under: no listener can
        mistake it for a publication it has already seen and drop it.  The
        scene object itself is what the GPU item keys node reuse on, not the
        generation.
        """
        _generation, scene, metadata = entry
        published = dict(metadata) if metadata is not None else {}
        published["retained_reuse"] = True
        self._generation += 1
        generation = self._generation
        self._published = (generation, scene, published)
        # The promoted scene is the complete frame motion now falls back to.
        self._last_ready = self._published
        self._state = STATE_READY
        self._error = ""
        self._incomplete = INCOMPLETE_LABELS
        total = published.get("planned_cells", published.get("cells", 0))
        self._progress_done = total
        self._progress_total = total
        return self._published

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
        content = metadata.get("content")
        if content is not None and content != self.content_generation:
            # New cells (or a new palette) arrived after this scene was
            # composed: it is no longer this demand's content.
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
            was_published = self._published is not None or self._last_ready is not None
            self._published = None
            # A new ratio invalidates every uploaded pixel, so the retained
            # complete scenes go with it.
            self._retained_ready = []
            self._last_ready = None
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

    def set_palette(self, palette):
        """Adopt a new chart palette; the published scene is withdrawn.

        Mirrors :meth:`set_device_pixel_ratio`: the palette is part of a
        scene's identity, so a scene made with the old palette is not current.
        It is dropped, progress resets and ``READY`` is demoted to ``IDLE`` so
        the next demand re-composes.  Nothing is purged from the caches -
        their keys already carry the palette, so the new palette is simply a
        miss - and the catalog is rebuilt by the worker on the next composition
        because the palette is one of its factory inputs.
        """
        notify = None
        with self._condition:
            if palette == self._palette:
                return False
            self._palette = palette
            # The palette is part of the rendered content: a demand made
            # after this replacement is a different demand.
            self._content_generation += 1
            was_published = self._published is not None or self._last_ready is not None
            self._published = None
            # The palette is part of a scene's identity; the retained complete
            # scenes were drawn with the old colours and must go too.
            self._retained_ready = []
            self._last_ready = None
            self._incomplete = ""
            self._progress_done = 0
            self._progress_total = 0
            if was_published and self._state == STATE_READY:
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
            self._retained_ready = []
            self._last_ready = None
            self._incomplete = ""
            self._progress_done = 0
            self._progress_total = 0
            for request in (self._pending, self._active):
                if request is not None:
                    request[2].set()
                    self.counters["cancelled"] += 1
            if self._prefetch is not None:
                self._prefetch[0].set()
                self._prefetch = None
                self.counters["prefetch_cancelled"] += 1
            self._pending = None
            self._condition.notify_all()

    # ------------------------------------------------------------ worker
    def _should_yield(self, generation):
        """Whether this generation defers to the weather pipeline.

        A busy weather worker defers the scene only within the layer's yield
        budget.  Past that budget the scene composes regardless: in scene mode
        the scene draws the chart itself, and deferring until the raster worker
        is free left it idle until the viewport's five-second source poll.  The
        budget runs from the demand, so a retried generation cannot extend it.
        """
        if self._worker_idle():
            return False
        if self._yield_budget is None:
            return True
        with self._condition:
            demand = self._demand_at if generation == self._generation else None
        if demand is None:
            return True
        return (time.perf_counter() - demand) < self._yield_budget

    def _notify_state(self, state):
        if self._on_state is not None:
            self._on_state(state)

    def _notify_publish(self, payload):
        if self._on_publish is not None:
            self._on_publish(payload)

    # -------------------------------------------------------- publication
    def _unavailable_clips(self, plan, scene):
        """``unavailable_clips`` that never fails an already-assembled scene."""
        try:
            return unavailable_clips(plan, scene)
        except Exception:
            # A malformed plan or geometry is metadata-only damage: record it
            # and publish the scene without clips rather than withdrawing it.
            self.counters["clip_failures"] += 1
            return ()

    def _publish_partial(
        self, generation, scene, done, total, cancel, metadata=None, plan=None
    ):
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

        Once a complete scene has been published, a partial never replaces it:
        the complete frame stays presented and the partial only reports
        progress.  Replacing a finished picture with a hole-ridden subset is
        what makes the chart blank and redraw during a pan or zoom.
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
            if enc_trace.enabled():
                published["cache_counters"] = _cache_counters(
                    self._mesh_cache, self._scene_cache, self._scene_store
                )
            # This snapshot's own refused cells, not the finished scene's:
            # a partial frame must not show a hole it cannot fill.
            published["unavailable_clips"] = self._unavailable_clips(plan, scene)
            # A cell the plan itself could not read never reaches assembly, so
            # its explicit state comes from the plan record, not the scene.
            published["skipped"] = tuple(
                published.get("skipped", ())
            ) + plan_unavailable(published)
            published["cells_order"] = tuple(published.get("cells_order", ()))
            published["cell_states"] = cell_states(
                published["cells_order"], published["skipped"], done
            )
            published.update(
                {
                    "complete": False,
                    "incomplete": partial_incompleteness(done, total),
                    "partial": True,
                    "progress": (done, total),
                }
            )
            if self._last_ready is None:
                # Nothing complete to keep: this partial is the only chart
                # content, so it is what the viewport draws.  The notified
                # payload is that same object, so a listener that records it
                # and a reader that asks the layer for ``published`` see one
                # identity.
                self._published = (generation, scene, published)
                self._incomplete = published["incomplete"]
                payload = self._published
            else:
                # A complete scene is retained and stays presented; the partial
                # is reported for progress only and never stored.
                payload = (generation, scene, published)
            self._state = STATE_LOADING
            self._error = ""
            self._progress_done = done
            self._progress_total = total
            self.counters["partial_publications"] += 1
        self._notify_publish(payload)
        return True

    def _run(self):
        try:
            while True:
                with self._condition:
                    while (
                        self._pending is None
                        and self._prefetch is None
                        and not self._closed
                    ):
                        self._condition.wait()
                    if self._closed:
                        return
                    if self._pending is None:
                        # Nothing real is waiting, so the idle prefetch may use
                        # the worker.  It holds no pending slot, publishes
                        # nothing, and is dropped the moment a demand arrives.
                        request = None
                        prefetch = self._prefetch
                        self._prefetch = None
                    else:
                        prefetch = None
                        request = self._pending
                        self._pending = None
                        self._active = request
                if prefetch is not None:
                    try:
                        self._prefetch_run(*prefetch)
                    except Exception:  # noqa: BLE001 - a warm-up never fails us
                        pass
                    continue
                generation, camera, cancel = request
                began = time.perf_counter()
                error = ""
                scene = None
                metadata = None
                yielded = False
                withdraw = None
                try:
                    if not cancel.is_set():
                        if self._should_yield(generation):
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
                    self._yielded = None
                    if (
                        yielded
                        and not cancel.is_set()
                        and generation == self._generation
                    ):
                        # The weather pipeline has priority: ask again later.
                        # A deferral while a scene is already presented is not
                        # "nothing to show" -- dropping to idle here made every
                        # motion tick hide the chart.  Stay loading (or ready)
                        # and let the next request compose.
                        if self._published is not None or self._last_ready is not None:
                            self._state = STATE_LOADING
                        else:
                            self._state = STATE_IDLE
                            notify_state = STATE_IDLE
                        # The demand is deferred, not refused: keep it so the
                        # retry resumes this same generation.
                        self._yielded = request
                        self.counters["yielded"] += 1
                    elif (
                        cancel.is_set()
                        or generation != self._generation
                        or self._closed
                    ):
                        self.counters["superseded_rejected"] += 1
                        # This generation may already have published a partial
                        # scene.  It is no longer current, so withdraw it, and
                        # name the generation in the withdrawal: the viewport
                        # must ignore it if the scene it actually presents
                        # belongs to another generation.  A retained complete
                        # scene is not withdrawn: it is the last good frame and
                        # the viewport keeps presenting it until a ready
                        # publication replaces it.
                        stale = self._published
                        if (
                            stale is not None
                            and stale[0] == generation
                            and self._last_ready is None
                        ):
                            self._published = None
                            self._incomplete = ""
                            self._progress_done = 0
                            self._progress_total = 0
                            if not self._closed:
                                withdraw = (generation, None, None)
                    elif scene is not None and metadata is not None:
                        state, report = scene_report(scene)
                        metadata = dict(metadata)
                        metadata.update(report)
                        if enc_trace.enabled():
                            metadata["cache_counters"] = _cache_counters(
                                self._mesh_cache, self._scene_cache, self._scene_store
                            )
                        # Every planned cell is resolved in a finished scene;
                        # ``skipped`` gives the ones that drew nothing, and the
                        # plan's own unreadable cells are named alongside them.
                        unreadable = plan_unavailable(metadata)
                        metadata["cell_states"] = cell_states(
                            metadata.get("cells_order", ()),
                            tuple(report["skipped"]) + unreadable,
                        )
                        metadata.setdefault("timings", {})["assembly_ms"] = round(
                            (ended - began) * 1000.0, 3
                        )
                        if state == STATE_READY:
                            self.counters["assemblies"] += 1
                            self.counters["publications"] += 1
                            self.counters["cells"] += report["cells"]
                            self.counters["skipped"] += len(report["skipped"]) + len(
                                unreadable
                            )
                            self.counters["unsupported"] += report["unsupported"]
                            self._published = (generation, scene, metadata)
                            # This is the frame motion may fall back to.
                            self._last_ready = self._published
                            # Complete publications are kept for motion
                            # fallback and return pans, oldest first and
                            # capped: the newest is the motion fallback,
                            # older entries answer a return pan through
                            # ``_retained_covering`` without a recomposition.
                            self._retained_ready.append(self._published)
                            del self._retained_ready[:-RETAINED_SCENES]
                            while (
                                len(self._retained_ready) > 1
                                and _retained_scene_bytes(self._retained_ready)
                                > RETAINED_SCENE_BYTES
                            ):
                                del self._retained_ready[0]
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
                    self._notify_publish(withdraw)
                elif notify_state is not None:
                    self._notify_state(notify_state)
        finally:
            self._release()

    # --------------------------------------------------------- composition
    def _compose(self, camera, cancel):
        """Plan and assemble the cells for ``camera`` on the worker."""
        from sailworks.core.mapviewport import MapViewport
        from sailworks.qt.gribviewportchart import ChartCamera
        from sailworks.qt.viewporttransform import enc_compose_margin

        if self._catalog is None or self._catalog_palette != self._palette:
            if self._catalog_factory is None:
                raise RuntimeError("The ENC scene layer has no catalog source")
            # Created on the worker thread, like the scene cache below, and
            # released with it.  A palette change rebuilds it here, on the
            # worker, rather than mutating the catalog from the GUI thread.
            self._catalog = self._catalog_factory(self._data_dir, self._palette)
            self._catalog_palette = self._palette
        if self._scene_cache is None:
            self._scene_cache = encscene.SceneCache(SCENE_CACHE_BYTES)
        if self._scene_store is None:
            self._scene_store = encscenestore.SceneStore(
                self._cache_dir / "enc-scenes", SCENE_STORE_BYTES
            )
        if self._mesh_cache is None:
            self._mesh_cache = encview.MeshCache(MESH_CACHE_BYTES)
        # Logical pixels: the item lives in QQuickItem coordinates and Qt
        # applies the device pixel ratio itself.
        # The same padded frame the raster composition uses, widened to two
        # viewports across: planning against the bare view drops a cell whose
        # envelope only reaches the padded area, and a frame that only reaches
        # MARGIN past the view runs out on an ordinary drag before its
        # replacement arrives.
        margin = enc_compose_margin(camera)
        viewport = MapViewport(ChartCamera(camera, 1), margin, lambda: None)
        viewport.render_cancelled = cancel.is_set
        # The projection published with the scene is the item's own frame:
        # screen logical pixels with 0,0 at the item's top-left, size
        # camera.width x camera.height.  The padded viewport above plans and
        # meshes; publishing its projection drew everything MARGIN logical
        # pixels right and down.  Reusing the closure for an unchanged camera
        # keeps ``EncMeshItem.set_projection`` a no-op, so a publication that
        # only replaces the scene does not invalidate geometry already correct
        # for this view.
        key = _view_key(camera)
        cached = self._screen_projection
        if cached is None or cached[0] != key:
            cached = (key, projection_for(screen_viewport_for(camera)))
            self._screen_projection = cached
        # Opt-in stage attribution for this composition.  It is the same dict
        # ``_run`` adds ``assembly_ms`` to, so a publication carries the stage
        # breakdown beside the total it belongs to.
        timings = {}
        if enc_trace.enabled():
            # Per-cell detail for the trace; absent when tracing is off, so the
            # build pays nothing for it.
            timings["cells"] = {}
        metadata = {
            "camera": camera,
            "time": camera.stamp(),
            "dpr": self._dpr,
            "palette": self._palette,
            "timings": timings,
            # The scene carries lon/lat positions; the item must project them
            # with the same view that produced this scene.  Without it the
            # item drew degrees, i.e. a chart at (-124, 48) and nothing on
            # screen.
            "projection": cached[1],
            # The ring the item must keep attached: culling against anything
            # narrower detaches cells a pan is about to bring into view.
            "compose_margin": margin,
            # Refused-but-planned cells publish their trimmed ground here once
            # assembly has run; absent or empty means nothing needs a fallback.
            "unavailable_clips": (),
        }
        # The generation this composition belongs to: a newer request bumps the
        # layer's generation and the progress callback then refuses to publish.
        with self._condition:
            generation = self._generation
        self._catalog.refresh()
        manifest = self._catalog.manifest
        # A downloaded/replaced cell or a class toggle is visible here even
        # when the settings stamp did not move; it bumps the content identity
        # so the viewport's demand key changes and a stationary camera asks
        # again.
        self._note_content(manifest)
        metadata["content"] = self.content_generation
        if not manifest.class_enabled:
            metadata["reason"] = "ENC charts are disabled"
            return None, metadata
        # A cell the catalog itself could not read is part of the composition,
        # not absent from it: it keeps its place and the catalog's reason, so
        # the caller can name it instead of inferring its disappearance from a
        # smaller drawable set.  It contributes no clip and no coverage claim,
        # exactly like a cell the plan cannot read below.
        unavailable = [
            encplan.UnavailableCell(
                path=os.fspath(source.path),
                rank_key=chart_scale_rank(source.native_scale, viewport.get_scale()),
                reason=str(source.error),
            )
            for source in self._catalog.sources
            if source.error
        ]
        usable = tuple(
            source.descriptor for source in self._catalog.sources if not source.error
        )
        if not usable:
            metadata["reason"] = "no readable ENC cell is enabled"
            metadata["planned_cells"] = len(unavailable)
            metadata["unavailable"] = tuple(
                (cell.path, cell.reason) for cell in unavailable
            )
            metadata["cells_order"] = tuple(
                {
                    "path": os.fspath(cell.path),
                    "name": os.path.basename(cell.path),
                    "rank_key": tuple(cell.rank_key),
                    "version": None,
                    "coverage_known": False,
                }
                for cell in unavailable
            )
            return None, metadata
        if cancel.is_set():
            return None, metadata
        # A cell the plan cannot read is recorded here, not raised: the rest of
        # the ordered set still composes, and the viewport keeps the raster over
        # whatever ground nothing readable draws.
        plan_started = time.perf_counter() if enc_trace.enabled() else None
        plan = encplan.plan_cells(
            viewport, usable, cancelled=cancel.is_set, unavailable=unavailable
        )
        if plan_started is not None:
            timings["plan"] = timings.get("plan", 0.0) + (
                time.perf_counter() - plan_started
            )
        if plan is None or cancel.is_set():
            return None, metadata
        if enc_trace.enabled():
            granule, _step = _view_granule(plan, viewport)
            metadata["granule"] = (
                encview._granule_key(granule) if granule is not None else None
            )
        metadata["planned_cells"] = len(plan) + len(unavailable)
        metadata["unavailable"] = tuple(
            (cell.path, cell.reason) for cell in unavailable
        )
        # Reporting only: the ordered identity of the plan, so a consumer can
        # name every planned cell instead of inferring it from a count.  A cell
        # that could not be read is named after the drawable ones with the same
        # identity shape and its own state record.
        metadata["cells_order"] = tuple(cell_identity(cell) for cell in plan) + tuple(
            {
                "path": os.fspath(cell.path),
                "name": os.path.basename(cell.path),
                "rank_key": tuple(cell.rank_key),
                "version": None,
                "coverage_known": False,
            }
            for cell in unavailable
        )
        if not plan:
            metadata["reason"] = (
                "no readable ENC cell is enabled"
                if unavailable
                else "no ENC cell covers this view"
            )
            return None, metadata
        if generation == self._generation and not cancel.is_set():
            # What an idle prefetch needs to warm the ground beside this view:
            # references only, read again solely while no demand is pending.
            previous = self._prefetch_basis
            travel = None
            if previous is not None:
                before = previous["camera"]
                travel = (
                    camera.center_lon - before.center_lon,
                    camera.center_lat - before.center_lat,
                )
            self._prefetch_basis = {
                "camera": camera,
                "plan": plan,
                "viewport": viewport,
                "projection": projection_for(viewport),
                "display_scale": display_scale_for(viewport),
                "travel": travel,
            }
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
            loader=_stored_loader(self._scene_store, timings),
            timings=timings,
            on_cell=lambda snapshot, done, total: self._publish_partial(
                generation, snapshot, done, total, cancel, metadata, plan
            ),
        )
        if scene is None:
            return None, metadata
        # A planned cell the cache refused still owns the ground its plan entry
        # was trimmed to, so carry that clip beside the reason-only records.
        metadata["unavailable_clips"] = self._unavailable_clips(plan, scene)
        return scene, metadata

    def _release(self):
        """Release worker-owned handles; runs on the worker thread."""
        self._catalog = None
        self._catalog_palette = None
        self._scene_cache = None
        self._scene_store = None
        self._mesh_cache = None
        self._screen_projection = None


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
