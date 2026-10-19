"""Numerical weather images for Qt Quick; no GPU objects or GDAL handles.

Each float32 occupies two opaque RGB texels. Unlike per-image normalization,
this preserves physical units, NaNs and the complete float32 bit pattern.
Opaque texels avoid Qt's alpha premultiplication corrupting numerical bytes.
Images must be sampled with nearest filtering and decoded *before* blending.
"""

from dataclasses import dataclass
from datetime import datetime
from threading import Lock

import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider


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
    """

    def __init__(self, byte_limit=32 * 1024 * 1024):
        super().__init__(QQuickImageProvider.ImageType.Image)
        self.byte_limit = byte_limit
        self._lock = Lock()
        self._generation = 0
        self._images = {}
        self.requests = 0

    def publish(self, pair):
        if pair.nbytes > self.byte_limit:
            raise ValueError("Weather pair exceeds staging memory budget")
        with self._lock:
            self._generation += 1
            self._images = {
                f"{self._generation}/{i}": QImage(image)
                for i, image in enumerate(pair.images)
            }
            return tuple(f"image://weather/{key}" for key in self._images)

    def clear(self):
        with self._lock:
            self._images.clear()

    def requestImage(self, identifier, size, requested_size):  # noqa: N802
        with self._lock:
            self.requests += 1
            image = QImage(self._images.get(identifier, QImage()))
        if size is not None:
            size.setWidth(image.width())
            size.setHeight(image.height())
        return image
