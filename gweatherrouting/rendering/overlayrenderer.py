"""Bounded background rendering and cached image reuse for map overlays."""

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Event, Thread

import cairo
from gweatherrouting.core.scheduling import schedule_idle

from gweatherrouting.core.mapviewport import MapViewport

logger = logging.getLogger("gweatherrouting")
MARGIN = 256


@dataclass
class RenderJob:
    key: tuple
    viewport: MapViewport
    charts: tuple
    factor: int
    palette: str
    cancel: Event = field(default_factory=Event)
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    published_at: float | None = None
    cancelled_at: float | None = None
    cancel_reason: str = ""
    request_id: int = 0


@dataclass
class RenderState:
    key: tuple = ()
    frame: object = None
    requested: object = None
    retry_after: float = 0.0
    demand: object = None


def fits(viewport, canvas):
    if viewport._zoom != canvas._zoom:
        return False
    if viewport.width != canvas.get_allocated_width() + 2 * MARGIN:
        return False
    if viewport.height != canvas.get_allocated_height() + 2 * MARGIN:
        return False
    dx = viewport._lon_to_x(viewport._center_lon) - viewport._lon_to_x(
        canvas._center_lon
    )
    dy = viewport._lat_to_y(viewport._center_lat) - viewport._lat_to_y(
        canvas._center_lat
    )
    return max(abs(dx), abs(dy)) <= MARGIN / 2


def covers(viewport, canvas):
    """Whether prepared pixels enclose this view at native zoom (not data coverage).

    Unlike the refresh threshold in fits(), this uses the actual image extent
    and permits a resize only when the new view is still entirely enclosed.
    """
    if viewport._zoom != canvas._zoom:
        return False
    dx = viewport._lon_to_x(viewport._center_lon) - viewport._lon_to_x(
        canvas._center_lon
    )
    dy = viewport._lat_to_y(viewport._center_lat) - viewport._lat_to_y(
        canvas._center_lat
    )
    return (
        abs(dx) + canvas.get_allocated_width() / 2 <= viewport.width / 2
        and abs(dy) + canvas.get_allocated_height() / 2 <= viewport.height / 2
    )


def paint_viewport(canvas, cr, viewport, surface):
    ratio = 2.0 ** (canvas._zoom - viewport._zoom)
    x = canvas._lon_to_x(viewport._center_lon) - canvas._lon_to_x(canvas._center_lon)
    y = canvas._lat_to_y(viewport._center_lat) - canvas._lat_to_y(canvas._center_lat)
    x += canvas.get_allocated_width() / 2 - viewport.width * ratio / 2
    y += canvas.get_allocated_height() / 2 - viewport.height * ratio / 2
    cr.save()
    cr.translate(x, y)
    cr.scale(ratio, ratio)
    cr.set_source_surface(surface, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_BILINEAR)
    cr.paint()
    cr.restore()


