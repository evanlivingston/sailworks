"""Isochrone routing with water-relative wind and sampled current advection.

References and numerical limits are documented in docs/current-routing.md.
"""

import json
import math
import threading
from collections import OrderedDict
from copy import copy
from dataclasses import dataclass
from datetime import timedelta, timezone

import numpy as np
from pyproj import Geod
from weatherrouting import IsoPoint
from weatherrouting.routers.linearbestisorouter import LinearBestIsoRouter
from weatherrouting.routers.router import RoutingResult

from gweatherrouting.core.grib import Grib
from gweatherrouting.core.gribsampling import WeatherSampler

from .chs import DownloadCancelledError
from .fields import Area, FieldSelection, field_sort_key, normalize_priority
from .models import utc_time

GEOD = Geod(ellps="WGS84")
KNOT = 1852 / 3600
ARRIVAL_METRES = 92.6


def routing_utc(instant):
    # The existing routing wizard uses local, naive datetimes.
    return instant.astimezone(timezone.utc)


def distance_bearing(start, end):
    bearing, _, distance = GEOD.inv(start[1], start[0], end[1], end[0])
    return distance, math.radians(bearing)


def move(position, east, north, seconds):
    lon, lat, _ = GEOD.fwd(
        position[1],
        position[0],
        math.degrees(math.atan2(east, north)),
        math.hypot(east, north) * seconds,
    )
    return lat, lon


def routing_completion(routing, best_distance=None):
    """Distance-based estimate; search cost and the final detour are unknown."""
    points = [routing.position, *routing.track[routing.wp :]]
    legs = [distance_bearing(a, b)[0] for a, b in zip(points, points[1:])]
    if not hasattr(routing, "_progress_distance"):
        routing._progress_distance = sum(legs)
    if routing.end:
        return 100.0
    if not legs:
        return 99.0
    if best_distance is None:
        if routing.log and not routing._startingNewPoint:
            frontier = routing.log[-1].isochrones[-1]
            best_distance = min(p.next_wp_dist for p in frontier) * 1852
        else:
            best_distance = legs[0]
    remaining = best_distance + sum(legs[1:])
    return max(
        0.0, min(99.0, 100 * (1 - remaining / max(routing._progress_distance, 1)))
    )


@dataclass(frozen=True)
class DataGap:
    position: tuple
    time: object
    kind: str
    reason: str

    def to_json(self):
        return dict(
            latitude=self.position[0],
            longitude=self.position[1],
            time=routing_utc(self.time).isoformat(),
            kind=self.kind,
            reason=self.reason,
        )


class RoutingDataGapError(ValueError):
    def __init__(self, gaps):
        self.gaps = tuple(gaps)
        first = self.gaps[0]
        action = (
            "Load a wind forecast covering this area and time."
            if first.kind == "wind"
            else (
                "Choose another current source or adjust the route."
                if first.reason == "masked_or_missing"
                else "Enable automatic downloads or load current data for this area and time."
            )
        )
        super().__init__(
            f"{first.kind.capitalize()} data unavailable near "
            f"{first.position[0]:.3f}, {first.position[1]:.3f} "
            f"at {routing_utc(first.time):%Y-%m-%d %H:%M UTC}. {action}"
        )


class InvalidMarineLegError(Exception):
    pass


