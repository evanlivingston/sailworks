"""Cheap, offline screening of current coverage before an isochrone search.

This samples a corridor and a time window, rather than attempting to predict
every position and arrival time the actual search will explore.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .chs import DownloadCancelledError
from .fields import FieldSelection
from .routing import GEOD, DataGap, gap_request_key, routing_utc


@dataclass(frozen=True)
class CoverageFinding:
    region: str
    status: str
    position: tuple
    time: datetime
    model: str
    locations: int


@dataclass(frozen=True)
class CurrentPreflight:
    findings: tuple
    blocked: bool
    positions: int
    start: datetime
    end: datetime
    coastline_checked: bool
    download_gaps: tuple = ()

    @property
    def needs_warning(self):
        return bool(self.findings) or not self.coastline_checked

    def describe(self, auto_download):
        lines = []
        statuses = {f.status for f in self.findings}
        if "land" in statuses:
            lines.append("A route point is on land. Move it into navigable water.")
        if statuses & {"no_coverage", "time_gap"}:
            lines.append(
                "Some current coverage is missing. It will download automatically."
                if auto_download
                else "Some current coverage is missing. "
                "Enable automatic downloads or load current data."
            )
        if "masked_or_missing" in statuses:
            lines.append(
                "No usable currents at departure. Choose another current source or move the start."
                if any(
                    f.region == "departure" and f.status == "masked_or_missing"
                    for f in self.findings
                )
                else "Some areas have no usable currents. Routing will avoid them. "
                "Choose another current source or adjust the route if needed."
            )
        if not self.coastline_checked:
            lines.append(
                "Coastline checks are off. Enable them to avoid land crossings."
            )
        return "\n\n".join(lines)


def _locations(points):
    legs = [GEOD.inv(a[1], a[0], b[1], b[0]) for a, b in zip(points, points[1:])]
    spacing = max(500, sum(leg[2] for leg in legs) / 800)
    locations = {tuple(points[0]): "track"}
    for (a, b), (bearing, _, distance) in zip(zip(points, points[1:]), legs):
        count = max(1, math.ceil(distance / spacing))
        middle = GEOD.npts(a[1], a[0], b[1], b[0], count - 1) if count > 1 else []
        for lat, lon in [a, *((lat, lon) for lon, lat in middle), b]:
            for offset in (-9260, *range(-9000, 9001, 500), 9260):
                if offset == 0:
                    continue
                x, y, _ = GEOD.fwd(lon, lat, bearing + 90, offset)
                locations.setdefault((y, x), "nearby")
            locations[(lat, lon)] = "track"
    return locations


def _water_mask(algorithm, positions):
    valid = [True] * len(positions)
    if algorithm.points_validity:
        valid = list(algorithm.points_validity(positions))
    if algorithm.point_validity:
        valid = [ok and algorithm.point_validity(*p) for ok, p in zip(valid, positions)]
    return valid


def _departure_issues(selection, points, endpoint_water, start, auto_download, add):
    blocked = not all(endpoint_water)
    for index, (point, ok) in enumerate(zip(points, endpoint_water)):
        if not ok:
            add("departure" if index == 0 else "track", "land", point, start)
    departure = selection.sample(points[0][0], points[0][1], start)
    if departure.status != "available":
        add("departure", departure.status, points[0], start, departure.dataset_id)
        blocked |= departure.status == "masked_or_missing" or not auto_download
    return blocked


def check_current_coverage(routing, auto_download=False):
    """Screen a frozen selection without downloads or changing run provenance."""
    environment = getattr(routing, "current_environment", None)
    if environment is None:
        return None
    if environment.cancel.is_set():
        raise DownloadCancelledError()
    selection = FieldSelection(
        environment.selection.fields,
        environment.selection.neighbor_valid,
        priority=getattr(environment.selection, "priority", "finest"),
    )
    start = routing_utc(routing.time)
    end = start + timedelta(hours=24)
    points = [tuple(routing.position), *(tuple(p) for p in routing.track[routing.wp :])]
    locations = _locations(points)
    positions = list(locations)
    valid = _water_mask(routing.algorithm, positions)
    coastline = bool(
        routing.algorithm.points_validity or routing.algorithm.point_validity
    )
    water = [p for p, ok in zip(positions, valid) if ok]
    models = {f.id: f.manifest()["model"] for f in selection.fields}
    groups: dict[tuple[str, str, str], tuple[tuple, datetime, set[tuple]]] = {}
    downloads: dict = {}

    def add(region, status, position, instant, dataset_id=None):
        key = (region, status, models.get(dataset_id, ""))
        if key not in groups:
            groups[key] = (position, instant, set())
        groups[key][2].add(position)

    endpoint_water = _water_mask(routing.algorithm, points)
    blocked = _departure_issues(
        selection, points, endpoint_water, start, auto_download, add
    )
    for hour in range(25):
        if environment.cancel.is_set():
            raise DownloadCancelledError()
        instant = start + timedelta(hours=hour)
        samples = selection.sample_many(water, [instant] * len(water))
        for point, sample in zip(water, samples):
            if sample.status != "available":
                add(locations[point], sample.status, point, instant, sample.dataset_id)
                if sample.status in ("no_coverage", "time_gap"):
                    gap = DataGap(point, instant, "current", sample.status)
                    # Keep a representative per tile and hour, rather than a
                    # second copy of every sampled position in the corridor.
                    key = (*gap_request_key(gap), hour)
                    downloads.setdefault(key, gap)
    findings = tuple(
        CoverageFinding(region, status, position, instant, model, len(seen))
        for (region, status, model), (position, instant, seen) in groups.items()
    )
    return CurrentPreflight(
        findings, blocked, len(water), start, end, coastline, tuple(downloads.values())
    )