class OverlayRenderer:
    def __init__(self, schedule=schedule_idle):
        self.schedule = schedule
        self._states = {}
        self._pending = OrderedDict()
        self._running = False
        self._active_canvas = None
        self._active_job: RenderJob | None = None
        self._coalesce_active = True
        self._next_request_id = 0

    def forget(self, canvas):
        state = self._states.pop(canvas, None)
        self._pending.pop(canvas, None)
        if state and state.requested:
            self._cancel(state.requested, "forgotten")
        if self._active_canvas is canvas and self._active_job is not None:
            self._cancel(self._active_job, "forgotten")

    def _cancel(self, job, reason):
        if not job.cancel.is_set():
            job.cancelled_at = time.monotonic()
            job.cancel_reason = reason
            job.cancel.set()
            self._schedule_event("cancel", job)

    def _schedule_event(self, event, job):
        """Optional observer; no production event history or GUI access in workers."""
        pass

    def draw(self, canvas, cr, charts, palette):
        if not charts:
            self.forget(canvas)
            return
        factor = canvas.get_scale_factor()
        key = (palette, factor, tuple(c[0] for c in charts))
        state = self._states.setdefault(canvas, RenderState())
        state.key = key
        state.demand = MapViewport(canvas, on_redraw=lambda: None)
        active = self._active_job if self._active_canvas is canvas else None
        if (
            self._coalesce_active
            and active is not None
            and (
                not self._can_complete(active, key)
                or (
                    not getattr(canvas, "_zoom_animating", False)
                    and not self._keep_active(active, canvas, key)
                )
            )
        ):
            self._cancel(active, "obsolete active demand")
        queued = self._pending.get(canvas)
        if (
            self._coalesce_active
            and queued is not None
            and not self._can_complete(queued, key)
        ):
            self._pending.pop(canvas)
            self._cancel(queued, "obsolete queued demand")
            if state.requested is queued:
                state.requested = None
        frame = state.frame
        fresh = frame and frame[0].key == key and fits(frame[0].viewport, canvas)
        if (
            self._coalesce_active
            and active is not None
            and not active.cancel.is_set()
            and active.key == key
            and fits(active.viewport, canvas)
        ):
            queued = self._pending.pop(canvas, None)
            if queued is not None:
                self._cancel(queued, "covered by active request")
            state.requested = active
        requested = state.requested
        pending = (
            requested
            and not requested.cancel.is_set()
            and requested.key == key
            and fits(requested.viewport, canvas)
        )
        if fresh and requested and not pending:
            self._cancel(requested, "already fresh")
            self._pending.pop(canvas, None)
            state.requested = None
        if (
            not fresh
            and not pending
            and not getattr(canvas, "_zoom_animating", False)
            and time.monotonic() >= state.retry_after
        ):
            if requested is not None and not (
                self._keep_active(requested, canvas, key)
                and requested is self._active_job
            ):
                self._cancel(requested, "superseded request")
            viewport = MapViewport(canvas, MARGIN, lambda: None)
            job = RenderJob(key, viewport, charts, factor, palette)
            self._next_request_id += 1
            job.request_id = self._next_request_id
            viewport.render_cancelled = job.cancel.is_set
            state.requested = job
            self._pending[canvas] = job
            self._schedule_event("request", job)
            self._start_next()
        if frame and self._can_preview(frame[0], key):
            self._paint(canvas, cr, *frame)

    def _can_complete(self, job, key):
        return job.key == key

    def _keep_active(self, job, canvas, key):
        return (
            not job.cancel.is_set()
            and fits(job.viewport, canvas)
            and self._can_complete(job, key)
        )

    def _can_preview(self, job, key):
        return job.key == key

    @staticmethod
    def _paint(canvas, cr, job, surface):
        paint_viewport(canvas, cr, job.viewport, surface)

    def _start_next(self):
        if self._running or not self._pending:
            return
        canvas, job = self._pending.popitem(last=False)
        self._running = True
        self._active_canvas, self._active_job = canvas, job
        job.started_at = time.monotonic()
        self._schedule_event("start", job)

        def worker():
            surface, error = None, None
            try:
                surface = self._render(job)
            except Exception as exc:
                error = str(exc)
            job.finished_at = time.monotonic()
            self._schedule_event("finish", job)
            self.schedule(self._completed, canvas, job, surface, error)

        Thread(target=worker, name="overlay-render", daemon=True).start()

    def _render(self, job):
        raise NotImplementedError

    def _completed(self, canvas, job, surface, error):
        self._running = False
        self._active_canvas = self._active_job = None
        state = self._states.get(canvas)
        if state:
            if state.requested is job:
                state.requested = None
            if error:
                state.retry_after = time.monotonic() + 1
                logger.warning("Unable to render map overlay: %s", error)
            if (
                surface is not None
                and not job.cancel.is_set()
                and self._can_complete(job, state.key)
            ):
                state.frame = (job, surface)
                job.published_at = time.monotonic()
                self._schedule_event("publish", job)
                canvas.queue_draw()
        self._start_next()
        return False
