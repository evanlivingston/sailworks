"""Batched RK2 propagation; the scalar integrator remains the reference."""

import math
from datetime import timedelta

import numpy as np
from weatherrouting import IsoPoint

from .chs import DownloadCancelledError
from .routing import ARRIVAL_METRES, GEOD, KNOT, RoutingDataGapError


def move_many(positions, velocities, duration):
    lon, lat, _ = GEOD.fwd(
        positions[:, 1],
        positions[:, 0],
        np.degrees(np.arctan2(velocities[:, 0], velocities[:, 1])),
        np.hypot(velocities[:, 0], velocities[:, 1]) * duration,
    )
    return np.column_stack((lat, lon))


def distances(positions, end):
    return GEOD.inv(
        positions[:, 1],
        positions[:, 0],
        np.full(len(positions), end[1]),
        np.full(len(positions), end[0]),
    )[2]


def velocities(router, ids, positions, times, headings, gaps):
    from gweatherrouting.core.safetypolicy import policy_of

    policy = policy_of(router.polar)
    values = np.zeros((len(positions), 7))
    metadata = {}
    sampled = router.environment.sample_many(positions[ids], [times[i] for i in ids])
    valid = []
    for i, sample in zip(ids, sampled):
        if isinstance(sample, RoutingDataGapError):
            gaps.extend(sample.gaps)
            continue
        (wd, ws, ce, cn, _), meta = sample
        if policy.is_nogo(ws / KNOT):
            router.environment.policy_pruned += 1
            continue
        angle = abs((headings[i] - wd + math.pi) % (2 * math.pi) - math.pi)
        speed = router.polar.get_speed(ws / KNOT, angle) * KNOT
        if not math.isfinite(speed) or speed < 0:
            raise ValueError("The boat polar returned an invalid speed")
        values[i] = (0, 0, speed, wd, ws, ce, cn)
        metadata[i] = meta
        valid.append(i)
    ids = np.asarray(valid, dtype=int)
    values[ids, 0] = values[ids, 2] * np.sin(headings[ids]) + values[ids, 5]
    values[ids, 1] = values[ids, 2] * np.cos(headings[ids]) + values[ids, 6]
    return ids, values, metadata


def valid_many(router, starts, ends):
    valid = np.ones(len(ends), dtype=bool)
    if router.points_validity:
        valid &= router.points_validity([tuple(p) for p in ends])
    if router.point_validity:
        valid &= [router.point_validity(*p) for p in ends]
    ids = np.flatnonzero(valid)
    if len(ids) and router.lines_validity:
        valid[ids] &= router.lines_validity(
            [list(starts[i]) + list(ends[i]) for i in ids]
        )
    ids = np.flatnonzero(valid)
    if len(ids) and router.line_validity:
        valid[ids] &= [router.line_validity(*starts[i], *ends[i]) for i in ids]
    return valid


def advance_headings(router, point, seconds, headings, end):  # noqa: C901
    """Advance independent headings together, preserving individual step lengths."""
    headings = np.asarray(headings, dtype=float)
    count = len(headings)
    positions = np.tile(point.pos, (count, 1)).astype(float)
    times = [point.time] * count
    elapsed = np.zeros(count)
    alive = np.ones(count, dtype=bool)
    samples: list = [[] for _ in headings]
    last_velocity = np.zeros((count, 7))
    gaps: list = []
    while True:
        if router.environment.cancel.is_set():
            raise DownloadCancelledError()
        distance = distances(positions, end)
        ids = np.flatnonzero(alive & (elapsed < seconds) & (distance > ARRIVAL_METRES))
        if not len(ids):
            break
        alive[ids] = False
        ids, v, vm = velocities(router, ids, positions, times, headings, gaps)
        if not len(ids):
            continue
        sog = np.maximum(np.hypot(v[ids, 0], v[ids, 1]), 0.01)
        duration = np.zeros(count)
        duration[ids] = np.minimum.reduce(
            (
                np.full(len(ids), 300),
                seconds - elapsed[ids],
                router.resolution / (2 * sog),
                distance[ids] / sog,
            )
        )
        midpoint = positions.copy()
        midpoint[ids] = move_many(positions[ids], v[ids], duration[ids] / 2)
        ids = ids[valid_many(router, positions[ids], midpoint[ids])]
        if not len(ids):
            continue
        mt = list(times)
        for i in ids:
            mt[i] = times[i] + timedelta(seconds=float(duration[i]) / 2)
        ids, mv, mm = velocities(router, ids, midpoint, mt, headings, gaps)
        if not len(ids):
            continue
        destination = positions.copy()
        destination[ids] = move_many(positions[ids], mv[ids], duration[ids])
        ids = ids[valid_many(router, midpoint[ids], destination[ids])]
        if not len(ids):
            continue
        arrival = list(times)
        for i in ids:
            arrival[i] = times[i] + timedelta(seconds=float(duration[i]))
        endpoint = router.environment.sample_many(
            destination[ids], [arrival[i] for i in ids]
        )
        for i, sample in zip(ids, endpoint):
            if isinstance(sample, RoutingDataGapError):
                gaps.extend(sample.gaps)
                continue
            _, metadata = sample
            samples[i].extend(
                (
                    dict(vm[i]),
                    dict(metadata),
                    dict(
                        **mm[i],
                        current_east_ms=float(mv[i, 5]),
                        current_north_ms=float(mv[i, 6]),
                        speed_through_water_kn=float(mv[i, 2]) / KNOT,
                        heading_true=math.degrees(headings[i]) % 360,
                    ),
                )
            )
            alive[i] = True
            positions[i] = destination[i]
            times[i] = arrival[i]
            elapsed[i] += duration[i]
            last_velocity[i] = v[i]
    result = []
    for i in np.flatnonzero(alive):
        bearing, _, travelled = GEOD.inv(
            point.pos[1], point.pos[0], positions[i, 1], positions[i, 0]
        )
        candidate = IsoPoint(
            tuple(float(v) for v in positions[i]),
            time=times[i],
            twd=float(last_velocity[i, 3]) if samples[i] else point.twd,
            tws=float(last_velocity[i, 4]) / KNOT if samples[i] else point.tws,
            speed=float(travelled) / max(elapsed[i], 0.01) / KNOT,
            brg=float(bearing) % 360,
            next_wp_dist=float(distances(positions[i : i + 1], end)[0]) / 1852,
        )
        candidate.marine_samples = tuple(samples[i])
        result.append(candidate)
    return result, gaps
