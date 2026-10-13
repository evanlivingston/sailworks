"""Batched wet-corner fallback with scalar source and corner precedence."""

import numpy as np
from pyproj import Geod

from .models import QueryResult, canonical_json

GEOD = Geod(ellps="WGS84")


def candidates(field, positions, times):
    """Return at most four corners per query, in scalar distance/tie order."""
    brackets = []
    for axis, values in zip(
        (field.times, field.latitudes, field.longitudes),
        (times, positions[:, 0], positions[:, 1]),
    ):
        right = np.searchsorted(axis, values)
        left = np.where(axis[right] == values, right, right - 1)
        brackets.append((left, right))
    t0, t1 = brackets[0]
    y0, y1 = brackets[1]
    x0, x1 = brackets[2]
    ys = np.column_stack((y0, y0, y1, y1))
    xs = np.column_stack((x0, x1, x0, x1))
    targets = np.stack((field.latitudes[ys], field.longitudes[xs]), axis=2)
    a = np.stack(
        (field.east[t0[:, None], ys, xs], field.north[t0[:, None], ys, xs]), axis=2
    )
    b = np.stack(
        (field.east[t1[:, None], ys, xs], field.north[t1[:, None], ys, xs]), axis=2
    )
    origins = np.broadcast_to(positions[:, None, :], targets.shape)
    distances = np.asarray(
        GEOD.inv(
            origins[:, :, 1].ravel(),
            origins[:, :, 0].ravel(),
            targets[:, :, 1].ravel(),
            targets[:, :, 0].ravel(),
        )[2]
    ).reshape(-1, 4)
    good = (
        np.isfinite(a).all(axis=2)
        & np.isfinite(b).all(axis=2)
        & (distances <= field.resolution_m)
        & ((field.times[t1] - field.times[t0]) <= 3600)[:, None]
    )
    # Scalar sorted(set(indices)) visits each exact grid edge only once.
    good[:, 1] &= x0 != x1
    good[:, 2] &= y0 != y1
    good[:, 3] &= (x0 != x1) & (y0 != y1)
    distances = np.where(good, distances, np.inf)
    # Corners already have latitude/longitude order, as in the scalar tuple tie.
    order = np.argsort(distances, axis=1, kind="stable")
    width = field.times[t1] - field.times[t0]
    weight = np.divide(
        times - field.times[t0], width, out=np.zeros(len(times)), where=width != 0
    )
    values = a + weight[:, None, None] * (b - a)
    return targets, distances, order, values


def field_nearest(field, positions, times, valid_many, check_cancel):
    targets, distances, order, values = candidates(field, positions, times)
    pending = np.arange(len(positions))
    result = {}
    for rank in range(4):
        if check_cancel is not None:
            check_cancel()
        corners = order[pending, rank]
        possible = np.isfinite(distances[pending, corners])
        ids, corners = pending[possible], corners[possible]
        if not len(ids):
            break
        accepted = np.asarray(
            valid_many(positions[ids], targets[ids, corners]), dtype=bool
        )
        for i, corner in zip(ids[accepted], corners[accepted]):
            result[int(i)] = QueryResult(
                "available",
                field.id,
                tuple(values[i, corner]),
                qc_flags=("nearest_wet_cell",),
                detail=canonical_json(
                    dict(
                        latitude=float(targets[i, corner, 0]),
                        longitude=float(targets[i, corner, 1]),
                        distance_m=float(distances[i, corner]),
                    )
                ),
            )
        pending = ids[~accepted]
        if not len(pending):
            break
    return result


def resolve_nearest(selection, positions, times, indices, check_cancel=None):
    """Try fields in unchanged priority order, validating each corner rank in batches."""
    positions = np.array(positions, dtype=float, copy=True)
    positions[:, 1] = (positions[:, 1] + 180) % 360 - 180
    pending = np.asarray(indices)
    result = {}
    for field in selection.fields:
        if check_cancel is not None:
            check_cancel()
        if not len(pending):
            break
        p = positions[pending]
        covered = (
            (times[pending] >= field.times[0])
            & (times[pending] <= field.times[-1])
            & (p[:, 0] >= field.latitudes[0])
            & (p[:, 0] <= field.latitudes[-1])
            & (p[:, 1] >= field.longitudes[0])
            & (p[:, 1] <= field.longitudes[-1])
        )
        ids = pending[covered]
        if not len(ids):
            continue
        resolved = field_nearest(
            field,
            positions[ids],
            times[ids],
            selection.neighbor_valid_many,
            check_cancel,
        )
        for local, answer in resolved.items():
            result[int(ids[local])] = answer
        if resolved:
            selection.used.add(field.id)
            pending = pending[~np.isin(pending, ids[list(resolved)])]
    return result
