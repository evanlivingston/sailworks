"""Explicit native-weather reservations and render-context-owned field groups.

Policy is headless. Only the Qt adapter (constructed explicitly) may create or
destroy scene-graph objects. Byte totals are texture payload reservations, not
physical VRAM, driver overhead or GPU completion telemetry.
"""

from collections import OrderedDict
from dataclasses import dataclass
import os
from threading import RLock

NATIVE_WEATHER_BYTES = 32 * 1024 * 1024


def native_weather_fields_enabled(override=None):
    """Native is the default; an explicit option wins over the recovery env."""
    if override is not None:
        return bool(override)
    return os.environ.get("GWR_NATIVE_WEATHER_FIELDS", "1") != "0"


class NativeFieldTooLarge(ValueError):
    """A complete endpoint group cannot fit the declared native payload cap."""


@dataclass(frozen=True)
class NativeFieldKey:
    source_epoch: int
    scene_epoch: int
    identity: object
    representation: str = "nodes"


@dataclass
class Reservation:
    owner: object
    key: object
    nbytes: int
    serial: int
    pins: int = 1
    state: str = "reserved"
    used: int = 0


class NativeWeatherBudget:
    """One session-wide cap shared by contexts; callbacks never delete resources.

    Retiring bytes remain charged until the owner confirms destruction. Lease
    identity uses the reservation object, so a late release cannot affect a
    different allocation that reused the same key.
    """

    def __init__(self, byte_limit=NATIVE_WEATHER_BYTES):
        if byte_limit <= 0:
            raise ValueError("Native weather budget must be positive")
        self.byte_limit = int(byte_limit)
        self._lock = RLock()
        self._owners = {}
        self._entries = {}
        self._serial = 0
        self._clock = 0
        self._peak = 0
        self.evictions = 0

    def register(self, owner, retire, wake):
        with self._lock:
            if owner in self._owners:
                raise ValueError("Native context already registered")
            self._owners[owner] = (retire, wake)

    def unregister(self, owner):
        with self._lock:
            if any(ticket.owner == owner for ticket in self._entries.values()):
                raise RuntimeError("Cannot unregister a context with charged resources")
            self._owners.pop(owner, None)

    def has_resources(self, owner):
        with self._lock:
            return any(ticket.owner is owner for ticket in self._entries.values())

    def _touch(self, ticket):
        self._clock += 1
        ticket.used = self._clock

    def reserve(self, owner, key, nbytes):
        callbacks = []
        with self._lock:
            if owner not in self._owners:
                raise RuntimeError("Unregistered native context")
            if nbytes <= 0:
                raise ValueError("Native group size must be positive")
            if nbytes > self.byte_limit:
                raise NativeFieldTooLarge("Weather field exceeds native payload budget")
            if (owner, key) in self._entries:
                raise RuntimeError("Duplicate native reservation")
            current = sum(ticket.nbytes for ticket in self._entries.values())
            if current + nbytes <= self.byte_limit:
                self._serial += 1
                ticket = Reservation(owner, key, int(nbytes), self._serial)
                self._touch(ticket)
                self._entries[owner, key] = ticket
                self._peak = max(self._peak, current + nbytes)
                return ticket
            # Request the oldest unpinned live groups. Do not lend bytes from
            # these groups before their render-thread destruction is confirmed.
            pending = sum(
                t.nbytes for t in self._entries.values() if t.state == "retiring"
            )
            for victim in sorted(self._entries.values(), key=lambda t: t.used):
                if current + nbytes - pending <= self.byte_limit:
                    break
                if victim.state != "live" or victim.pins:
                    continue
                victim.state = "retiring"
                pending += victim.nbytes
                self.evictions += 1
                callbacks.append((self._owners[victim.owner][0], victim.key))
        for callback, victim_key in callbacks:
            callback(victim_key)
        return None

    def commit(self, ticket):
        with self._lock:
            if self._entries.get((ticket.owner, ticket.key)) is not ticket:
                raise RuntimeError("Stale native reservation")
            if ticket.state != "reserved":
                raise RuntimeError("Native reservation already committed")
            ticket.state = "live"

    def acquire(self, owner, key):
        with self._lock:
            ticket = self._entries.get((owner, key))
            if ticket is None or ticket.state != "live":
                return None
            ticket.pins += 1
            self._touch(ticket)
            return ticket

    def unpin(self, ticket):
        with self._lock:
            if self._entries.get((ticket.owner, ticket.key)) is not ticket:
                return  # cancelled request from a retired scene generation
            if ticket.pins <= 0:
                raise RuntimeError("Unbalanced native lease release")
            ticket.pins -= 1

    def releasable(self, ticket):
        with self._lock:
            return (
                self._entries.get((ticket.owner, ticket.key)) is ticket
                and not ticket.pins
            )

    def is_live(self, ticket):
        with self._lock:
            return (
                self._entries.get((ticket.owner, ticket.key)) is ticket
                and ticket.state == "live"
            )

    def released(self, ticket):
        with self._lock:
            if self._entries.get((ticket.owner, ticket.key)) is not ticket:
                return
            del self._entries[ticket.owner, ticket.key]
            wakes = [wake for _retire, wake in self._owners.values()]
        for wake in wakes:
            wake()

    def statistics(self):
        with self._lock:
            totals = {
                state
                + "_bytes": sum(
                    t.nbytes for t in self._entries.values() if t.state == state
                )
                for state in ("reserved", "live", "retiring")
            }
            return {
                **totals,
                "current_bytes": sum(totals.values()),
                "peak_bytes": self._peak,
                "limit_bytes": self.byte_limit,
                "groups": len(self._entries),
                "contexts": len(self._owners),
                "evictions": self.evictions,
            }


