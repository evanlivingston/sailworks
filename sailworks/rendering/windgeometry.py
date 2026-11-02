"""Screen-space GPU stroke meshes. No weather sampling or source selection."""

import math

import numpy as np


def unit_scale_texture(image_width, image_height, target_width, target_height, dpr):
    return math.isclose(
        target_width * dpr, image_width, rel_tol=1e-12
    ) and math.isclose(target_height * dpr, image_height, rel_tol=1e-12)


def stroke_mesh(lines, dots, circle_segments=64):
    """Flat-cap unit-width lines and radius-two rings; immutable float32 vertices."""
    chunks = []
    if lines:
        segments = np.asarray(lines, dtype=np.float64).reshape(-1, 2, 2)
        direction = segments[:, 1] - segments[:, 0]
        length = np.linalg.norm(direction, axis=1)
        segments, direction, length = (
            segments[length > 0],
            direction[length > 0],
            length[length > 0],
        )
        normal = np.column_stack((-direction[:, 1], direction[:, 0])) * (
            0.5 / length[:, None]
        )
        a, b = segments[:, 0] + normal, segments[:, 0] - normal
        c, d = segments[:, 1] + normal, segments[:, 1] - normal
        chunks.append(np.stack((a, b, c, c, b, d), axis=1).reshape(-1, 2))
    if dots:
        angles = np.arange(circle_segments + 1) * (2 * np.pi / circle_segments)
        directions = np.column_stack((np.cos(angles), np.sin(angles)))
        centers = np.asarray(dots)[:, None, :]
        outer, inner = centers + 2.5 * directions, centers + 1.5 * directions
        a, b, c, d = outer[:, :-1], inner[:, :-1], outer[:, 1:], inner[:, 1:]
        chunks.append(np.stack((a, b, c, c, b, d), axis=2).reshape(-1, 2))
    result = np.ascontiguousarray(
        np.concatenate(chunks) if chunks else np.empty((0, 2)), dtype=np.float32
    )
    result.setflags(write=False)
    return result


def triangle_mesh(triangles):
    result = np.array(triangles, dtype=np.float32).reshape(-1, 2).copy()
    result.setflags(write=False)
    return result
