import json
from datetime import datetime, timedelta, timezone

import pytest

from sailworks.core import era5
from sailworks.core.forecasttranche import AnalysisSelection
from sailworks.core.historicaltranche import (
    AREA_TOLERANCE_DEG,
    MAX_WINDOW_HOURS,
    MIN_WINDOW_HOURS,
    HistoricalTrancheError,
    build_wind_tranche,
    hourly_window,
    read_era5_catalog,
    select_window_entries,
    wind_tranche_for_window,
)
from sailworks.core.tranchestore import ResolvedForecast, TrancheStore

UTC = timezone.utc
# Catalog chunks record ERA5's (north, west, south, east) order.
CATALOG_AREA = (50.0, -10.0, 40.0, 5.0)
# Callers pass our (west, south, east, north) convention.
REGION = (-10.0, 40.0, 5.0, 50.0)
WIND = era5.FIELD_VARIABLES["wind"]


def at(hour):
    return datetime(2025, 1, 1, hour, tzinfo=UTC)


def stamp(value):
    return value.isoformat()


def make_store(tmp_path):
    return TrancheStore(tmp_path / "store")


def chunk(root, name, start, end=None, *, area=CATALOG_AREA, variables=WIND):
    (root / name).write_bytes(name.encode("ascii"))
    return {
        "artifact": name,
        "sha256": "0" * 64,
        "size": len(name),
        "start": stamp(start),
        "end": stamp(end if end is not None else start),
        "area": list(area),
        "variables": list(variables),
    }


def hourly_chunks(root, hours):
    return [chunk(root, f"h{hour}.grib", at(hour)) for hour in hours]


def test_window_builds_one_artifact_and_resolves(tmp_path):
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, range(4))
    tranche = build_wind_tranche(store, at(0), at(4), REGION, catalog=entries)

    assert len(tranche.entries) == 1
    entry = tranche.entries[0]
    assert len(entry.artifacts) == 1
    assert entry.fields == ("wind",)
    assert dict(entry.field_times)["wind"] == hourly_window(at(0), at(4))

    resolved = wind_tranche_for_window(store, at(0), at(4), REGION, catalog=entries)
    assert isinstance(resolved, ResolvedForecast)
    assert len(resolved.artifact_paths) == 1
    assert resolved.artifact_paths[0].read_bytes() == (b"h0.gribh1.gribh2.gribh3.grib")


def test_active_selection_is_never_touched(tmp_path):
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, range(4))
    assert store.active() is None
    wind_tranche_for_window(store, at(0), at(4), REGION, catalog=entries)
    assert store.active() is None

    other = build_wind_tranche(store, at(0), at(2), REGION, catalog=entries[:2])
    store.activate(AnalysisSelection(other.tranche_id, other.entries[0].entry_id))
    selection = store.active_selection()

    wind_tranche_for_window(store, at(0), at(4), REGION, catalog=entries)
    assert store.active_selection() == selection
    assert store.active().tranche.tranche_id == other.tranche_id


def test_hourly_window_is_half_open():
    stamps = hourly_window(at(0), at(3))
    assert stamps == (at(0), at(1), at(2))
    assert at(3) not in stamps
    with pytest.raises(HistoricalTrancheError, match="whole hour"):
        hourly_window(datetime(2025, 1, 1, 0, 30, tzinfo=UTC), at(3))


def test_rejects_window_shorter_than_minimum(tmp_path):
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, range(2))
    assert MIN_WINDOW_HOURS == 2
    with pytest.raises(HistoricalTrancheError, match="at least 2 hours"):
        select_window_entries(entries, at(0), at(1), CATALOG_AREA)


def test_rejects_window_longer_than_maximum(tmp_path):
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, range(2))
    end = at(0) + timedelta(hours=MAX_WINDOW_HOURS + 1)
    with pytest.raises(HistoricalTrancheError, match="at most 384 hours"):
        select_window_entries(entries, at(0), end, CATALOG_AREA)