@dataclass
class NativeGroup:
    ticket: Reservation
    textures: tuple


class NativeFieldStore:
    """Render-thread-only resource groups; allocator/disposer are context-local.

    A GUI-side acquired lease may be passed to bind(). It pins a complete live
    group until synchronization, allowing warm readiness without touching Qt
    objects from the GUI thread. Unbinding releases the visible lease only;
    inactive textures remain resident until explicit retirement or pressure.
    """

    def __init__(self, budget, allocate, dispose, wake=lambda: None):
        self.budget, self.allocate, self.dispose = budget, allocate, dispose
        self.owner = object()
        self.entries = OrderedDict()
        self.current = None
        self._retire_lock = RLock()
        self._retire_keys = set()
        self._wake = wake
        self.created = self.destroyed = 0
        budget.register(self.owner, self.request_retirement, wake)

    def request_retirement(self, key):
        with self._retire_lock:
            self._retire_keys.add(key)
        self._wake()

    def bind(self, key, images, lease=None):
        if len(images) != 4:
            raise ValueError("Native weather fields require four endpoint images")
        if lease is not None and (lease.owner is not self.owner or lease.key != key):
            self.budget.unpin(lease)
            raise ValueError("Native lease belongs to another field/context")
        if lease is not None and (
            not self.budget.is_live(lease)
            or key not in self.entries
            or self.entries[key].ticket is not lease
        ):
            self.budget.unpin(lease)
            raise ValueError("Native lease belongs to a retired allocation")
        if self.current == key:
            if lease is not None:
                self.budget.unpin(lease)  # current already owns a visible pin
            return self.entries[key].textures
        group = self.entries.get(key)
        if group is not None:
            lease = lease or self.budget.acquire(self.owner, key)
            if lease is not None:
                self.unbind()
                self.current = key
                return group.textures
            # A retiring identity must finish retirement before its reload.
            self.unbind()
            return None
        if lease is not None:
            self.budget.unpin(lease)
            raise RuntimeError("Live native reservation has no allocated group")
        self.unbind()
        ticket = self.budget.reserve(
            self.owner, key, sum(i.sizeInBytes() for i in images)
        )
        if ticket is None:
            return None
        textures = []
        try:
            for image in images:
                textures.append(self.allocate(image))
                self.created += 1
        except Exception:
            # No consumer has seen this incomplete group. Roll back all
            # resources before releasing the full reservation.
            for texture in textures:
                self.dispose(texture)
                self.destroyed += 1
            self.budget.released(ticket)
            raise
        self.entries[key] = NativeGroup(ticket, tuple(textures))
        self.budget.commit(ticket)
        self.current = key
        return self.entries[key].textures

    def unbind(self):
        if self.current is not None:
            self.budget.unpin(self.entries[self.current].ticket)
            self.current = None

    def after_sync(self):
        """Destroy only after consumers' detach/bind reached the scene graph."""
        with self._retire_lock:
            keys, self._retire_keys = self._retire_keys, set()
        for key in keys:
            group = self.entries.get(key)
            if group is None or not self.budget.releasable(group.ticket):
                continue
            for texture in group.textures:
                self.dispose(texture)
                self.destroyed += 1
            del self.entries[key]
            self.budget.released(group.ticket)

    def clear(self):
        self.unbind()
        for key in tuple(self.entries):
            self.request_retirement(key)
        self.after_sync()


