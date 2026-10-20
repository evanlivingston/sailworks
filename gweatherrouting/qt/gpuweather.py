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
        # The resident field's images are retained under their field identity
        # and outlive the per-view selection. Stable URLs allow the current
        # QML images to retain textures; this is not hardware-upload telemetry.
        self._fields = {}
        self._field_urls = {}
        self._field_bytes = 0
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
        size = sum(image.sizeInBytes() for image in images)
        if size > self.byte_limit:
            raise ValueError("Resident field exceeds the staging memory budget")
        with self._lock:
            existing = self._field_urls.get(key)
            if existing is not None:
                # This CPU publication already exists. Returning stable URLs
                # does not prove that Qt retained an inactive GPU texture.
                return existing
            self.field_publications += 1
            names = [f"f/{key}/{_digest(image)}" for image in images]
            for name, image in zip(names, images):
                if name not in self._fields:
                    self._field_bytes += image.sizeInBytes()
                self._fields[name] = QImage(image)
            urls = tuple(f"image://weather/{name}" for name in names)
            self._field_urls[key] = urls
            self._trim_fields()
            return urls

    def publish_selection(self, images, key):
        """Publish the current-view selection; its URLs change with the view."""
        size = sum(image.sizeInBytes() for image in images)
        if size > self.byte_limit:
            raise ValueError("Selection exceeds the staging memory budget")
        with self._lock:
            self.selection_publications += 1
            names = [f"s/{key}/{_digest(image)}" for image in images]
            self._images = {name: QImage(image) for name, image in zip(names, images)}
            return tuple(f"image://weather/{name}" for name in names)

    def _trim_fields(self):
        """Release the oldest retained field when over the budget.

        A field is released as a whole: its images are one GPU resource and
        must not be released one by one.
        """
        while self._field_bytes > self.byte_limit and len(self._field_urls) > 1:
            oldest = next(iter(self._field_urls))
            self._field_urls.pop(oldest, None)
            for name in [
                name for name in self._fields if name.startswith(f"f/{oldest}/")
            ]:
                image = self._fields.pop(name)
                self._field_bytes -= image.sizeInBytes()

    def field_bytes(self):
        with self._lock:
            return self._field_bytes

    def clear(self):
        with self._lock:
            self._images.clear()
            self._fields.clear()
            self._field_urls.clear()
            self._field_bytes = 0

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
