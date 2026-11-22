"""Batched RK2 propagation; the scalar integrator remains the reference."""

import math
from datetime import datetime, timezone

import numpy as np
from weatherrouting import IsoPoint

from .chs import DownloadCancelledError
from .routing import GEOD, KNOT, routing_utc


def epoch_us(value):
    """Convert a datetime to integer microseconds without a float round trip."""
    utc = routing_utc(value)
    delta = utc - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


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
    from .frontierrouting import ArrayEnvironment, restore_time

    headings = np.asarray(headings, dtype=float)
    count = len(headings)
    positions = np.tile(point.pos, (count, 1)).astype(float)
    times = np.full(count, epoch_us(point.time), dtype=np.int64)
    elapsed = np.zeros(count)
    alive = np.ones(count, dtype=bool)
    samples: list = [[] for _ in headings]
    last_velocity = np.zeros((count, 7))
    gap_map: dict = {}
    numeric = ArrayEnvironment(router.environment)

    # Per-substep scratch buffers, written only at `ids` and read through the
    # returned id arrays, so rows outside the candidate set may stay stale.
    v = np.zeros((count, 7))
    mv = np.zeros((count, 7))
    duration = np.empty(count)
    midpoint = np.empty((count, 2))
    destination = np.empty((count, 2))

    def velocities(ids, pos, clock, out):
        from sailworks.core.safetypolicy import policy_of

        policy = policy_of(router.polar)
        result = numeric.sample(ids, pos[ids], clock, gap_map)
        metadata = {}
        kept = []
        for j, i in enumerate(ids):
            if not result.valid[j]:
                continue
            wd, ws = result.values[j, 0], result.values[j, 1]
            ce, cn = result.values[j, 2], result.values[j, 3]
            if policy.is_nogo(ws / KNOT):
                router.environment.policy_pruned += 1
                continue
            angle = abs((headings[i] - wd + math.pi) % (2 * math.pi) - math.pi)
            speed = router.polar.get_speed(ws / KNOT, angle) * KNOT
            if not math.isfinite(speed) or speed < 0:
                raise ValueError("The boat polar returned an invalid speed")
            out[i] = (0, 0, speed, wd, ws, ce, cn)
            metadata[i] = result.metadata_at(j)
            kept.append(i)
        kept = np.asarray(kept, dtype=int)
        out[kept, 0] = out[kept, 2] * np.sin(headings[kept]) + out[kept, 5]
        out[kept, 1] = out[kept, 2] * np.cos(headings[kept]) + out[kept, 6]
        return kept, out, metadata

    while True:
        if router.environment.cancel.is_set():
            raise DownloadCancelledError()
        distance = distances(positions, end)
        ids = np.flatnonzero(alive & (elapsed < seconds) & (distance > 1.0))
        if not len(ids):
            break
        alive[ids] = False
        ids, v, vm = velocities(ids, positions, times[ids], v)
        if not len(ids):
            continue
        sog = np.maximum(np.hypot(v[ids, 0], v[ids, 1]), 0.01)
        duration[ids] = np.minimum.reduce(
            (
                np.full(len(ids), 300),
                seconds - elapsed[ids],
                router.resolution / (2 * sog),
                distance[ids] / sog,
            )
        )
        midpoint[ids] = move_many(positions[ids], v[ids], duration[ids] / 2)
        ids = ids[valid_many(router, positions[ids], midpoint[ids])]
        if not len(ids):
            continue
        mt = times[ids] + np.rint(duration[ids] * 1e6 / 2).astype(np.int64)
        ids, mv, mm = velocities(ids, midpoint, mt, mv)
        if not len(ids):
            continue
        destination[ids] = move_many(positions[ids], mv[ids], duration[ids])
        ids = ids[valid_many(router, midpoint[ids], destination[ids])]
        if not len(ids):
            continue
        arrival = times[ids] + np.rint(duration[ids] * 1e6).astype(np.int64)
        endpoint = numeric.sample(ids, destination[ids], arrival, gap_map)
        for j, i in enumerate(ids):
            if not endpoint.valid[j]:
                continue
            samples[i].extend(
                (
                    dict(vm[i]),
                    endpoint.metadata_at(j),
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
            times[i] = arrival[j]
            elapsed[i] += duration[i]
            last_velocity[i] = v[i]
    result = []
    for i in np.flatnonzero(alive):
        bearing, _, travelled = GEOD.inv(
            point.pos[1], point.pos[0], positions[i, 1], positions[i, 0]
        )
        candidate = IsoPoint(
            tuple(float(v) for v in positions[i]),
            time=restore_time(times[i], point.time),
            twd=float(last_velocity[i, 3]) if samples[i] else point.twd,
            tws=float(last_velocity[i, 4]) / KNOT if samples[i] else point.tws,
            speed=float(travelled) / max(elapsed[i], 0.01) / KNOT,
            brg=float(bearing) % 360,
            next_wp_dist=float(distances(positions[i : i + 1], end)[0]) / 1852,
        )
        candidate.marine_samples = tuple(samples[i])
        result.append(candidate)
    return result, list(gap_map.values())
