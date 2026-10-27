"""Full-frontier RK2 integration with numeric samples and deferred provenance.

Heading order, adaptive intervals, exact geodesics, validity predicates and
bearing-bin ties follow CurrentIsoRouter's scalar reference. Only retained
candidates allocate IsoPoints and detailed sample dictionaries.
"""

import json
import math
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
from weatherrouting import IsoPoint, Polar

from gweatherrouting.core.polarwrapper import PolarWrapper

from .batchrouting import distances, move_many
from .batchwind import ArrayWind, instant
from .chs import DownloadCancelledError
from .fields import CurrentSamples
from .routing import (
    ARRIVAL_METRES,
    GEOD,
    KNOT,
    DataGap,
    RoutingDataGapError,
    distance_bearing,
    routing_utc,
)


def restore_time(microseconds, original):
    value = instant(microseconds)
    return (
        value.astimezone().replace(tzinfo=None)
        if original.tzinfo is None
        else value.astimezone(original.tzinfo)
    )


def array_clock_compatible(instants, seconds):
    """Keep legacy wall-clock arithmetic across local daylight-saving changes."""
    return all(
        not value.fold
        and abs(
            (value + timedelta(seconds=seconds)).timestamp()
            - value.timestamp()
            - seconds
        )
        < 1e-4
        for value in instants
    )


def polar_many(polar, ws, angles):
    """Match the scalar table's interpolation order, including extrapolation."""
    if type(polar) is PolarWrapper:
        return polar_many(polar._polar, ws, angles) * polar._efficiency
    from gweatherrouting.core.safetypolicy import SafetyPolar

    if type(polar) is SafetyPolar:
        base = polar_many(polar._polar, ws, angles)
        adjusted, _ = polar.apply_array(ws, angles, base)
        return adjusted
    if type(polar) is not Polar:
        return np.array(
            [polar.get_speed(w, a) for w, a in zip(ws, angles)], dtype=float
        )
    w = np.asarray(polar.tws)
    a = np.asarray(polar.twa)
    table = np.asarray(polar.speed_table)
    w0 = np.maximum(0, np.searchsorted(w, ws, side="right") - 1)
    w1 = np.minimum(len(w) - 1, np.searchsorted(w, ws, side="left"))
    a0 = np.maximum(0, np.searchsorted(a, angles, side="right") - 1)
    a1 = np.searchsorted(a, angles, side="left")
    a1 = np.where(a1 >= len(a), 0, a1)
    # The scalar scans upper indices from n-1 through 1, never index zero.
    w1 = np.where(ws <= w[1], 1, w1) if len(w) > 1 else w1
    a1 = np.where(angles <= a[1], 1, a1) if len(a) > 1 else a1
    w1 = np.where(w0 > w1, len(w) - 1, w1)
    s1 = table[a0, w0]
    s2 = table[a1, w0]
    s3 = table[a0, w1]
    s4 = table[a1, w1]
    da = a[a1] - a[a0]
    # Keep multiply, then divide ordering used by Polar.get_speed.
    p = (
        np.divide(
            (s2 - s1) * (angles - a[a0]), da, out=np.zeros(len(ws)), where=da != 0
        )
        + s1
    )
    q = (
        np.divide(
            (s4 - s3) * (angles - a[a0]), da, out=np.zeros(len(ws)), where=da != 0
        )
        + s3
    )
    dw = w[w1] - w[w0]
    result = p + np.divide(
        (q - p) * (ws - w[w0]), dw, out=np.zeros(len(ws)), where=dw != 0
    )
    return result


@dataclass
class ArraySamples:
    ids: np.ndarray
    positions: np.ndarray
    times_us: np.ndarray
    currents: CurrentSamples
    sources: np.ndarray
    values: np.ndarray
    valid: np.ndarray

    def metadata(self, global_id):
        j = int(np.searchsorted(self.ids, global_id))
        cur = self.currents
        result = dict(
            latitude=self.positions[j, 0],
            longitude=self.positions[j, 1],
            time=instant(self.times_us[j]).isoformat(),
            dataset_id=cur.identifier(j),
            wind_source=self.sources[j],
        )
        if j in cur.special:
            q = cur.special[j]
            if q.qc_flags:
                result["current_sampling"] = q.qc_flags
                result["current_sample_location"] = json.loads(q.detail)
        return result