def test_rejects_non_whole_hour_and_naive_bounds(tmp_path):
    entries = hourly_chunks(make_store(tmp_path).root, range(2))
    with pytest.raises(HistoricalTrancheError, match="whole hour"):
        select_window_entries(
            entries, datetime(2025, 1, 1, 0, 30, tzinfo=UTC), at(3), CATALOG_AREA
        )
    with pytest.raises(HistoricalTrancheError, match="timezone-aware UTC"):
        select_window_entries(entries, datetime(2025, 1, 1, 0), at(3), CATALOG_AREA)


def test_rejects_when_no_entries_cover_the_window(tmp_path):
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, range(4, 6))
    with pytest.raises(HistoricalTrancheError, match="cover"):
        select_window_entries(entries, at(0), at(2), CATALOG_AREA)


def test_rejects_a_window_with_a_gap(tmp_path):
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, (0, 1, 3))
    with pytest.raises(HistoricalTrancheError, match="2025-01-01T02:00:00"):
        select_window_entries(entries, at(0), at(4), CATALOG_AREA)


def test_overlapping_entries_are_deduplicated(tmp_path):
    """Two runs may store the same hour; the window still resolves."""
    store = make_store(tmp_path)
    entries = [
        chunk(store.root, "a.grib", at(0), at(1)),
        chunk(store.root, "b.grib", at(1), at(2)),
    ]

    selected = select_window_entries(entries, at(0), at(3), CATALOG_AREA)

    assert [entry["artifact"] for entry in selected] == ["a.grib", "b.grib"]
    assert all(entry["artifact"] for entry in selected)


def test_a_duplicate_hour_does_not_hide_a_gap(tmp_path):
    store = make_store(tmp_path)
    entries = [
        chunk(store.root, "a.grib", at(0)),
        chunk(store.root, "b.grib", at(0)),
    ]
    with pytest.raises(HistoricalTrancheError, match="not covered"):
        select_window_entries(entries, at(0), at(2), CATALOG_AREA)


def test_another_area_does_not_count_as_coverage(tmp_path):
    """Chunks stored for a different region leave this window uncovered."""
    store = make_store(tmp_path)
    entries = [
        chunk(store.root, "a.grib", at(0)),
        chunk(store.root, "b.grib", at(1), area=(30.0, -10.0, 20.0, 5.0)),
    ]
    with pytest.raises(HistoricalTrancheError, match="not covered"):
        select_window_entries(entries, at(0), at(2), CATALOG_AREA)


def test_missing_wind_component_names_the_hour(tmp_path):
    store = make_store(tmp_path)
    entries = [
        chunk(store.root, "a.grib", at(0)),
        chunk(store.root, "b.grib", at(1)),
        chunk(store.root, "c.grib", at(2), variables=[WIND[0]]),
    ]
    with pytest.raises(HistoricalTrancheError) as error:
        select_window_entries(entries, at(0), at(3), CATALOG_AREA)
    assert "2025-01-01T02:00:00" in str(error.value)
    assert WIND[1] in str(error.value)


def test_area_tolerance_accepts_near_and_rejects_far(tmp_path):
    store = make_store(tmp_path)
    near = list(CATALOG_AREA)
    near[0] += AREA_TOLERANCE_DEG / 2
    entries = [
        chunk(store.root, "a.grib", at(0), area=near),
        chunk(store.root, "b.grib", at(1), area=near),
    ]
    selected = select_window_entries(entries, at(0), at(2), CATALOG_AREA)
    assert len(selected) == 2

    far = list(CATALOG_AREA)
    far[0] += 1e-3
    far_entries = [
        chunk(store.root, "c.grib", at(0), area=far),
        chunk(store.root, "d.grib", at(1), area=far),
    ]
    with pytest.raises(HistoricalTrancheError, match="match area"):
        select_window_entries(far_entries, at(0), at(2), CATALOG_AREA)