class CurrentRoutingEnvironment:
    def __init__(self, wind, selection, cancel=None):
        self.wind = wind
        self.selection = selection
        self.cancel = cancel or threading.Event()
        self.gap_history = []
        self.downloads = []
        self.excluded_samples = {}
        self.excluded_samples_truncated = False
        self.last_sample = None
        self.progress = None
        self.prefetch = None
        self.wind_recovery = None
        self._sample_cache: OrderedDict = OrderedDict()
        self.policy_pruned = 0

    def record_exclusions(self, gaps):
        # Bound memory and saved JSON; merge repeated hits at the same 11 m cell.
        for gap in gaps:
            key = (round(gap.position[0], 4), round(gap.position[1], 4), gap.reason)
            if key in self.excluded_samples:
                continue
            if len(self.excluded_samples) >= 10000:
                self.excluded_samples_truncated = True
                continue
            self.excluded_samples[key] = gap.to_json()

    def sample(self, position, instant):
        if self.cancel.is_set():
            raise DownloadCancelledError()
        key = (self.selection, tuple(position), instant)
        if key not in self._sample_cache:
            self._cache_sample(key, self._sample(*key))
        values, metadata = self._sample_cache[key]
        self._sample_cache.move_to_end(key)
        self.last_sample = dict(metadata)
        return values

    def _sample(self, selection, position, instant):
        current = selection.sample(*position, routing_utc(instant))
        return self._wind_sample(current, position, instant)

    def _cache_sample(self, key, value):
        self._sample_cache[key] = value
        self._sample_cache.move_to_end(key)
        if len(self._sample_cache) > 512:
            self._sample_cache.popitem(last=False)

    def sample_many(self, positions, instants):
        """Return per-trajectory samples or explicit gaps; never drop a gap."""
        if self.cancel.is_set():
            raise DownloadCancelledError()
        keys = [(self.selection, tuple(p), t) for p, t in zip(positions, instants)]
        unique = dict.fromkeys(keys)
        found = {
            key: self._sample_cache[key] for key in unique if key in self._sample_cache
        }
        missing = [key for key in unique if key not in found]
        currents = self.selection.sample_many(
            [key[1] for key in missing], [routing_utc(key[2]) for key in missing]
        )
        for key, current in zip(missing, currents):
            if self.cancel.is_set():
                raise DownloadCancelledError()
            try:
                value = self._wind_sample(current, key[1], key[2])
                found[key] = value
                self._cache_sample(key, value)
            except RoutingDataGapError as error:
                found[key] = error
        return [found[key] for key in keys]

    def _wind_sample(self, current, position, instant):
        if current.status != "available":
            raise RoutingDataGapError(
                (DataGap(position, instant, "current", current.status),)
            )
        wind = self.wind.get_wind_at(instant, *position)
        if wind is None or not all(math.isfinite(v) for v in wind):
            raise RoutingDataGapError(
                (DataGap(position, instant, "wind", "no_coverage"),)
            )
        direction, speed = wind
        radians = math.radians(direction)
        ce, cn = current.values
        metadata = dict(
            latitude=position[0],
            longitude=position[1],
            time=routing_utc(instant).isoformat(),
            dataset_id=current.dataset_id,
            wind_source=getattr(self.wind, "last_source", None),
        )
        if current.qc_flags:
            metadata["current_sampling"] = current.qc_flags
            metadata["current_sample_location"] = json.loads(current.detail)
        # Forecast wind is FROM; current vectors flow TOWARD, in true east/north.
        we = -speed * math.sin(radians) - ce
        wn = -speed * math.cos(radians) - cn
        water_from = math.atan2(-we, -wn)
        return (water_from, math.hypot(we, wn), ce, cn, current.dataset_id), metadata

    def provenance(self, path):
        legs = [
            sample for point in path for sample in getattr(point, "marine_samples", ())
        ]
        selected = self.selection.to_json()
        selected["actual_sources"] = sorted({s["dataset_id"] for s in legs})
        return dict(
            schema_version=1,
            routing="isochrone-with-currents",
            integration="explicit midpoint (RK2)",
            max_substep_seconds=300,
            spatial_step="target half the finest selected model resolution",
            arrival_radius_metres=ARRIVAL_METRES,
            currents=selected,
            samples=legs,
            coverage="complete at integration sample positions/times",
            search_policy="reject masked-current candidates; recover absent coverage",
            safety_pruned_candidates=self.policy_pruned,
            excluded_current_samples=list(self.excluded_samples.values()),
            excluded_current_samples_truncated=self.excluded_samples_truncated,
            optimality="best explored route within usable environmental coverage",
            resolved_search_gaps=self.gap_history,
            downloads=self.downloads,
            tide_heights="not used for depth or clearance",
        )


class FractionalRoutingGrib(Grib):
    """Compatibility wrapper using the shared space/time sampler."""

    def __init__(self, grib):
        self.grib = grib

    def __getattr__(self, name):
        return getattr(self.grib, name)

    @property
    def sampling(self):
        return self.grib.sampling

    def get_wind_at(self, instant, latitude, longitude):
        return self.grib.get_wind_at(instant, latitude, longitude)


