"""Run-local batched wind lookups with unchanged GRIB source precedence."""

from datetime import datetime, timezone

import numpy as np

from .chs import DownloadCancelledError
from .routing import FrozenRoutingWind, FractionalRoutingGrib


def instant(microseconds):
    """Decode the integer UTC clock used by the frontier integrator."""
    seconds, remainder = divmod(int(microseconds), 1_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=remainder)


class ArrayWind:
    """Group wind windows by cell and forecast interval within one frozen run."""

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

    def query_us(self, positions, times_us):  # noqa: C901
        wind = self.wind
        if not isinstance(wind, FrozenRoutingWind):
            return self.query(positions, [instant(t) for t in times_us])
        count = len(positions)
        result = np.full((count, 2), np.nan)
        sources = np.full(count, None, dtype=object)
        found = np.zeros(count, dtype=bool)
        stamps = times_us.astype(float) / 1e6
        for grib in wind.gribs:
            self.check_cancel()
            pending = np.flatnonzero(~found)
            if not len(pending):
                break
            if (
                not isinstance(grib, FractionalRoutingGrib)
                or len(grib._forecast_hours) < 2
            ):
                for i in pending:
                    self.check_cancel()
                    try:
                        answer = grib.get_wind_at(instant(times_us[i]), *positions[i])
                    except Exception:
                        continue
                    if answer is not None:
                        result[i] = answer
                        found[i] = True
                        sources[i] = grib.name
                        wind.used.add(grib.name)
                continue
            hours = (stamps - grib._start_timestamp) / 3600
            available = np.asarray(grib._forecast_hours)
            ids = pending[
                (hours[pending] >= available[0]) & (hours[pending] <= available[-1])
            ]
            if not len(ids):
                continue
            idx = np.clip(
                np.searchsorted(available, hours[ids], side="left") - 1,
                0,
                len(available) - 2,
            )
            p = positions[ids]
            south = np.floor(p[:, 0] * 2)
            west = np.floor(p[:, 1] * 2)
            north = np.ceil(p[:, 0] * 2)
            east = np.ceil(p[:, 1] * 2)
            edge = (south == north).astype(np.int64) * 2 + (west == east)
            # Integer keys avoid sorting six float columns. Include exact
            # half-degree edges, whose legacy windows have zero width/height.
            west_codes = west.astype(np.int64)
            span = int(west_codes.max() - west_codes.min()) + 1
            codes = (
                (south.astype(np.int64) * span + west_codes - west_codes.min()) * 4
                + edge
            ) * len(available) + idx
            _, first, inverse = np.unique(codes, return_index=True, return_inverse=True)
            uv = np.full((len(first), 4), np.nan)
            present = np.zeros(len(first), dtype=bool)
            for k, j in enumerate(first):
                self.check_cancel()
                rectangle = (
                    (float(south[j] / 2), float(west[j] / 2)),
                    (float(north[j] / 2), float(east[j] / 2)),
                )
                try:
                    a = grib._get_wind_data(float(available[idx[j]]), rectangle)
                    b = grib._get_wind_data(float(available[idx[j] + 1]), rectangle)
                    if a and b:
                        uv[k] = (a[0][2], a[0][3], b[0][2], b[0][3])
                        present[k] = True
                except Exception:
                    continue
            a = available[idx]
            b = available[idx + 1]
            vv = uv[inverse]
            u = vv[:, 0] + (vv[:, 2] - vv[:, 0]) * (hours[ids] - a) / (b - a)
            v = vv[:, 1] + (vv[:, 3] - vv[:, 1]) * (hours[ids] - a) / (b - a)
            result[ids, 0] = (np.degrees(np.arctan2(-u, -v)) + 360) % 360
            result[ids, 1] = np.sqrt(u**2 + v**2)
            good = ids[present[inverse]]
            found[good] = True
            sources[good] = grib.name
            if len(good):
                wind.used.add(grib.name)
        if wind.override is not None:
            result[:, 0] = (result[:, 0] + wind.override._dir_offset) % 360
            result[:, 1] *= wind.override._speed_factor
        if found.any():
            wind.last_source = sources[np.flatnonzero(found)[-1]]
        return result, sources