class ArrayEnvironment:
    def __init__(self, env):
        self.env = env
        self.wind = ArrayWind(env.wind, env.cancel)

    def check_cancel(self):
        if self.env.cancel.is_set():
            raise DownloadCancelledError()

    def sample(self, ids, positions, times_us, gaps):
        self.check_cancel()
        currents = self.env.selection.sample_arrays(
            positions, times_us.astype(float) / 1e6, check_cancel=self.check_cancel
        )
        valid = currents.status == "available"
        rows = np.flatnonzero(valid)
        values = np.zeros((len(ids), 4))
        sources = np.full(len(ids), None, dtype=object)
        if len(rows):
            wind, src = self.wind.query_us(positions[rows], times_us[rows])
            sources[rows] = src
            ce = currents.values[rows, 0]
            cn = currents.values[rows, 1]
            radians = np.radians(wind[:, 0])
            we = -wind[:, 1] * np.sin(radians) - ce
            wn = -wind[:, 1] * np.cos(radians) - cn
            values[rows] = np.column_stack(
                (np.arctan2(-we, -wn), np.hypot(we, wn), ce, cn)
            )
            valid[rows] &= np.isfinite(wind).all(axis=1)
        for j in np.flatnonzero(~valid):
            kind, reason = (
                ("current", currents.status[j])
                if currents.status[j] != "available"
                else ("wind", "no_coverage")
            )
            gap = DataGap(tuple(positions[j]), instant(times_us[j]), kind, reason)
            gaps[(gap.position, gap.time, gap.kind)] = gap
        return ArraySamples(
            ids.copy(),
            positions.copy(),
            times_us.copy(),
            currents,
            sources,
            values,
            valid,
        )