def wind_run_timestamp(grib):
    """GRIB reference time is the model issue time, not the file download time."""
    start = getattr(grib, "start_time", None)
    return start.timestamp() if start is not None else float("-inf")


class FrozenRoutingWind:
    """Prefer newest enabled model runs and record sources used by the search."""

    def __init__(self, manager, override=None):
        self.gribs = tuple(
            sorted(
                (
                    FractionalRoutingGrib(g) if isinstance(g, Grib) else g
                    for g in manager.gribs
                ),
                key=wind_run_timestamp,
                reverse=True,
            )
        )
        self.sampler = (
            WeatherSampler(self.gribs)
            if self.gribs and all(isinstance(g, Grib) for g in self.gribs)
            else None
        )
        self.used = set()
        self.last_source = None
        self.override = None
        if override is not None:
            self.override = copy(override)
            self.override._grib = self

    def get_wind_at(self, instant, latitude, longitude):
        if self.sampler is not None:
            result = self.sampler.wind_at(instant, latitude, longitude)
            if result is not None:
                self.used.update(self.sampler.last_sources)
                self.last_source = ", ".join(sorted(self.sampler.last_sources))
                if self.override is not None:
                    wd, ws = result
                    return (
                        (wd + self.override._dir_offset) % 360,
                        ws * self.override._speed_factor,
                    )
            return result
        for grib in self.gribs:
            try:
                result = grib.get_wind_at(instant, latitude, longitude)
            except Exception:
                continue
            if result is not None:
                self.used.add(grib.name)
                self.last_source = grib.name
                if self.override is not None:
                    wd, ws = result
                    return (
                        (wd + self.override._dir_offset) % 360,
                        ws * self.override._speed_factor,
                    )
                return result
        return None

    def add_gribs(self, gribs):
        """Add coverage and prefer newer runs while retaining scenario overrides."""
        known = {g.path for g in self.gribs}
        added = [
            FractionalRoutingGrib(g) if isinstance(g, Grib) else g
            for g in gribs
            if g.path not in known
        ]
        self.gribs = tuple(
            sorted((*self.gribs, *added), key=wind_run_timestamp, reverse=True)
        )
        self.sampler = (
            WeatherSampler(self.gribs)
            if self.gribs and all(isinstance(g, Grib) for g in self.gribs)
            else None
        )
        return bool(added)

    def provenance(self):
        return dict(
            selected=[
                dict(
                    name=g.name,
                    path=g.path,
                    centre=g.centre,
                    start=str(g.start_time),
                    end=str(g.end_time),
                )
                for g in self.gribs
            ],
            actual_sources=sorted(self.used),
            selection_policy="newest enabled model run with valid wind; load order breaks ties",
        )