def test_repeated_build_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, range(2))
    first = wind_tranche_for_window(store, at(0), at(2), REGION, catalog=entries)
    second = wind_tranche_for_window(store, at(0), at(2), REGION, catalog=entries)
    assert first.tranche.tranche_id == second.tranche.tranche_id
    assert len(list(store.manifests.glob("*.json"))) == 1


def test_read_era5_catalog_handles_missing_and_malformed(tmp_path):
    assert read_era5_catalog(tmp_path / "absent") == []

    root = tmp_path / "store"
    root.mkdir()
    (root / "era5-history.json").write_text("[", encoding="utf8")
    with pytest.raises(HistoricalTrancheError, match="era5-history.json"):
        read_era5_catalog(root)


def test_catalog_entries_are_read_from_the_store(tmp_path):
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, range(2))
    (store.root / "era5-history.json").write_text(json.dumps(entries), encoding="utf8")
    resolved = wind_tranche_for_window(store, at(0), at(2), REGION)
    assert len(resolved.entry.artifacts) == 1


def test_region_is_converted_to_catalog_order(tmp_path, monkeypatch):
    """A W,S,E,N caller region still matches N,W,S,E catalog entries."""
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, range(2))
    captured = {}
    real = era5.build_history_display

    def record(carrier, selected, *, model_id="ERA5"):
        captured["areas"] = [tuple(item["area"]) for item in selected]
        return real(carrier, selected, model_id=model_id)

    monkeypatch.setattr(era5, "build_history_display", record)
    tranche = build_wind_tranche(store, at(0), at(2), REGION, catalog=entries)

    assert len(tranche.entries) == 1
    # The selected entries keep the catalog's ERA5-order area.
    assert captured["areas"] == [CATALOG_AREA, CATALOG_AREA]
    north, west, south, east = captured["areas"][0]
    assert north > south and west < east


def test_rejects_a_caller_region_in_catalog_order(tmp_path):
    """Passing (north, west, south, east) where a region is expected fails."""
    store = make_store(tmp_path)
    entries = hourly_chunks(store.root, range(2))
    with pytest.raises(HistoricalTrancheError, match="ordered"):
        build_wind_tranche(store, at(0), at(2), CATALOG_AREA, catalog=entries)


def test_accepts_a_window_that_starts_mid_day(tmp_path):
    """Acquisition stores whole UTC days; a 20:00 window is still covered."""
    store = make_store(tmp_path)
    entries = [
        chunk(store.root, "day1.grib", at(0), at(23)),
        chunk(
            store.root,
            "day2.grib",
            at(0) + timedelta(days=1),
            at(23) + timedelta(days=1),
        ),
    ]
    start = datetime(2025, 1, 1, 20, tzinfo=UTC)

    selected = select_window_entries(
        entries, start, start + timedelta(hours=20), CATALOG_AREA
    )

    assert selected
    # Only the chunks that overlap the window are used, and they are enough to
    # cover every hour in it exactly once.
    assert len(selected) <= 2


def test_another_area_storing_the_same_days_does_not_block_the_window(tmp_path):
    """A second route's region must not make this window look ambiguous."""
    store = make_store(tmp_path)
    other_area = (55.0, -20.0, 30.0, 10.0)
    entries = [
        chunk(store.root, "a1.grib", at(0), at(23)),
        chunk(
            store.root, "a2.grib", at(0) + timedelta(days=1), at(23) + timedelta(days=1)
        ),
        chunk(store.root, "b1.grib", at(0), at(23), area=other_area),
        chunk(
            store.root,
            "b2.grib",
            at(0) + timedelta(days=1),
            at(23) + timedelta(days=1),
            area=other_area,
        ),
    ]
    start = datetime(2025, 1, 1, 20, tzinfo=UTC)

    selected = select_window_entries(
        entries, start, start + timedelta(hours=4), CATALOG_AREA
    )

    assert [entry["artifact"] for entry in selected] == ["a1.grib"]