def propagate(self, frontier, seconds, end, gaps, numeric):  # noqa: C901
    parent_ids = []
    heading_values = []
    for i, point in enumerate(frontier):
        if self.environment.cancel.is_set():
            raise DownloadCancelledError()
        try:
            hh = self.headings(point, end)
        except RoutingDataGapError as error:
            for g in error.gaps:
                gaps[(g.position, g.time, g.kind)] = g
            continue
        parent_ids.extend([i] * len(hh))
        heading_values.extend(hh)
    parents = np.asarray(parent_ids, dtype=int)
    headings = np.asarray(heading_values)
    count = len(parents)
    if not count:
        return None
    positions = np.array([frontier[i].pos for i in parents], dtype=float)
    times = np.array(
        [round(routing_utc(frontier[i].time).timestamp() * 1e6) for i in parents],
        dtype=np.int64,
    )
    elapsed = np.zeros(count)
    alive = np.ones(count, dtype=bool)
    last = np.zeros((count, 7))
    logs = []

    def velocities(ids, positions, times):
        from gweatherrouting.core.safetypolicy import policy_of

        samples = numeric.sample(ids, positions[ids], times[ids], gaps)
        kept = ids[samples.valid]
        data = samples.values[samples.valid]
        result = np.zeros((count, 7))
        angle = np.abs(
            (headings[kept] - data[:, 0] + math.pi) % (2 * math.pi) - math.pi
        )
        ws_kn = data[:, 1] / KNOT
        policy = policy_of(self.polar)
        nogo = np.asarray([policy.is_nogo(w) for w in ws_kn], dtype=bool)
        if nogo.any():
            self.environment.policy_pruned += int(nogo.sum())
            keep = ~nogo
            kept = kept[keep]
            data = data[keep]
            angle = angle[keep]
            ws_kn = ws_kn[keep]
        if not len(kept):
            return kept, result, samples
        speed = polar_many(self.polar, ws_kn, angle) * KNOT
        if (~np.isfinite(speed) | (speed < 0)).any():
            raise ValueError("Invalid polar speed")
        result[kept, 2:] = np.column_stack(
            (speed, data[:, 0], data[:, 1], data[:, 2], data[:, 3])
        )
        result[kept, 0] = speed * np.sin(headings[kept]) + data[:, 2]
        result[kept, 1] = speed * np.cos(headings[kept]) + data[:, 3]
        return kept, result, samples

    def valid(ids, a, b):
        mask = np.ones(len(ids), dtype=bool)
        if self.points_validity:
            mask &= self.points_validity(b[ids])
        if self.point_validity:
            mask &= [self.point_validity(*p) for p in b[ids]]
        kept = ids[mask]
        if len(kept) and self.lines_validity:
            ok = self.lines_validity(np.column_stack((a[kept], b[kept])))
            kept = kept[np.asarray(ok, dtype=bool)]
        if len(kept) and self.line_validity:
            kept = np.array(
                [i for i in kept if self.line_validity(*a[i], *b[i])], dtype=int
            )
        return kept

    while True:
        if self.environment.cancel.is_set():
            raise DownloadCancelledError()
        distance = distances(positions, end)
        ids = np.flatnonzero(alive & (elapsed < seconds) & (distance > 1.0))
        if self.environment.progress:
            # All headings advance together. Count parents whose trajectories
            # have finished, rather than reporting an arbitrary heading index.
            pending_parents = np.unique(parents[ids])
            best = min(
                min(p.next_wp_dist for p in frontier) * 1852,
                float(np.min(distance[alive])) if alive.any() else math.inf,
            )
            self.environment.progress(
                best, len(frontier) - len(pending_parents), len(frontier)
            )
        if not len(ids):
            break
        alive[ids] = False
        ids, v, initial = velocities(ids, positions, times)
        if not len(ids):
            continue
        sog = np.maximum(np.hypot(v[ids, 0], v[ids, 1]), 0.01)
        duration = np.zeros(count)
        duration[ids] = np.minimum.reduce(
            (
                np.full(len(ids), 300),
                seconds - elapsed[ids],
                self.resolution / (2 * sog),
                distance[ids] / sog,
            )
        )
        midpoint = positions.copy()
        midpoint[ids] = move_many(positions[ids], v[ids], duration[ids] / 2)
        ids = valid(ids, positions, midpoint)
        if not len(ids):
            continue
        mt = times.copy()
        mt[ids] += np.rint(duration[ids] * 1e6 / 2).astype(np.int64)
        ids, mv, middle = velocities(ids, midpoint, mt)
        if not len(ids):
            continue
        destination = positions.copy()
        destination[ids] = move_many(positions[ids], mv[ids], duration[ids])
        ids = valid(ids, midpoint, destination)
        if not len(ids):
            continue
        arrival = times.copy()
        arrival[ids] += np.rint(duration[ids] * 1e6).astype(np.int64)
        endpoint = numeric.sample(ids, destination[ids], arrival[ids], gaps)
        ids = ids[endpoint.valid]
        if not len(ids):
            continue
        alive[ids] = True
        positions[ids] = destination[ids]
        times[ids] = arrival[ids]
        elapsed[ids] += duration[ids]
        last[ids] = v[ids]
        accepted = np.zeros(count, dtype=bool)
        accepted[ids] = True
        logs.append((accepted, initial, middle, endpoint, mv))
    return dict(
        parents=parents,
        headings=headings,
        positions=positions,
        times=times,
        elapsed=elapsed,
        alive=alive,
        last=last,
        logs=logs,
    )