class CurrentIsoRouter(LinearBestIsoRouter):
    """Reuse the engine's isochrone interface; never use its wind-only final shortcut."""

    PARAMS: dict = {}
    use_numpy = True

    def __init__(self, *args, environment, **kwargs):
        super().__init__(*args, **kwargs)
        self.environment = environment
        if (self.points_validity or self.point_validity) and (
            self.lines_validity or self.line_validity
        ):
            environment.selection.neighbor_valid = self.valid_current_neighbor
            environment.selection.neighbor_valid_many = self.valid_current_neighbors
        self.resolution = min(
            (f.manifest()["resolution_m"] for f in environment.selection.fields),
            default=9000,
        )

    def valid_current_neighbor(self, start, end):
        return self.valid(start, start) and self.valid(start, end)

    def valid_current_neighbors(self, starts, ends):
        """The same origin, endpoint and full-segment checks in batches."""
        starts, ends = np.asarray(starts), np.asarray(ends)
        valid = np.ones(len(starts), dtype=bool)
        for targets in (starts, ends):
            ids = np.flatnonzero(valid)
            if not len(ids):
                break
            if self.points_validity:
                valid[ids] &= np.asarray(self.points_validity(targets[ids]), dtype=bool)
            if self.point_validity:
                for i in np.flatnonzero(valid):
                    valid[i] = self.point_validity(*targets[i])
            ids = np.flatnonzero(valid)
            if self.lines_validity and len(ids):
                valid[ids] &= np.asarray(
                    self.lines_validity(np.column_stack((starts[ids], targets[ids]))),
                    dtype=bool,
                )
            if self.line_validity:
                for i in np.flatnonzero(valid):
                    valid[i] = self.line_validity(*starts[i], *targets[i])
        return valid

    def valid(self, start, end):
        if self.points_validity and not all(self.points_validity([end])):
            return False
        if self.point_validity and not self.point_validity(*end):
            return False
        if self.lines_validity and not all(self.lines_validity([[*start, *end]])):
            return False
        return not self.line_validity or self.line_validity(*start, *end)

    def velocity(self, position, instant, heading):
        from gweatherrouting.core.safetypolicy import policy_of

        wd, ws, ce, cn, source = self.environment.sample(position, instant)
        if policy_of(self.polar).is_nogo(ws / KNOT):
            self.environment.policy_pruned += 1
            raise InvalidMarineLegError()
        angle = abs((heading - wd + math.pi) % (2 * math.pi) - math.pi)
        speed = self.polar.get_speed(ws / KNOT, angle) * KNOT
        if not math.isfinite(speed) or speed < 0:
            raise ValueError("The boat polar returned an invalid speed")
        return (
            speed * math.sin(heading) + ce,
            speed * math.cos(heading) + cn,
            speed,
            wd,
            ws,
            source,
            ce,
            cn,
            dict(self.environment.last_sample),
        )

    def advance(self, point, seconds, heading, end):
        position, instant = point.pos, point.time
        samples: list = []
        elapsed = 0.0
        while elapsed < seconds:
            distance, _ = distance_bearing(position, end)
            if distance <= ARRIVAL_METRES:
                break
            v = self.velocity(position, instant, heading)
            sog = math.hypot(v[0], v[1])
            duration = min(
                300,
                seconds - elapsed,
                self.resolution / (2 * max(sog, 0.01)),
                distance / max(sog, 0.01),
            )
            midpoint = move(position, v[0], v[1], duration / 2)
            if not self.valid(position, midpoint):
                raise InvalidMarineLegError()
            mt = instant + timedelta(seconds=duration / 2)
            mv = self.velocity(midpoint, mt, heading)
            destination = move(position, mv[0], mv[1], duration)
            if not self.valid(midpoint, destination):
                raise InvalidMarineLegError()
            arrival = instant + timedelta(seconds=duration)
            # Also check the endpoint: a field's validity must cover the full leg.
            self.environment.sample(destination, arrival)
            samples.extend((v[8], dict(self.environment.last_sample)))
            samples.append(
                dict(
                    **mv[8],
                    current_east_ms=mv[6],
                    current_north_ms=mv[7],
                    speed_through_water_kn=mv[2] / KNOT,
                    heading_true=math.degrees(heading) % 360,
                )
            )
            position, instant = destination, arrival
            elapsed += duration
        distance, _ = distance_bearing(position, end)
        _, bearing = distance_bearing(point.pos, position)
        travelled, _ = distance_bearing(point.pos, position)
        result = IsoPoint(
            position,
            time=instant,
            twd=v[3] if samples else point.twd,
            tws=v[4] / KNOT if samples else point.tws,
            speed=travelled / max(elapsed, 0.01) / KNOT,
            brg=math.degrees(bearing) % 360,
            next_wp_dist=distance / 1852,
        )
        result.marine_samples = tuple(samples)
        return result

    def headings(self, point, end):
        from gweatherrouting.core.safetypolicy import policy_of

        wd, ws, ce, cn, _ = self.environment.sample(point.pos, point.time)
        if policy_of(self.polar).is_nogo(ws / KNOT):
            self.environment.policy_pruned += 1
            return []
        headings = [wd + math.radians(angle) for angle in range(-180, 180, 5)]
        _, course = distance_bearing(point.pos, end)
        heading = course
        # Add a course-to-steer candidate between the regular heading samples.
        # The propagated trajectory still gets the same full integration/checks.
        cross = ce * math.cos(course) - cn * math.sin(course)
        for _ in range(8):
            angle = abs((heading - wd + math.pi) % (2 * math.pi) - math.pi)
            speed = self.polar.get_speed(ws / KNOT, angle) * KNOT
            if speed <= abs(cross):
                break
            heading = course + math.asin(-cross / speed)
        headings.append(heading)
        return headings

    def advance_candidates(self, point, seconds, headings, end):
        if self.use_numpy:
            from .batchrouting import advance_headings

            candidates, gaps = advance_headings(self, point, seconds, headings, end)
            yield from candidates
            if gaps:
                yield RoutingDataGapError(gaps)
            return
        for heading in headings:
            try:
                yield self.advance(point, seconds, heading, end)
            except InvalidMarineLegError:
                continue
            except RoutingDataGapError as error:
                yield error

    def route(self, lastlog, t, hours, start, end):  # noqa: C901
        # Preserve custom candidate generators and the scalar reference path.
        if (
            self.use_numpy
            and "advance_candidates" not in self.__dict__
            and type(self).advance_candidates is CurrentIsoRouter.advance_candidates
        ):
            from .frontierrouting import array_clock_compatible, route_frontier

            instants = [p.time for p in lastlog.isochrones[-1]] if lastlog else [t]
            if array_clock_compatible(instants, min(hours * 3600, 600)):
                return route_frontier(self, lastlog, t, hours, start, end)
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
            raise ValueError(
                "Routing exceeded seven days without reaching the waypoint"
            )
        bins: dict = {}
        gaps = {}
        winner = None
        best_distance = min(p.next_wp_dist for p in isochrones[-1]) * 1852
        total = len(isochrones[-1])
        for index, point in enumerate(isochrones[-1]):
            if self.environment.cancel.is_set():
                raise DownloadCancelledError()
            if self.environment.progress:
                self.environment.progress(best_distance, index, total)
            try:
                headings = self.headings(point, end)
            except RoutingDataGapError as error:
                for gap in error.gaps:
                    gaps[(gap.position, gap.time, gap.kind)] = gap
                continue
            for candidate in self.advance_candidates(
                point, min(hours * 3600, 600), headings, end
            ):
                if isinstance(candidate, RoutingDataGapError):
                    for gap in candidate.gaps:
                        gaps[(gap.position, gap.time, gap.kind)] = gap
                else:
                    candidate.prev_idx = index
                    best_distance = min(best_distance, candidate.next_wp_dist * 1852)
                    if candidate.next_wp_dist * 1852 <= ARRIVAL_METRES:
                        if winner is None or candidate.time < winner.time:
                            winner = candidate
                    # Prune as candidates arrive, rather than retaining every
                    # rejected trajectory and its provenance until the step ends.
                    _, bearing = distance_bearing(start, candidate.pos)
                    key = round(math.degrees(bearing) / 2)
                    if (
                        key not in bins
                        or candidate.next_wp_dist < bins[key].next_wp_dist
                    ):
                        bins[key] = candidate
        if self.environment.progress:
            self.environment.progress(best_distance, total, total)
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
            # Recover genuinely absent coverage before constraining the search.
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
        if winner is not None:
            path = [winner]
            for frontier in reversed(isochrones):
                path.append(frontier[path[-1].prev_idx])
            path.reverse()
            return self.result(winner.time, path, isochrones + [[winner]], winner.pos)
        # Equal-time isochrones, pruned by bearing from the leg's start.
        frontier = list(bins.values())
        return self.result(frontier[0].time, [], isochrones + [frontier], start)

    def result(self, time, path, isochrones, position):
        result = RoutingResult(time, path, isochrones, position)
        setattr(
            result,
            "excluded_current_samples",
            tuple(self.environment.excluded_samples.values()),
        )
        return result


