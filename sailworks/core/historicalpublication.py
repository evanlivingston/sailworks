"""Scene publication, deletion and recovery for historical routing jobs.

Split out of :mod:`sailworks.core.historicalcoordinator` so the job sequence and
the live-scene lifecycle stay separately readable. The coordinator owns a
publisher and delegates to it, so the method names and signatures are unchanged.

Publication is idempotent: the job record's ``published`` ledger keys each route
uid, so re-publishing the same live routes assigns nothing, saves nothing and
reports no change. Deletion removes routes and releases pins but never deletes
an ordinary scene group: it only detaches the job from the scene.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sailworks.core.historicalrouting import JobState
from sailworks.core.scene import KIND_ROUTING

__all__ = [
    "DEFAULT_ROUTING_GROUP",
    "GROUP_PREFIX",
    "PUBLISHED_ROUTE_PREFIX",
    "CoordinatorError",
    "HistoricalPublisher",
]

GROUP_PREFIX = "historical-routing:"
DEFAULT_ROUTING_GROUP = "default-routings"
PUBLISHED_ROUTE_PREFIX = "route:"


class CoordinatorError(Exception):
    """The job cannot continue in the state it is in."""


class HistoricalPublisher:
    """Group, publish, delete and recover one job's routes in the live scene.

    ``store`` is a :class:`~sailworks.core.historicalroutingstore.
    HistoricalRoutingStore`; ``scene`` is an optional
    :class:`~sailworks.core.scene.SceneCollection`; ``now`` is an injected clock
    (a callable returning an aware datetime, or a fixed datetime). Without a
    scene every method still maintains the durable record.
    """

    def __init__(self, *, store, scene=None, now=None):
        self.store = store
        self.scene = scene
        if now is None:
            self._clock = lambda: datetime.now(timezone.utc)
        elif callable(now):
            self._clock = now
        else:
            fixed = now
            self._clock = lambda: fixed

    def _stamp(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    def ensure_group(self, record) -> str:
        """Create the job's group once; ``""`` when there is no scene."""
        if self.scene is None:
            return ""
        if record.scene_group_id:
            return record.scene_group_id
        identifier = f"{GROUP_PREFIX}{record.job_id}"
        name = str(record.spec.get("group_name") or identifier)
        group_id = self.scene.ensure_group(
            identifier, name, parent=DEFAULT_ROUTING_GROUP
        )
        record.scene_group_id = str(group_id or identifier)
        self.store.save(record)
        return record.scene_group_id

    def publish_routes(self, job_id, routes, *, entries=None):
        """Put this job's live routes in its scene group; return changed?

        ``entries`` is the caller's complete live scene entry set: ``reconcile``
        prunes every item it is not given, so it only runs when the whole set is
        supplied. Without ``entries`` this only groups ``routes``.
        """
        if self.scene is None:
            return False
        record = self.store.load(job_id)
        group_id = self.ensure_group(record)
        changed = False
        if entries is not None:
            self.scene.reconcile(entries)
            changed = True
        for route in routes or ():
            uid = str(getattr(route, "uid", "") or "")
            if not uid:
                raise CoordinatorError(f"route {route!r} has no uid")
            key = f"{PUBLISHED_ROUTE_PREFIX}{uid}"
            if record.published.get(key) == uid:
                continue
            # An ensemble publishes dozens of routes at once. Show the route
            # lines and leave the contours off so the map stays readable; the
            # user can turn any isochrone layer on by hand. Only the first
            # publication sets this, so a later user toggle is never undone.
            try:
                route.isochrones_visible = False
            except AttributeError:
                pass
            self.scene.assign({(KIND_ROUTING, uid)}, group_id, save=False)
            record.published[key] = uid
            changed = True
        if changed:
            self.store.save(record)
            self.scene.save()
        return changed

    def delete_job(self, job_id, routes, *, collection=None):
        """Remove the job's routes, release its pins and archive the job.

        The scene group itself is left alone: deleting routes must never take an
        ordinary scene group with it. The record is marked ``scene_detached`` so
        a later scene edit cannot reintroduce the routes.
        """
        record = self.store.load(job_id)
        target = collection if collection is not None else self.scene
        removed = []
        if target is not None:
            for route in routes or ():
                if self._remove_route(target, route):
                    removed.append(str(getattr(route, "uid", "") or ""))
        record.state = JobState.ARCHIVED.value
        record.scene_detached = True
        record.last_checkpoint_utc = self._stamp()
        self.store.save(record)
        self.store.release(record)
        return tuple(removed)

    def recover(self):
        """What a restarted process should do; never writes."""
        return self.store.recover()

    @staticmethod
    def _remove_route(collection, route) -> bool:
        """Remove one live route through whichever collection API it exposes."""
        uid = str(getattr(route, "uid", "") or "")
        for name in ("remove_route", "remove"):
            remove = getattr(collection, name, None)
            if not callable(remove):
                continue
            try:
                result = remove(route)
            except TypeError:
                if not uid:
                    return False
                result = remove(uid)
            return True if result is None else bool(result)
        routes = getattr(collection, "routes", None)
        if isinstance(routes, dict) and uid:
            routes.pop(uid, None)
            return True
        raise CoordinatorError(f"collection cannot remove route {uid!r}")
