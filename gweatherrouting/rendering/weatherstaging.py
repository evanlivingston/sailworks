"""Worker-side temporal staging for numerical display textures.

No routing or shared sampler behavior changes. Within an open interval between
metadata events, source selection and valid before/after records are constant
at each spatial query point. Evaluate their linear functions at the interval
edges once; the GPU can then interpolate continuously inside that interval.
At an event itself, build a singleton snapshot to preserve exact-time mask and
new-cycle selection semantics. Never interpolate across a model-run switch.
"""

from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

import numpy as np

# The declared ceiling of the resident per-node sample memo.  It retains the
# endpoint pair and provenance of native nodes already sampled for one
# (source set, time window), so a re-centred field only samples the nodes the
# previous window did not contain.  This is a declared budget, not a measured
# optimum; the per-entry byte cost is an estimate that includes the two-key
# tuple overhead.
SAMPLING_MEMO_BYTES = 16 * 1024 * 1024
SAMPLING_ENTRY_BYTES = 112
LAND_ENTRY_BYTES = 72


class NodeSampleMemo:
    """A bounded memo of per-node sampled endpoints and land decisions.

    The memo is a pure function of its inputs: it stores the exact float32
    endpoint pair ``stage_wind`` computed for a node, so restoring it is
    bit-identical to sampling that node again.  A missing endpoint is never
    stored, so a hole still follows the sampler's fallback every time.  Land
    flags are stored only for the shoreline quality they were computed for.
    Access is on the owning worker thread only.
    """

    def __init__(self, byte_limit=SAMPLING_MEMO_BYTES):
        if byte_limit <= 0:
            raise ValueError("Sampling memo budget must be positive")
        self.byte_limit = byte_limit
        self._entries = OrderedDict()
        self.current_bytes = self.peak_bytes = 0
        self.hits = self.misses = self.evictions = 0

    def _get(self, key, entry_bytes):
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry[0]

    def _put(self, key, value, entry_bytes):
        old = self._entries.pop(key, None)
        if old is not None:
            self.current_bytes -= old[1]
        while self.current_bytes + entry_bytes > self.byte_limit and self._entries:
            _, (_, size) = self._entries.popitem(last=False)
            self.current_bytes -= size
            self.evictions += 1
        self._entries[key] = (value, entry_bytes)
        self.current_bytes += entry_bytes
        self.peak_bytes = max(self.peak_bytes, self.current_bytes)

    def get_sample(self, namespace, key):
        return self._get(("sample", namespace, key), SAMPLING_ENTRY_BYTES)

    def put_sample(self, namespace, key, first, second, origin):
        self._put(
            ("sample", namespace, key),
            (first[0], first[1], second[0], second[1], origin),
            SAMPLING_ENTRY_BYTES,
        )

    def get_land(self, key, quality):
        return self._get(("land", quality, key), LAND_ENTRY_BYTES)

    def put_land(self, key, quality, over_land):
        self._put(("land", quality, key), bool(over_land), LAND_ENTRY_BYTES)

    def statistics(self):
        return dict(
            limit_bytes=self.byte_limit,
            current_bytes=self.current_bytes,
            peak_bytes=self.peak_bytes,
            retained_entries=len(self._entries),
            hits=self.hits,
            misses=self.misses,
            evictions=self.evictions,
        )


@dataclass(frozen=True)
class StagedWind:
    before: float
    after: float
    first: np.ndarray
    second: np.ndarray
    sources: tuple

    def contains(self, stamp):
        return (
            stamp == self.before
            if self.before == self.after
            else self.before < stamp < self.after
        )

    def weight(self, stamp):
        if not self.contains(stamp):
            raise ValueError("Timestamp crosses a source-selection event")
        return (
            (stamp - self.before) / (self.after - self.before)
            if self.after > self.before
            else 0.0
        )

    def texture_pair(self):
        # Keep Qt out of worker-selection imports until transport is needed.
        from gweatherrouting.qt.gpuweather import WeatherTexturePair

        return WeatherTexturePair.from_arrays(
            datetime.fromtimestamp(self.before),
            datetime.fromtimestamp(self.after),
            self.first[..., 0],
            self.first[..., 1],
            self.second[..., 0],
            self.second[..., 1],
        )


