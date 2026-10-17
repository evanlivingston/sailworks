"""Shared chart coverage and tide acquisition; no GUI toolkit dependencies."""

from datetime import datetime
from typing import List, Tuple
from gweatherrouting.core.mapviewport import MapPoint
from .spatialproviders import CIOPS_DOMAINS
from .models import utc_time


def has_current_provider(manager, latitude, longitude):
    regional = any(
        bounds.south <= latitude <= bounds.north
        and bounds.west <= longitude <= bounds.east
        for _, _, _, _, bounds in CIOPS_DOMAINS.values()
    )
    return regional or bool(manager.coverage_planner.worldwide.credentials)


def no_current_provider():
    return (
        False,
        "No provider for current area",
        "Worldwide currents can be configured in Settings → Currents.",
    )


def current_coverage(manager, latitude, longitude, instant, selection=None):
    if selection is None:
        selection = manager.fields.snapshot()
    result = selection.sample(latitude, longitude, instant)
    if result.status == "available":
        field = manager.fields.fields[result.dataset_id]
        return (
            True,
            "Current data available",
            field.manifest()["model"],
        )
    if not selection.fields:
        if not has_current_provider(manager, latitude, longitude):
            return no_current_provider()
        return (
            False,
            "No enabled current models",
            "Download data or enable a saved source.",
        )
    overlapping = [
        f
        for f in selection.fields
        if f.latitudes[0] <= latitude <= f.latitudes[-1]
        and f.longitudes[0] <= longitude <= f.longitudes[-1]
    ]
    if not overlapping:
        if not has_current_provider(manager, latitude, longitude):
            return no_current_provider()
        return False, "No current coverage here", "Download data for this map area."
    if all(not f.times[0] <= instant.timestamp() <= f.times[-1] for f in overlapping):
        return (
            False,
            "Outside the loaded forecast times",
            "Choose a loaded time or download this interval.",
        )
    return (
        False,
        "No valid model current here",
        "The model masks this location or has missing data.",
    )


def stations_in_view(stations, view):
    visible = []
    for station in stations:
        longitude = (
            view._center_lon + (station.longitude - view._center_lon + 180) % 360 - 180
        )
        x, y = view.convert_geographic_to_screen(MapPoint(station.latitude, longitude))
        if (
            0 <= x <= view.get_allocated_width()
            and 0 <= y <= view.get_allocated_height()
        ):
            visible.append(station)
    return visible


def download_tides(manager, provider, view, area, cached, start, end, cancel, progress):
    stations = cached or tuple(provider.discover(cancel))
    candidates = (
        [
            s
            for s in stations
            if any(
                part.south <= s.latitude <= part.north
                and part.west <= s.longitude <= part.east
                for part in area.parts()
            )
        ]
        if area
        else stations_in_view(stations, view)
    )
    selected = [s for s in candidates if "wlp" in s.series_codes]
    if not selected:
        raise ValueError(
            "No CHS tide stations in the selected area. "
            "Coverage is currently Canadian Pacific stations."
        )
    if len(selected) > 50:
        raise ValueError(
            "Choose an area or route containing 50 or fewer tide stations."
        )
    datasets, issues = [], []
    reused = 0
    for index, station in enumerate(selected, 1):
        if cancel.is_set():
            break
        saved_prediction = manager.cached_station(station, start, end)
        progress(
            ("Using saved" if saved_prediction is not None else "Downloading")
            + " %s (%d/%d)…" % (station.name, index, len(selected))
        )
        try:
            value = manager.download_station(provider, station, start, end, cancel)
            datasets.append(value)
            reused += int(saved_prediction is not None)
        except (OSError, ValueError) as error:
            issues.append(station.name + ": " + str(error))
    return stations, datasets, issues, reused


def source_intervals(manifests):
    merged: List[Tuple[datetime, datetime]] = []
    for start, end in sorted(
        (utc_time(m["valid_start"]), utc_time(m["valid_end"])) for m in manifests
    ):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return "\n".join(
        start.strftime("%d %b %H:%M") + " → " + end.strftime("%d %b %H:%M UTC")
        for start, end in merged
    )
