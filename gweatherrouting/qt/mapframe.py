"""Immutable, GUI-produced presentation snapshots; no dataset handles cross here."""

from dataclasses import dataclass

import numpy as np
from PySide6.QtGui import QImage

from gweatherrouting.rendering.windgeometry import stroke_mesh, triangle_mesh


@dataclass(frozen=True)
class ImageFrame:
    image: QImage
    target: tuple[float, float, float, float]
    smooth: bool = False

    def __post_init__(self):
        object.__setattr__(self, "image", QImage(self.image))


@dataclass(frozen=True)
class WindBatch:
    bucket: int
    kind: int
    vertices: np.ndarray

    def __post_init__(self):
        vertices = np.asarray(self.vertices)
        if (
            vertices.dtype != np.float32
            or not vertices.flags.c_contiguous
            or vertices.flags.writeable
        ):
            vertices = np.array(vertices, dtype=np.float32, order="C", copy=True)
        vertices.setflags(write=False)
        object.__setattr__(self, "vertices", vertices)

    @property
    def key(self):
        return self.bucket, self.kind


@dataclass(frozen=True)
class MapFrame:
    sequence: int
    width: int
    height: int
    dpr: float
    sea_color: tuple
    base: ImageFrame
    navigation: ImageFrame
    wind: tuple[WindBatch, ...]
    opacity: float
    raster_wind: ImageFrame | None = None
    numerical_wind: object = None


def prepare_wind(geometry):
    batches = []
    for bucket, (lines, triangles, dots) in sorted(geometry.items()):
        if lines or dots:
            batches.append(WindBatch(bucket, 0, stroke_mesh(lines, dots)))
        if triangles:
            batches.append(WindBatch(bucket, 1, triangle_mesh(triangles)))
    return tuple(batches)


def resource_estimate(frame, layer_keys, window_pixels):
    """Conservative explicit-resource envelope, not measured VRAM or total RSS."""
    image_layers = (frame.base, frame.navigation) + (
        (frame.raster_wind,) if frame.raster_wind else ()
    )
    images = {
        layer.image.cacheKey(): layer.image.sizeInBytes() for layer in image_layers
    }
    image_bytes = sum(images.values())
    vertex_bytes = sum(batch.vertices.nbytes for batch in frame.wind)
    numerical = getattr(frame, "numerical_wind", None)
    staged_bytes = 0
    if numerical is not None:
        atlas, _blend = numerical
        image_bytes += atlas.nbytes
        rows, cols = atlas.pair.shape
        # Four vertices per symbol, connector indices and old/new buffers.
        vertex_bytes += rows * cols * (4 * 32 + 24)
        staged_bytes = atlas.staged.first.nbytes + atlas.staged.second.nbytes
    pixels = int(np.ceil(frame.width * frame.dpr)) * int(
        np.ceil(frame.height * frame.dpr)
    )
    layer_count = len(set(layer_keys) | {batch.key for batch in frame.wind})
    # Resolved color + 4x MSAA per wind layer and QQuickWidget target. Include
    # old/new image and vertex storage and a whole-window triple-buffer estimate.
    return {
        "cpu_scene_bytes": 2 * (image_bytes + vertex_bytes + staged_bytes),
        "upload_bytes": image_bytes + vertex_bytes,
        "gpu_estimate_bytes": (
            2 * (image_bytes + vertex_bytes)
            + (layer_count + 1) * pixels * 4 * 5
            + window_pixels * 4 * 3
        ),
        "wind_layers": layer_count,
    }
