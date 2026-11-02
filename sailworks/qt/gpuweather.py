"""Numerical weather images for Qt Quick; no GPU objects or GDAL handles.

Each float32 occupies two opaque RGB texels. Unlike per-image normalization,
this preserves physical units, NaNs and the complete float32 bit pattern.
Opaque texels avoid Qt's alpha premultiplication corrupting numerical bytes.
Images must be sampled with nearest filtering and decoded *before* blending.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
from threading import Lock

import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider


def _digest(image):
    """A stable digest of the bytes an image serves."""
    return hashlib.sha256(image.constBits().tobytes()).hexdigest()[:16]


def encode_component(values):
    values = np.asarray(values, dtype="<f4")
    if values.ndim != 2 or not values.size:
        raise ValueError("Expected a nonempty two-dimensional component")
    height, width = values.shape
    bits = np.ascontiguousarray(values).view("<u4")
    pixels = np.zeros((height, width, 2, 4), dtype=np.uint8)
    for channel in range(3):
        pixels[:, :, 0, channel] = (bits >> (8 * channel)) & 255
    pixels[:, :, 1, 0] = bits >> 24
    pixels[:, :, :, 3] = 255
    return QImage(
        pixels.data,
        width * 2,
        height,
        width * 8,
        QImage.Format.Format_RGBA8888,
    ).copy()


def decode_component(image):
    """CPU reference for the shader byte decoder (also checks transport)."""
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(
        image.height(), image.width() // 2, 2, 4
    )
    bits = pixels[:, :, 1, 0].astype(np.uint32) << 24
    for channel in range(3):
        bits |= pixels[:, :, 0, channel].astype(np.uint32) << (8 * channel)
    return bits.view(np.float32).copy()


@dataclass(frozen=True)
class WeatherTexturePair:
    """Worker-produced immutable snapshot for one compatible forecast pair.

    Components are true east/north m/s on the *same* spatial grid. Selection,
    projection and vector rotation belong to the staging worker, not the GUI.
    This transport deliberately does not select GRIB files or round timestamps.
    """

    before: datetime
    after: datetime
    images: tuple
    shape: tuple

    @classmethod
    def from_arrays(cls, before, after, u0, v0, u1, v1):
        if after < before:
            raise ValueError("Forecast endpoints are reversed")
        arrays = tuple(np.asarray(a) for a in (u0, v0, u1, v1))
        shape = arrays[0].shape
        if any(a.shape != shape for a in arrays):
            raise ValueError("Forecast components must share a grid")
        return cls(before, after, tuple(map(encode_component, arrays)), shape)

    def weight(self, instant):
        if not self.before <= instant <= self.after:
            raise ValueError("Time is outside the staged forecast pair")
        duration = (self.after - self.before).total_seconds()
        return (instant - self.before).total_seconds() / duration if duration else 0.0

    @property
    def nbytes(self):
        return sum(image.sizeInBytes() for image in self.images)


class WeatherImageProvider(QQuickImageProvider):
    """Bounded CPU staging; Qt owns upload and render-thread texture disposal.

    Publish on the GUI thread; requestImage may execute on a Qt loader thread.
    Only one pair is retained. URLs change only when a new pair is published,
    never when the time uniform changes. Reject oversized pairs before use.

    A named publication keeps the pair under a stable key instead of replacing
    it: the URLs of a resident field do not change, so a field that is still
    resident is not uploaded again.  The retained pairs share their pixels
    with the field store that owns them, so the accounted bytes are counted
    once.
    """

    def __init__(self, byte_limit=32 * 1024 * 1024):
        super().__init__(QQuickImageProvider.ImageType.Image)
        self.byte_limit = byte_limit
        self._lock = Lock()
        self._generation = 0
        self._images = {}
        self._pinned_selection = {}
        # The resident field's images are retained under their field identity
        # and outlive the per-view selection. Stable URLs allow the current
        # QML images to retain textures; this is not hardware-upload telemetry.
        self._fields = {}
        self._field_urls = {}
        self._field_bytes = 0
        # Each consumer has at most one displayed and one pending field.
        self._reservations = {}
        self.requests = 0
        self.publishes = 0
        self.field_publications = 0
        self.selection_publications = 0

    def publish(self, pair, key=None):
        """Publish ``pair`` and return the URLs of its images.

        Without a key the historical generation scheme is used: every
        publication replaces the retained pair and every URL changes.  With a
        key the URLs are derived from the bytes they serve, so the same field
        always publishes the same URLs and an image that is already resident
        on the GPU is never uploaded a second time.
        """
        if pair.nbytes > self.byte_limit:
            raise ValueError("Weather pair exceeds staging memory budget")
        with self._lock:
            self.publishes += 1
            if key is None:
                self._generation += 1
                names = [
                    f"{self._generation}/{index}" for index in range(len(pair.images))
                ]
            else:
                names = [f"{key}/{_digest(image)}" for image in pair.images]
            self._images = {
                name: QImage(image) for name, image in zip(names, pair.images)
            }
            return tuple(f"image://weather/{name}" for name in names)

    def publish_field(self, images, key):
        """Publish a resident field's endpoint images under its identity.

        The URLs are derived from the bytes they serve and the field's
        identity, so republishing the same field keeps its URLs. Qt owns
        texture lifetimes; a return after switching fields may upload again.
        A field that does not fit the budget is
        rejected: it is not published and not retained.
        """
        with self._lock:
            return self._publish_field_locked(images, key, self._protected_fields())

    def reserve_field(self, owner, images, key):
        """Reserve a pending field without disturbing any consumer's front.

        A successful replacement cancels that owner's previous pending field;
        failure leaves both reservations and retained images unchanged.
        """
        with self._lock:
            front, _pending = self._reservations.get(owner, (None, None))
            protected = self._protected_fields(exclude_pending=owner)
            urls = self._publish_field_locked(images, key, protected)
            self._reservations[owner] = (front, key)
            return urls

    def promote_field(self, owner):
        """Promote the pending reservation to front after presentation swaps."""
        with self._lock:
            _front, pending = self._reservations.get(owner, (None, None))
            if pending is None:
                raise ValueError("No pending field reservation")
            self._reservations[owner] = (pending, None)
            self._trim_fields()

    def cancel_field(self, owner):
        """Cancel a pending reservation without releasing the front."""
        with self._lock:
            front, _pending = self._reservations.get(owner, (None, None))
            if front is None:
                self._reservations.pop(owner, None)
            else:
                self._reservations[owner] = (front, None)
            self._trim_fields()

    def release_fields(self, owner):
        """Release both reservations belonging to a consumer."""
        with self._lock:
            self._reservations.pop(owner, None)
            self._trim_fields()

    _NO_OWNER = object()

    def _protected_fields(self, exclude_pending=_NO_OWNER):
        protected = set()
        for owner, (front, pending) in self._reservations.items():
            if front is not None:
                protected.add(front)
            if pending is not None and owner != exclude_pending:
                protected.add(pending)
        return protected

    def _publish_field_locked(self, images, key, protected):
        existing = self._field_urls.get(key)
        names = (
            [url.removeprefix("image://weather/") for url in existing]
            if existing is not None
            else [f"f/{key}/{_digest(image)}" for image in images]
        )
        new_images = dict(zip(names, images)) if existing is None else {}
        required = set(names)
        for protected_key in protected:
            required.update(
                url.removeprefix("image://weather/")
                for url in self._field_urls[protected_key]
            )
        size = sum(
            (
                self._fields[name] if name in self._fields else new_images[name]
            ).sizeInBytes()
            for name in required
        )
        if size > self.byte_limit:
            raise ValueError("Resident fields exceed the staging memory budget")
        if existing is None:
            additional = sum(
                image.sizeInBytes()
                for name, image in new_images.items()
                if name not in self._fields
            )
            # Free unreserved fields *before* storing new images, so even the
            # transient CPU footprint remains inside the configured limit.
            while self._field_bytes + additional > self.byte_limit:
                oldest = next(
                    (
                        old_key
                        for old_key in self._field_urls
                        if old_key not in protected and old_key != key
                    ),
                    None,
                )
                if oldest is None:
                    raise ValueError("Resident fields exceed the staging memory budget")
                self._drop_field(oldest)
            self.field_publications += 1
            for name, image in new_images.items():
                if name not in self._fields:
                    self._fields[name] = QImage(image)
                    self._field_bytes += image.sizeInBytes()
            existing = tuple(f"image://weather/{name}" for name in names)
            self._field_urls[key] = existing
        self._trim_fields(protected | {key})
        return existing

    def publish_selection(self, images, key):
        """Publish the current-view selection; its URLs change with the view."""
        size = sum(image.sizeInBytes() for image in images)
        if size > self.byte_limit:
            raise ValueError("Selection exceeds the staging memory budget")
        with self._lock:
            self.selection_publications += 1
            names = [f"s/{key}/{_digest(image)}" for image in images]
            self._images = dict(self._pinned_selection)
            self._images.update(
                (name, QImage(image)) for name, image in zip(names, images)
            )
            return tuple(f"image://weather/{name}" for name in names)

    def pin_selection(self, urls):
        """Keep the displayed selection servable while a replacement loads."""
        with self._lock:
            self._pinned_selection = {
                name: self._images[name]
                for url in urls
                if (name := url.removeprefix("image://weather/")) in self._images
            }
            self._images = dict(self._pinned_selection)

    def _trim_fields(self, protected=None):
        """Release the oldest retained field when over the budget.

        A field is released as a whole: its images are one GPU resource and
        must not be released one by one.
        """
        protected = self._protected_fields() if protected is None else protected
        while self._field_bytes > self.byte_limit:
            oldest = next(
                (key for key in self._field_urls if key not in protected), None
            )
            if oldest is None:
                break
            self._drop_field(oldest)

    def _drop_field(self, key):
        urls = self._field_urls.pop(key)
        retained = {
            url for field_urls in self._field_urls.values() for url in field_urls
        }
        for name in set(url.removeprefix("image://weather/") for url in urls):
            if f"image://weather/{name}" in retained:
                continue
            image = self._fields.pop(name)
            self._field_bytes -= image.sizeInBytes()

    def field_bytes(self):
        with self._lock:
            return self._field_bytes

    def clear(self):
        with self._lock:
            self._images.clear()
            self._pinned_selection.clear()
            self._fields.clear()
            self._field_urls.clear()
            self._field_bytes = 0
            self._reservations.clear()

    def requestImage(self, identifier, size, requested_size):  # noqa: N802
        with self._lock:
            self.requests += 1
            # The resident field's images outlive the current selection, so
            # both namespaces are served.
            image = QImage(
                self._fields.get(identifier, self._images.get(identifier, QImage()))
            )
        if size is not None:
            size.setWidth(image.width())
            size.setHeight(image.height())
        return image