def route_frontier(self, lastlog, t, hours, start, end):  # noqa: C901
    if self.environment.cancel.is_set():
        raise DownloadCancelledError()
    isochrones = (
        list(lastlog.isochrones)
        if lastlog
        else [
            [
                IsoPoint(
                    tuple(start),
                    time=t,
                    next_wp_dist=distance_bearing(start, end)[0] / 1852,
                )
            ]
        ]
    )
    if len(isochrones) > 1008:
        raise ValueError("Routing exceeded seven days without reaching the waypoint")
    frontier = isochrones[-1]
    gaps: dict = {}
    bins: dict = {}
    winner = None
    best_distance = min(p.next_wp_dist for p in frontier) * 1852
    if self.environment.progress:
        self.environment.progress(best_distance, 0, len(frontier))
    data = propagate(
        self,
        frontier,
        min(hours * 3600, 600),
        end,
        gaps,
        ArrayEnvironment(self.environment),
    )
    if data is not None:
        ids = np.flatnonzero(data["alive"])
        p = data["positions"]
        distance = distances(p, end)
        bearings = GEOD.inv(
            np.full(len(p), start[1]), np.full(len(p), start[0]), p[:, 1], p[:, 0]
        )[0]
        keys = np.array([round(float(b) / 2) for b in bearings])
        for i in ids:
            if distance[i] <= ARRIVAL_METRES and (
                winner is None or data["times"][i] < data["times"][winner]
            ):
                winner = i
            key = int(keys[i])
            if key not in bins:
                bins[key] = [i]
            else:
                bins[key].append(i)
                bins[key].sort(key=lambda idx: distance[idx])
                if len(bins[key]) > 2:
                    bins[key] = bins[key][:2]
        if len(ids):
            best_distance = min(best_distance, float(np.min(distance[ids])))
    if self.environment.progress:
        self.environment.progress(best_distance, len(frontier), len(frontier))
    masked = [
        g
        for g in gaps.values()
        if g.kind == "current" and g.reason == "masked_or_missing"
    ]
    unresolved = [
        g
        for g in gaps.values()
        if not (g.kind == "current" and g.reason == "masked_or_missing")
    ]
    if unresolved:
        raise RoutingDataGapError(unresolved)
    self.environment.record_exclusions(masked)
    if not bins and masked:
        raise RoutingDataGapError(masked)
    if not bins and self.environment.policy_pruned:
        raise ValueError(
            "No navigable route: forecast winds exceed the configured "
            "TWS limit in the search area."
        )
    if not bins:
        raise ValueError("No navigable route through the current search area")

    def materialize(i):
        parent = int(data["parents"][i])
        point = frontier[parent]
        bearing, _, travelled = GEOD.inv(point.pos[1], point.pos[0], p[i, 1], p[i, 0])
        samples: list = []
        for accepted, initial, middle, endpoint, mv in data["logs"]:
            if not accepted[i]:
                continue
            samples.extend(
                (
                    initial.metadata(i),
                    endpoint.metadata(i),
                    dict(
                        **middle.metadata(i),
                        current_east_ms=float(mv[i, 5]),
                        current_north_ms=float(mv[i, 6]),
                        speed_through_water_kn=float(mv[i, 2]) / KNOT,
                        heading_true=math.degrees(data["headings"][i]) % 360,
                    ),
                )
            )
        result = IsoPoint(
            tuple(p[i]),
            prev_idx=parent,
            time=restore_time(data["times"][i], point.time),
            twd=float(data["last"][i, 3]) if samples else point.twd,
            tws=float(data["last"][i, 4]) / KNOT if samples else point.tws,
            speed=float(travelled) / max(data["elapsed"][i], 0.01) / KNOT,
            brg=float(bearing) % 360,
            next_wp_dist=float(distance[i]) / 1852,
        )
        result.marine_samples = tuple(samples)
        return result

    if winner is not None:
        win = materialize(winner)
        path = [win]
        for front in reversed(isochrones):
            path.append(front[path[-1].prev_idx])
        path.reverse()
        return self.result(win.time, path, isochrones + [[win]], win.pos)
    retained = [idx for group in bins.values() for idx in group]
    new = [materialize(i) for i in retained]
    return self.result(new[0].time, [], isochrones + [new], start)