def time_window(sampler, stamp):
    """The resident time window that serves ``stamp``.

    An instant that is itself a selection event gets a singleton window, so
    the exact-time mask and new-cycle selection semantics are preserved and
    no interpolation crosses a model-run switch.  Every other instant is
    served by the window that brackets it.
    """
    events = sorted(
        {
            event
            for identity in sampler.order
            for event in (
                identity[0],
                sampler.coverage[identity],
                *sampler._times[identity, "wind"],
            )
        }
    )
    pos = bisect_left(events, stamp)
    if not events or pos == 0 or pos == len(events) or events[pos] == stamp:
        return stamp, stamp
    return events[pos - 1], events[pos]


def stage_wind(
    sampler,
    stamp,
    lats,
    lons,
    cancel=lambda: False,
    byte_limit=16 << 20,
    *,
    memo=None,
    memo_namespace=None,
    node_key=None,
):
    """Stage a fixed query grid using a worker-private WeatherSampler.

    Reuse the result only for the same coordinates and sampler generation.
    Missing points stay missing; per-point fallback across split files and
    older model cycles follows WeatherSampler, including grid-vector rotation.
    Cancellation never returns a cacheable partial snapshot.

    A supplied ``memo`` (a :class:`NodeSampleMemo`) prefills the endpoints of
    nodes already sampled for the same ``(source set, time window)``.  The
    memo stores the exact float32 pair, so a prefilled node is bit-identical
    to sampling it again; only finite pairs are stored.
    """
    lats, lons = np.broadcast_arrays(np.asarray(lats), np.asarray(lons))
    shape = lats.shape
    if len(shape) != 2 or not lats.size:
        raise ValueError("Expected a nonempty two-dimensional query grid")
    # Two float32 U/V arrays, excluding the sampler's own bounded band cache.
    if lats.size * 16 > byte_limit:
        raise ValueError("Staged wind exceeds memory budget")
    if not np.isfinite(stamp):
        raise ValueError("Invalid forecast time")
    before, after = time_window(sampler, stamp)
    if before == after:
        values = sampler.sample_timestamps("wind", stamp, lats, lons, cancel)
        if cancel():
            return None
        first = np.array(values.reshape(shape + (2,)), dtype=np.float32)
        first.setflags(write=False)
        return StagedWind(stamp, stamp, first, first, tuple(sampler.point_sources))
    # Use the requested interior instant, not a rounded hour or midpoint.
    lat, lon = lats.ravel(), lons.ravel()
    first = np.full((len(lat), 2), np.nan)
    second = np.full_like(first, np.nan)
    origins = np.full(len(lat), "", dtype=object)
    keys = None
    if memo is not None and memo_namespace is not None and node_key is not None:
        keys = [node_key((float(a), float(b))) for a, b in zip(lat, lon)]
        for position, key in enumerate(keys):
            entry = memo.get_sample(memo_namespace, key)
            if entry is None:
                continue
            first[position] = entry[0:2]
            second[position] = entry[2:4]
            origins[position] = entry[4]
    for identity in sampler.order:
        if cancel():
            return None
        use = (
            ~np.isfinite(first).all(axis=1)
            & (stamp >= identity[0])
            & (stamp <= sampler.coverage[identity])
        )
        if not use.any():
            continue
        group = sampler.groups[identity]
        times = sampler._times[identity, "wind"]
        query = np.full(use.sum(), stamp)
        a, ta, sa = sampler._side(
            group, reversed(times), "wind", lat[use], lon[use], query, True, cancel
        )
        b, tb, sb = sampler._side(
            group, times, "wind", lat[use], lon[use], query, False, cancel
        )
        left, names = sampler._blend(a, ta, sa, b, tb, sb, np.full(use.sum(), before))
        right, _ = sampler._blend(a, ta, sa, b, tb, sb, np.full(use.sum(), after))
        first[use], second[use], origins[use] = left, right, names
    if cancel():
        return None
    first = first.reshape(shape + (2,)).astype(np.float32)
    second = second.reshape(shape + (2,)).astype(np.float32)
    first.setflags(write=False)
    second.setflags(write=False)
    if keys is not None:
        flat_first, flat_second = first.reshape(-1, 2), second.reshape(-1, 2)
        finite = np.isfinite(flat_first).all(axis=1) & np.isfinite(flat_second).all(
            axis=1
        )
        for position in np.flatnonzero(finite):
            memo.put_sample(
                memo_namespace,
                keys[position],
                flat_first[position],
                flat_second[position],
                origins[position],
            )
    return StagedWind(
        before,
        after,
        first,
        second,
        tuple(
            ", ".join(origin.split("\x1f")) if origin else None for origin in origins
        ),
    )