def gap_request_key(gap):
    lat, lon = gap.position
    day = routing_utc(gap.time).replace(hour=0, minute=0, second=0, microsecond=0)
    return math.floor(lon * 4) / 4, math.floor(lat * 4) / 4, day


def download_gap_coverage(manager, gaps, cancel, progress=None):
    """Bound requests by quarter-degree tiles and UTC day; never request the globe."""
    requests = {}
    for gap in gaps:
        if gap.kind != "current":
            continue
        west, south, day = gap_request_key(gap)
        requests[(west, south, day)] = Area(
            west, max(-90, south), min(180, west + 0.25), min(90, south + 0.25)
        )
    if len(requests) > 32:
        raise ValueError(
            "Missing coverage spans more than 32 tiles. Download the route area first."
        )
    fields, issues = [], []
    for (_, _, day), area in requests.items():
        result, errors = manager.coverage_planner.download(
            area, day, day + timedelta(days=1), cancel, progress
        )
        fields.extend(result)
        issues.extend(errors)
    return fields, issues


def install_downloaded_fields(routing, manager, fields, save_fields=None):
    """Publish new coverage only between steps, preserving the frozen selection."""
    environment = routing.current_environment
    before = {f.id for f in environment.selection.fields}
    fields = [manager.fields.equivalent(field) for field in fields]
    if save_fields is not None:
        save_fields(fields)
    else:
        for field in fields:
            manager.fields.add(field)
    selected = {f.id: f for f in environment.selection.fields}
    for field in fields:
        if field.id not in manager.fields.excluded:
            selected[field.id] = field
    previous = environment.selection
    priority = normalize_priority(getattr(previous, "priority", "finest"))
    environment.selection = FieldSelection(
        sorted(
            selected.values(),
            key=field_sort_key(priority),
        ),
        neighbor_valid=previous.neighbor_valid,
        neighbor_valid_many=previous.neighbor_valid_many,
        priority=priority,
    )
    environment.selection.used.update(previous.used)
    environment.selection.missing.update(previous.missing)
    routing.algorithm.resolution = min(
        (f.manifest()["resolution_m"] for f in environment.selection.fields),
        default=9000,
    )
    added = {f.id for f in environment.selection.fields} - before
    environment.downloads.extend(sorted(added))
    return added


