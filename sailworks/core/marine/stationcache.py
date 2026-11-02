"""Reuse compatible station predictions and request only uncovered time spans."""

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .models import Capability, Dataset, Sample, Series, canonical_json, sample_station


def compatible(a, b):
    """Never stitch changed stations, units or datum/provider metadata."""
    return (
        a.provider == b.provider
        and a.station == b.station
        and a.metadata_json == b.metadata_json
        and a.attribution == b.attribution
        and a.licence_url == b.licence_url
        and {(s.code, s.unit, s.source_code) for s in a.series}
        == {(s.code, s.unit, s.source_code) for s in b.series}
    )


def coverage(dataset, start, end):
    series = dataset.get_series("water_level")
    if series is None:
        return []
    left, right = max(start, dataset.requested_start), min(end, dataset.requested_end)
    if right <= left:
        return []
    times = sorted(
        {left, right, *(s.time for s in series.samples if left < s.time < right)}
    )
    good = {
        t: sample_station(dataset, Capability.WATER_LEVEL, t).status == "available"
        for t in times
    }
    return [
        (a, b)
        for a, b in zip(times, times[1:])
        if good[a]
        and good[b]
        and sample_station(dataset, Capability.WATER_LEVEL, a + (b - a) / 2).status
        == "available"
    ]


def missing_intervals(datasets, start, end):
    intervals = sorted(
        interval for d in datasets for interval in coverage(d, start, end)
    )
    missing, cursor = [], start
    for left, right in intervals:
        if left > cursor:
            missing.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        missing.append((cursor, end))
    return missing


def plan(datasets, station, start, end):
    groups: list[list[Dataset]] = []
    for dataset in reversed(datasets):
        if dataset.station.id != station.id or not coverage(dataset, start, end):
            continue
        group = next((g for g in groups if compatible(g[0], dataset)), None)
        if group is None:
            groups.append([dataset])
        else:
            group.append(dataset)
    plans = [(g, missing_intervals(g, start, end)) for g in groups]
    return (
        min(plans, key=lambda p: sum((b - a).total_seconds() for a, b in p[1]))
        if plans
        else ([], [(start, end)])
    )


def aligned_requests(intervals):
    """Include interpolation brackets on CHS's fifteen-minute sample clock."""
    ranges: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        a = datetime.fromtimestamp(
            math.floor(start.timestamp() / 900) * 900, timezone.utc
        )
        b = datetime.fromtimestamp(math.ceil(end.timestamp() / 900) * 900, timezone.utc)
        if ranges and a <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(b, ranges[-1][1]))
        else:
            ranges.append((a, b))
    # Padding a seven-day user window must not exceed the provider's limit.
    for start, end in ranges:
        while start < end:
            right = min(end, start + timedelta(days=7))
            yield start, right
            start = right


def merge(datasets):
    """Keep original sample values/QC and complete source-response provenance."""
    latest = max(datasets, key=lambda d: d.retrieved_at)
    series, sources = [], {}
    for original in latest.series:
        samples: dict[datetime, Sample] = {}
        for dataset in datasets:
            value = dataset.get_series(original.code)
            assert value is not None
            samples.update((s.time, s) for s in value.samples)
        series.append(
            Series(
                original.code,
                original.unit,
                tuple(samples[t] for t in sorted(samples)),
                original.source_code,
            )
        )
    for dataset in datasets:
        for source in json.loads(dataset.sources_json):
            sources[canonical_json(source)] = source
    return replace(
        latest,
        requested_start=min(d.requested_start for d in datasets),
        requested_end=max(d.requested_end for d in datasets),
        series=tuple(series),
        sources_json=canonical_json(list(sources.values())),
    )


def download_missing(provider, station, start, end, cancel, datasets):
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise ValueError("Choose a download interval between zero and seven days")
    cached, missing = plan(datasets, station, start, end)
    if len(missing) > 8:
        cached, missing = [], [(start, end)]
    downloaded = [
        provider.download(station, a, b, cancel) for a, b in aligned_requests(missing)
    ]
    values = [*sorted(cached, key=lambda d: d.retrieved_at), *downloaded]
    if not all(compatible(values[0], d) for d in values[1:]):
        # A changed datum or station definition requires a fresh coherent window.
        values = [
            provider.download(station, a, b, cancel)
            for a, b in aligned_requests([(start, end)])
        ]
        if not all(compatible(values[0], d) for d in values[1:]):
            raise ValueError(
                "Station metadata changed during download; retry the request"
            )
    return merge(values) if len(values) > 1 else values[0]
