"""Run-local batched wind lookups over the shared space/time sampler."""

from datetime import datetime, timezone

import numpy as np

from .chs import DownloadCancelledError
from .routing import FrozenRoutingWind


def instant(microseconds):
    """Decode the integer UTC clock used by the frontier integrator."""
    seconds, remainder = divmod(int(microseconds), 1_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=remainder)


class ArrayWind:
    """Sample arbitrary positions/timestamps together within one frozen run."""

    def __init__(self, wind, cancel=None):
        self.wind = wind
        self.cancel = cancel

    def check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise DownloadCancelledError()

    def query(self, positions, times):
        values = np.full((len(positions), 2), np.nan)
        sources = np.full(len(positions), None, dtype=object)
        for i, (position, time) in enumerate(zip(positions, times)):
            self.check_cancel()
            answer = self.wind.get_wind_at(time, *position)
            if answer is not None:
                values[i] = answer
                sources[i] = getattr(self.wind, "last_source", None)
        return values, sources

    def query_us(self, positions, times_us):
        wind = self.wind
        if not isinstance(wind, FrozenRoutingWind) or wind.sampler is None:
            return self.query(positions, [instant(t) for t in times_us])
        self.check_cancel()
        uv = wind.sampler.sample_timestamps(
            "wind",
            times_us.astype(float) / 1e6,
            positions[:, 0],
            positions[:, 1],
            self.check_cancel,
        )
        result = np.column_stack(
            (
                (np.degrees(np.arctan2(-uv[:, 0], -uv[:, 1])) + 360) % 360,
                np.hypot(uv[:, 0], uv[:, 1]),
            )
        )
        sources = wind.sampler.point_sources.copy()
        wind.used.update(wind.sampler.last_sources)
        if wind.override is not None:
            result[:, 0] = (result[:, 0] + wind.override._dir_offset) % 360
            result[:, 1] *= wind.override._speed_factor
        found = np.flatnonzero(np.isfinite(result).all(axis=1))
        if len(found):
            wind.last_source = sources[found[-1]]
        self.check_cancel()
        return result, sources