def step_with_coverage(  # noqa: C901
    routing, manager, auto_download, report, save_fields=None
):
    """Retry the unchanged isochrone after bounded coverage recovery."""
    environment = getattr(routing, "current_environment", None)
    if environment is None:
        return routing.step()
    if environment.wind_recovery is not None:
        environment.wind_recovery.ready()
    if environment.prefetch is not None:
        fields, _ = environment.prefetch.ready()
        if fields:
            install_downloaded_fields(routing, manager, fields, save_fields)
    for attempt in range(3):
        try:
            return routing.step()
        except RoutingDataGapError as error:
            report(str(error))
            if (
                (
                    not auto_download
                    and (
                        any(g.kind != "wind" for g in error.gaps)
                        or getattr(environment.wind_recovery, "model_recovery", None)
                        is None
                    )
                )
                or attempt == 2
                or (
                    any(g.kind == "wind" for g in error.gaps)
                    and environment.wind_recovery is None
                )
                or any(g.reason == "masked_or_missing" for g in error.gaps)
            ):
                raise
            environment.gap_history.extend(g.to_json() for g in error.gaps[:64])
            wind_gaps = [g for g in error.gaps if g.kind == "wind"]
            if wind_gaps:
                report(
                    "Downloading missing wind forecasts; routing will resume automatically…"
                )
                environment.wind_recovery.recover(wind_gaps)
                if all(g.kind == "wind" for g in error.gaps):
                    continue
            current_gaps = [g for g in error.gaps if g.kind == "current"]
            report(
                "Missing current data; downloading coverage and retrying this routing step…"
            )
            if environment.prefetch is not None:
                fields, issues = environment.prefetch.wait_for(current_gaps)
            else:
                fields, issues = download_gap_coverage(
                    manager, current_gaps, environment.cancel, report
                )
            if environment.cancel.is_set():
                raise DownloadCancelledError()
            added = install_downloaded_fields(routing, manager, fields, save_fields)
            # New retrieval IDs alone are not evidence that coverage improved.
            improved = any(
                environment.selection.sample(
                    *gap.position, routing_utc(gap.time)
                ).status
                == "available"
                for gap in error.gaps
            )
            if not added or not improved:
                detail = (
                    "\n" + "\n".join(issues)
                    if issues
                    else "\nDownloads did not fill the gap."
                )
                raise ValueError(str(error) + detail) from error