def native_texture_context(window, parent_item, budget):
    """Create an explicitly enabled Qt adapter; importing policy needs no Qt.

    Returned GUI object offers request(identity, images, representation), clear(),
    close(), sources (four QQuickItems), stateChanged and snapshot(). All native
    objects stay in the window's callbacks. No experiment tooling is imported.
    """
    from threading import get_ident
    import shiboken6
    from PySide6.QtCore import QObject, Qt, Signal
    from PySide6.QtGui import QImage
    from PySide6.QtQuick import (
        QQuickItem,
        QQuickWindow,
        QSGNode,
        QSGTexture,
        QSGTextureProvider,
    )

    class Provider(QSGTextureProvider):
        def __init__(self, context, index):
            super().__init__()
            self.context, self.index = context, index

        def texture(self):
            self.context._assert_render()
            self.context._texture_queries += 1
            key = self.context._store.current
            group = self.context._store.entries.get(key)
            if group is None:
                return None
            texture = group.textures[self.index]
            return texture if shiboken6.isValid(texture) else None

    class Source(QQuickItem):
        def __init__(self, context, index):
            super().__init__(parent_item)
            self.context, self.index = context, index
            self.setFlag(QQuickItem.Flag.ItemHasContents, True)
            self.setWidth(1)
            self.setHeight(1)
            self.node = None

        def isTextureProvider(self):  # noqa: N802
            return True

        def textureProvider(self):  # noqa: N802
            self.context._assert_render()
            self.context._provider_queries += 1
            return self.context._provider(self.index)

        def updatePaintNode(self, old, _data):  # noqa: N802
            self.node = old if old is not None else QSGNode()
            return self.node

    class Context(QObject):
        stateChanged = Signal(object)
        _status = Signal(object)
        _wake = Signal()

        def __init__(self):
            # Caller retains this GUI object through widget destruction. A QML
            # root parent would disconnect our invalidation receiver too early.
            super().__init__()
            self._lock = RLock()
            self._closed = False
            self._registered = True
            self._source_epoch = self._scene_epoch = self._sequence = 0
            self._pending = self._desired = self._latest = None
            self._frame_ready = self._last_delivered = None
            self._clear_requested = False
            self._render_thread = None
            self._providers = [None] * 4
            self._created = self._destroyed = self._invalidations = 0
            self._provider_queries = self._texture_queries = 0
            self._errors = []
            self._snapshot = {}
            self.ready = False
            self._store = NativeFieldStore(
                budget, self._allocate, self._dispose, self._notify_wake
            )
            self.sources = tuple(Source(self, i) for i in range(4))
            self._status.connect(
                self._accept_status, Qt.ConnectionType.QueuedConnection
            )
            self._wake.connect(self._request_frame, Qt.ConnectionType.QueuedConnection)
            window.beforeSynchronizing.connect(
                self._before_sync, Qt.ConnectionType.DirectConnection
            )
            window.afterSynchronizing.connect(
                self._after_sync, Qt.ConnectionType.DirectConnection
            )
            window.afterRendering.connect(
                self._observe, Qt.ConnectionType.DirectConnection
            )
            window.sceneGraphInvalidated.connect(
                self._invalidated, Qt.ConnectionType.DirectConnection
            )
            parent_item.destroyed.connect(self._mark_closed)

        def _assert_render(self):
            if get_ident() != self._render_thread:
                raise RuntimeError(
                    "Native weather operation outside render callback thread"
                )

        def _notify_wake(self):
            if shiboken6.isValid(self) and not self._closed:
                self._wake.emit()

        def _request_frame(self):
            if not self._closed and shiboken6.isValid(self):
                for item in self.sources:
                    if shiboken6.isValid(item):
                        item.update()
                if shiboken6.isValid(window):
                    window.update()

        def request(self, identity, images, representation="nodes"):
            with self._lock:
                if self._closed:
                    return False
                if self._pending is not None and self._pending[3] is not None:
                    budget.unpin(self._pending[3])
                self._sequence += 1
                key = NativeFieldKey(
                    self._source_epoch, self._scene_epoch, identity, representation
                )
                lease = budget.acquire(self._store.owner, key)
                self._pending = (self._sequence, key, tuple(images), lease)
                self._latest = (self._sequence, key)
                self.ready = lease is not None
            self._request_frame()
            return self.ready

        def clear(self):
            with self._lock:
                self._source_epoch += 1
                self._sequence += 1
                if self._pending is not None and self._pending[3] is not None:
                    budget.unpin(self._pending[3])
                self._pending = self._desired = self._latest = None
                self._frame_ready = self._last_delivered = None
                self._clear_requested = True
                self.ready = False
            self._request_frame()

        def _mark_closed(self, *_args):
            # Metadata only: this can run as QObject wrappers are invalidated.
            with self._lock:
                self._closed = True
                if self._pending is not None and self._pending[3] is not None:
                    budget.unpin(self._pending[3])
                self._pending = self._desired = self._latest = None
                self._clear_requested = True
                self.ready = False

        def close(self):
            self.clear()
            self._mark_closed()
            if self._registered and not budget.has_resources(self._store.owner):
                budget.unregister(self._store.owner)
                self._registered = False

        def _provider(self, index):
            if self._closed:
                return None
            if self._providers[index] is None:
                self._providers[index] = Provider(self, index)
            return self._providers[index]

        def _allocate(self, image):
            self._assert_render()
            if image.format() != QImage.Format.Format_RGBA8888:
                raise ValueError("Native weather expects opaque packed RGBA8888")
            texture = window.createTextureFromImage(
                image, QQuickWindow.CreateTextureOption.TextureIsOpaque
            )
            if texture is None:
                raise RuntimeError("Unable to allocate native weather texture")
            self._created += 1
            texture.destroyed.connect(
                self._on_destroyed, Qt.ConnectionType.DirectConnection
            )
            try:
                # PySide factory-return parenting otherwise invalidates this
                # wrapper with its window before scene invalidation cleanup.
                texture.setParent(None)
                if not shiboken6.ownedByPython(texture) or texture.parent() is not None:
                    raise RuntimeError("Native weather wrapper adoption failed")
                texture.setFiltering(QSGTexture.Filtering.Nearest)
                return texture
            except Exception:
                if shiboken6.isValid(texture):
                    shiboken6.delete(texture)
                raise

        def _on_destroyed(self, *_args):
            self._assert_render()
            self._destroyed += 1

        def _dispose(self, texture):
            self._assert_render()
            if not shiboken6.isValid(texture) or not shiboken6.ownedByPython(texture):
                raise RuntimeError(
                    "Native weather texture ownership lost before retirement"
                )
            shiboken6.delete(texture)

        def _emit_status(self, sequence, key, ready, error=""):
            if shiboken6.isValid(self) and not self._closed:
                self._status.emit(
                    {
                        "sequence": sequence,
                        "key": key,
                        "ready": ready,
                        "error": error,
                        "frame_observed": self._frame_ready == (sequence, key),
                    }
                )

        def _accept_status(self, status):
            with self._lock:
                if status.get("invalidated"):
                    if (
                        self._closed
                        or status["source_epoch"] != self._source_epoch
                        or status["scene_epoch"] != self._scene_epoch
                    ):
                        return
                    self.ready = False
                    self.stateChanged.emit(status)
                    return
                if self._closed or self._latest != (status["sequence"], status["key"]):
                    return
                if self._last_delivered == status:
                    return
                self._last_delivered = status.copy()
                self.ready = status["ready"]
            self.stateChanged.emit(status)

        def _before_sync(self):
            self._render_thread = get_ident()
            try:
                with self._lock:
                    if self._clear_requested:
                        self._store.unbind()
                        for key in tuple(self._store.entries):
                            self._store.request_retirement(key)
                        self._clear_requested = False
                    if self._pending is not None:
                        self._desired, self._pending = self._pending, None
                    desired = self._desired
                    if self._closed or desired is None:
                        for provider in self._providers:
                            if provider is not None:
                                provider.textureChanged.emit()
                        return
                    sequence, key, images, lease = desired
                    self._desired = (sequence, key, images, None)
                    textures = self._store.bind(key, images, lease)
                    for index in range(4):
                        self._provider(index).textureChanged.emit()
                    self._emit_status(sequence, key, textures is not None)
            except Exception as exc:
                self._errors.append(str(exc))
                if self._desired is not None:
                    self._emit_status(
                        self._desired[0], self._desired[1], False, str(exc)
                    )
                self._desired = None

        def _after_sync(self):
            try:
                self._assert_render()
                self._store.after_sync()
            except Exception as exc:
                self._errors.append(str(exc))

        def _observe(self):
            try:
                self._assert_render()
                groups = []
                for key, group in self._store.entries.items():
                    handles = []
                    for texture in group.textures:
                        native = texture.rhiTexture()
                        handles.append(
                            None
                            if native is None
                            else shiboken6.getCppPointer(native)[0]
                        )
                    groups.append(
                        {
                            "identity": repr(key.identity),
                            "serial": group.ticket.serial,
                            "handles": handles,
                            "atlas": any(t.isAtlasTexture() for t in group.textures),
                        }
                    )
                with self._lock:
                    self._snapshot = {
                        "groups": groups,
                        "current": (
                            None
                            if self._store.current is None
                            else repr(self._store.current.identity)
                        ),
                        "created": self._created,
                        "destroyed": self._destroyed,
                        "invalidations": self._invalidations,
                        "provider_queries": self._provider_queries,
                        "texture_queries": self._texture_queries,
                        "errors": list(self._errors),
                    }
                    if (
                        self._desired is not None
                        and self._store.current == self._desired[1]
                    ):
                        active = self._store.entries.get(self._store.current)
                        if active is not None and all(
                            texture.rhiTexture() is not None
                            for texture in active.textures
                        ):
                            self._frame_ready = (self._desired[0], self._desired[1])
                            self._emit_status(self._desired[0], self._desired[1], True)
            except Exception as exc:
                self._errors.append(str(exc))

        def _invalidated(self):
            try:
                if self._render_thread is None:
                    self._render_thread = get_ident()
                self._assert_render()
                with self._lock:
                    if self._pending is not None and self._pending[3] is not None:
                        budget.unpin(self._pending[3])
                    self._pending = self._desired = None
                    self._scene_epoch += 1
                    self._latest = self._frame_ready = self._last_delivered = None
                    self.ready = False
                self._store.clear()
                for provider in self._providers:
                    if provider is not None and shiboken6.isValid(provider):
                        shiboken6.delete(provider)
                self._providers = [None] * 4
                self._invalidations += 1
                if self._registered and (self._closed or not shiboken6.isValid(self)):
                    budget.unregister(self._store.owner)
                    self._registered = False
                self._observe()
                if not self._closed and shiboken6.isValid(self):
                    self._status.emit(
                        {
                            "invalidated": True,
                            "source_epoch": self._source_epoch,
                            "scene_epoch": self._scene_epoch,
                        }
                    )
            except Exception as exc:
                self._errors.append(str(exc))

        def snapshot(self):
            with self._lock:
                return {
                    **self._snapshot,
                    "errors": list(self._errors),
                    "budget": budget.statistics(),
                }

    return Context()
