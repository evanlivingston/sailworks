"""Worker-side temporal staging for numerical display textures.

No routing or shared sampler behavior changes. Within an open interval between
metadata events, source selection and valid before/after records are constant
at each spatial query point. Evaluate their linear functions at the interval
edges once; the GPU can then interpolate continuously inside that interval.
At an event itself, build a singleton snapshot to preserve exact-time mask and
new-cycle selection semantics. Never interpolate across a model-run switch.
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime

import numpy as np


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


def stage_wind(sampler, stamp, lats, lons, cancel=lambda: False, byte_limit=16 << 20):
    """Stage a fixed query grid using a worker-private WeatherSampler.

    Reuse the result only for the same coordinates and sampler generation.
    Missing points stay missing; per-point fallback across split files and
    older model cycles follows WeatherSampler, including grid-vector rotation.
    Cancellation never returns a cacheable partial snapshot.
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
        values = sampler.sample_timestamps("wind", stamp, lats, lons, cancel)
        if cancel():
            return None
        first = np.array(values.reshape(shape + (2,)), dtype=np.float32)
        first.setflags(write=False)
        return StagedWind(stamp, stamp, first, first, tuple(sampler.point_sources))
    before, after = events[pos - 1], events[pos]
    # Use the requested interior instant, not a rounded hour or midpoint.
    lat, lon = lats.ravel(), lons.ravel()
    first = np.full((len(lat), 2), np.nan)
    second = np.full_like(first, np.nan)
    origins = np.full(len(lat), "", dtype=object)
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
    return StagedWind(
        before,
        after,
        first,
        second,
        tuple(
            ", ".join(origin.split("\x1f")) if origin else None for origin in origins
        ),
    )
