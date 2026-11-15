"""ERA5 planning: unit deduplication, day splitting, availability, bindings."""

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import sailworks.core.historicalweather as weather_module
from sailworks.core.historicalbinding import CurrentBinding, WeatherBindingDescriptor
from sailworks.core.historicalrouting import HistoricalMemberPlan
from sailworks.core.historicalweather import (
    DEFAULT_REGION_PADDING_DEG,
    ERA5_EARLIEST_UTC,
    ERA5_LATENCY_HOURS,
    MAX_UNIT_DAYS,
    AcquisitionUnit,
    CoverageWindow,
    availability_reason,
    era5_area,
    estimate_decoded_bytes,
    merge_coverage,
    plan_acquisition_units,
    plan_member_availability,
    plan_year_exclusions,
    region_from_era5_area,
    region_from_points,
    split_dateline,
    split_region_at_dateline,
    split_unit_days,
    units_for_members,
)

UTC = timezone.utc
REGION = (-10.0, 40.0, 5.0, 50.0)
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=ERA5_LATENCY_HOURS)


def window(start, end):
    return CoverageWindow(start, end)


def moment(*parts):
    return datetime(*parts, tzinfo=UTC)


def make_member(year, offset, start, hours=240, member_id=None):
    return HistoricalMemberPlan(
        member_id=member_id or f"{year:04d}-{offset:+03d}",
        year=year,
        offset_days=offset,
        intended_wall_datetime=start.replace(tzinfo=None),
        resolved_departure_utc=start,
        calendar_adjustments=(),
        coverage_start_utc=start,
        coverage_end_utc=start + timedelta(hours=hours),
    )


def test_binding_names_are_reexported_from_weather():
    assert weather_module.CurrentBinding is CurrentBinding
    assert weather_module.WeatherBindingDescriptor is WeatherBindingDescriptor
    assert WeatherBindingDescriptor.__module__ == "sailworks.core.historicalbinding"


def test_coverage_window_requires_aware_utc_and_positive_span():
    with pytest.raises(ValueError):
        CoverageWindow(datetime(2020, 1, 1), datetime(2020, 1, 2))
    with pytest.raises(ValueError):
        CoverageWindow(moment(2020, 1, 1), moment(2020, 1, 1))
    with pytest.raises(ValueError):
        CoverageWindow(moment(2020, 1, 2), moment(2020, 1, 1))
    with pytest.raises(ValueError):
        CoverageWindow(
            datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=2))),
            datetime(2020, 1, 2, tzinfo=timezone(timedelta(hours=2))),
        )
    assert window(moment(2020, 1, 1), moment(2020, 1, 2)).hours() == 24.0


def test_merge_coverage_unions_overlapping_and_touching_windows():
    base = moment(2020, 1, 1)
    merged = merge_coverage(
        [
            window(base + timedelta(hours=6), base + timedelta(hours=12)),
            window(base, base + timedelta(hours=6)),
            window(base + timedelta(hours=20), base + timedelta(hours=30)),
            window(base + timedelta(hours=40), base + timedelta(hours=44)),
        ]
    )
    assert merged == (
        window(base, base + timedelta(hours=12)),
        window(base + timedelta(hours=20), base + timedelta(hours=30)),
        window(base + timedelta(hours=40), base + timedelta(hours=44)),
    )
    assert merge_coverage([]) == ()
    overlapping = merge_coverage(
        [
            window(base + timedelta(hours=5), base + timedelta(hours=10)),
            window(base, base + timedelta(hours=6)),
        ]
    )
    assert overlapping == (window(base, base + timedelta(hours=10)),)


def test_split_unit_days_splits_at_midnight_without_empty_pieces():
    start = moment(2019, 6, 1, 18)
    pieces = split_unit_days([window(start, start + timedelta(hours=12))])
    assert pieces == (
        (moment(2019, 6, 1, 18), moment(2019, 6, 2)),
        (moment(2019, 6, 2), moment(2019, 6, 2, 6)),
    )

    # Starts exactly at midnight, ends exactly at midnight: one whole-day piece.
    midnight_start = moment(2019, 6, 1)
    pieces = split_unit_days([window(midnight_start, midnight_start + timedelta(days=1))])
    assert pieces == ((midnight_start, moment(2019, 6, 2)),)

    # Ends exactly at midnight: the final piece ends there and nothing empty follows.
    pieces = split_unit_days([window(moment(2019, 6, 1, 20), moment(2019, 6, 2))])
    assert pieces == ((moment(2019, 6, 1, 20), moment(2019, 6, 2)),)
    assert all(end > begin for begin, end in pieces)
    assert all(begin.date() == (end - timedelta(microseconds=1)).date() for begin, end in pieces)

    daily = split_unit_days([window(moment(2019, 1, 1), moment(2019, 1, 4))], max_days=1)
    assert daily == (
        (moment(2019, 1, 1), moment(2019, 1, 2)),
        (moment(2019, 1, 2), moment(2019, 1, 3)),
        (moment(2019, 1, 3), moment(2019, 1, 4)),
    )
    assert all((end - begin) <= timedelta(days=MAX_UNIT_DAYS) for begin, end in daily)
    with pytest.raises(ValueError):
        split_unit_days([window(moment(2019, 1, 1), moment(2019, 1, 2))], max_days=0)


def test_acquisition_units_are_deterministic_and_deduplicated():
    start = moment(2019, 6, 1)
    end = start + timedelta(days=2)
    first = AcquisitionUnit.create(start_utc=start, end_utc=end, region=REGION)
    second = AcquisitionUnit.create(start_utc=start, end_utc=end, region=REGION)
    assert first.unit_id == second.unit_id
    assert len(first.unit_id) == 64
    assert first.utc_day == date(2019, 6, 1)
    assert first.fields == ("wind",)

    # Field order is canonicalised; field content is not.
    ordered = AcquisitionUnit.create(
        start_utc=start, end_utc=end, region=REGION, fields=("wind", "currents")
    )
    reordered = AcquisitionUnit.create(
        start_utc=start, end_utc=end, region=REGION, fields=("currents", "wind")
    )
    assert ordered.unit_id == reordered.unit_id
    assert ordered.unit_id != first.unit_id

    # A different region or span changes the id.
    moved = AcquisitionUnit.create(start_utc=start, end_utc=end, region=(-9.0, 40.0, 5.0, 50.0))
    assert moved.unit_id != first.unit_id
    longer = AcquisitionUnit.create(start_utc=start, end_utc=end + timedelta(days=1), region=REGION)
    assert longer.unit_id != first.unit_id

    payload = AcquisitionUnit.canonical_payload(start, end, REGION, ("wind",))
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode("utf8")).hexdigest() == first.unit_id

    with pytest.raises(ValueError):
        AcquisitionUnit(
            unit_id="not-a-digest",
            start_utc=start,
            end_utc=end,
            utc_day=date(2019, 6, 1),
            region=REGION,
            fields=("wind",),
        )

    units = plan_acquisition_units([window(start, end), window(start, end)], REGION)
    assert len(units) == 2  # one unit per UTC day, not per window
    assert [unit.start_utc for unit in units] == sorted(unit.start_utc for unit in units)
    assert {unit.unit_id for unit in units} == {
        AcquisitionUnit.create(
            start_utc=start, end_utc=start + timedelta(days=1), region=REGION
        ).unit_id,
        AcquisitionUnit.create(
            start_utc=start + timedelta(days=1), end_utc=end, region=REGION
        ).unit_id,
    }


def test_availability_boundaries_are_exact():
    assert (
        availability_reason(
            window(ERA5_EARLIEST_UTC, ERA5_EARLIEST_UTC + timedelta(days=1)), now=NOW
        )
        is None
    )

    before = window(ERA5_EARLIEST_UTC - timedelta(hours=1), ERA5_EARLIEST_UTC + timedelta(hours=5))
    reason = availability_reason(before, now=NOW)
    assert "before ERA5 begins" in reason
    assert "1940-01-01" in reason

    assert availability_reason(window(CUTOFF - timedelta(days=2), CUTOFF), now=NOW) is None
    late = availability_reason(
        window(CUTOFF - timedelta(days=2), CUTOFF + timedelta(hours=1)), now=NOW
    )
    assert "after the ERA5 availability cutoff" in late
    assert "120" in late or str(ERA5_LATENCY_HOURS) in late

    with pytest.raises(ValueError):
        availability_reason(
            window(moment(2020, 1, 1), moment(2020, 1, 2)), now=datetime(2026, 1, 1)
        )


def test_member_availability_and_year_exclusions():
    old = [
        make_member(1939, -1, moment(1939, 5, 31)),
        make_member(1939, 0, moment(1939, 6, 1)),
        make_member(2019, 0, moment(2019, 6, 1)),
    ]
    unavailable = plan_member_availability(old, now=NOW)
    assert set(unavailable) == {"1939--01", "1939-+00"}
    assert "1940-01-01" in unavailable["1939--01"]

    exclusions = plan_year_exclusions(old, now=NOW)
    assert [item.year for item in exclusions] == [1939]
    # The first offending member in (year, offset) order names the exclusion.
    assert exclusions[0].member_id == "1939--01"
    assert exclusions[0].offset_days == -1
    assert "before ERA5 begins" in exclusions[0].reason

    recent = [make_member(2026, 0, moment(2026, 6, 1), hours=6)]
    assert plan_year_exclusions(recent, now=NOW) == ()
    assert plan_member_availability(recent, now=NOW) == {}
    assert plan_year_exclusions(old, now=NOW, latency_hours=0)[0].reason


def test_region_from_points_pads_and_clips():
    region = region_from_points([(50.0, -5.0), (45.0, 10.0)])
    assert region == (-10.0, 40.0, 15.0, 55.0)
    assert region_from_points([(50.0, 0.0)], padding_deg=1.0) == (-1.0, 49.0, 1.0, 51.0)

    clipped = region_from_points([(89.0, 179.0)], padding_deg=DEFAULT_REGION_PADDING_DEG)
    assert clipped == (174.0, 84.0, 180.0, 90.0)
    assert region_from_points([(-89.0, -179.0)]) == (-180.0, -90.0, -174.0, -84.0)

    with pytest.raises(ValueError):
        region_from_points([])
    with pytest.raises(ValueError):
        region_from_points([(0.0, 0.0)], padding_deg=-1.0)
    # Zero padding around a single waypoint has no area to sample.
    with pytest.raises(ValueError, match="degenerate"):
        region_from_points([(50.0, 0.0)], padding_deg=0.0)


def test_region_from_points_refuses_a_dateline_span():
    with pytest.raises(ValueError, match="split_region_at_dateline"):
        region_from_points([(0.0, -179.0), (0.0, 179.0)])
    with pytest.raises(ValueError, match="split_region_at_dateline"):
        region_from_points([(10.0, -178.0), (-10.0, 178.0)], padding_deg=3.0)


def test_dateline_handling_is_explicit():
    assert split_dateline((170.0, -10.0, 175.0, 10.0)) == ((170.0, -10.0, 175.0, 10.0),)
    with pytest.raises(ValueError, match="more than 180"):
        split_dateline((-170.0, -10.0, 20.0, 10.0))
    with pytest.raises(ValueError):
        split_dateline((100.0, -10.0, -100.0, 10.0))

    boxes = split_region_at_dateline(170.0, -10.0, -170.0, 10.0)
    assert boxes == ((170.0, -10.0, 180.0, 10.0), (-180.0, -10.0, -170.0, 10.0))
    assert split_region_at_dateline(0.0, -10.0, 20.0, 10.0) == ((0.0, -10.0, 20.0, 10.0),)
    assert split_region_at_dateline(-180.0, -10.0, -170.0, 10.0) == ((-180.0, -10.0, -170.0, 10.0),)
    with pytest.raises(ValueError):
        split_region_at_dateline(0.0, 10.0, 10.0, -10.0)
    # A non-wrapping span over half the globe is refused, like split_dateline.
    with pytest.raises(ValueError, match="more than 180"):
        split_region_at_dateline(-170.0, -10.0, 20.0, 10.0)


def test_members_share_deduplicated_units():
    first = make_member(2019, 0, moment(2019, 6, 1), hours=12)
    second = make_member(
        2019,
        1,
        moment(2019, 6, 1, 12),
        hours=12,
        member_id="2019-adjacent",
    )
    mapping = units_for_members([second, first], REGION)
    assert set(mapping) == {"2019-+00", "2019-adjacent"}
    # Adjacent windows merge into one UTC day and both members reuse its unit.
    assert mapping["2019-+00"] == mapping["2019-adjacent"]
    assert len(mapping["2019-+00"]) == 1

    units = {
        unit.unit_id: unit
        for unit in plan_acquisition_units(
            [
                CoverageWindow(first.coverage_start_utc, first.coverage_end_utc),
                CoverageWindow(second.coverage_start_utc, second.coverage_end_utc),
            ],
            REGION,
        )
    }
    shared = units[mapping["2019-+00"][0]]
    assert shared.start_utc <= first.coverage_start_utc
    assert shared.end_utc >= second.coverage_end_utc

    far = make_member(2018, 0, moment(2018, 6, 1), hours=6)
    spread = units_for_members([first, far], REGION)
    assert spread["2018-+00"] != spread["2019-+00"]


def test_decoded_byte_estimate_and_binding_digest():
    start = moment(2019, 6, 1)
    units = plan_acquisition_units(
        [window(start, start + timedelta(days=1))], (0.0, 0.0, 10.0, 10.0)
    )
    assert len(units) == 1
    assert estimate_decoded_bytes(units, (0.0, 0.0, 10.0, 10.0)) == 24 * 40 * 40 * 2 * 4
    assert (
        estimate_decoded_bytes(units, (0.0, 0.0, 10.0, 10.0), components=1, bytes_per_value=2)
        == 24 * 40 * 40 * 1 * 2
    )

    binding = WeatherBindingDescriptor(
        binding_version=1,
        source_id="ERA5",
        wind_artifact_hashes=("a" * 64,),
        wind_coverage_start_utc=start,
        wind_coverage_end_utc=start + timedelta(days=10),
        wind_region=REGION,
        member_departure_utc=start + timedelta(hours=12),
        currents=CurrentBinding(enabled=False, target_departure_utc=None, coverage_end_utc=None),
        max_duration_hours=240,
    )
    twin = WeatherBindingDescriptor.from_dict(json.loads(json.dumps(binding.to_dict())))
    assert twin == binding
    assert twin.digest() == binding.digest()
    assert len(binding.digest()) == 64
    assert json.loads(json.dumps(binding.to_dict())) == binding.to_dict()

    changed = WeatherBindingDescriptor.from_dict(binding.to_dict())
    assert changed.digest() == binding.digest()
    different = WeatherBindingDescriptor(
        binding_version=2,
        source_id=binding.source_id,
        wind_artifact_hashes=binding.wind_artifact_hashes,
        wind_coverage_start_utc=binding.wind_coverage_start_utc,
        wind_coverage_end_utc=binding.wind_coverage_end_utc,
        wind_region=binding.wind_region,
        member_departure_utc=binding.member_departure_utc,
        currents=binding.currents,
        max_duration_hours=binding.max_duration_hours,
    )
    assert different.digest() != binding.digest()
    assert binding.to_dict()["wind_coverage_start_utc"].endswith("Z")


def make_binding(**overrides):
    start = moment(2019, 6, 1)
    values = {
        "binding_version": 1,
        "source_id": "ERA5",
        "wind_artifact_hashes": ("a" * 64,),
        "wind_coverage_start_utc": start,
        "wind_coverage_end_utc": start + timedelta(days=10),
        "wind_region": REGION,
        "member_departure_utc": start + timedelta(hours=12),
        "currents": CurrentBinding(False, None, None),
        "max_duration_hours": 240,
    }
    values.update(overrides)
    return WeatherBindingDescriptor(**values)


def test_binding_rejects_naive_times_and_bad_regions():
    with pytest.raises(ValueError):
        make_binding(member_departure_utc=datetime(2019, 6, 1, 12))
    with pytest.raises(ValueError):
        make_binding(wind_coverage_start_utc=datetime(2019, 6, 1))
    with pytest.raises(ValueError):
        make_binding(wind_region=(0.0, 1.0, 2.0))
    with pytest.raises(ValueError):
        make_binding(
            wind_coverage_start_utc=moment(2019, 6, 2),
            wind_coverage_end_utc=moment(2019, 6, 1),
        )
    with pytest.raises(ValueError):
        CurrentBinding(True, datetime(2019, 6, 1), None)
    assert CurrentBinding(False, None, None).enabled is False


def test_binding_parsers_reject_naive_and_normalize_offsets():
    payload = make_binding().to_dict()
    payload["member_departure_utc"] = "2019-06-01T12:00:00"
    with pytest.raises(ValueError):
        WeatherBindingDescriptor.from_dict(payload)

    payload["member_departure_utc"] = "2019-06-01T17:00:00+05:00"
    parsed = WeatherBindingDescriptor.from_dict(payload)
    assert parsed.member_departure_utc == datetime(2019, 6, 1, 12, tzinfo=UTC)
    assert parsed.member_departure_utc.utcoffset() == timedelta(0)

    payload["currents"] = {
        "enabled": True,
        "target_departure_utc": "2019-06-01T12:00:00",
        "coverage_end_utc": None,
    }
    with pytest.raises(ValueError):
        WeatherBindingDescriptor.from_dict(payload)


def test_generator_fields_are_coerced_once():
    start = moment(2019, 6, 1)
    fields = (name for name in ("wind", "currents"))
    units = plan_acquisition_units([window(start, start + timedelta(days=2))], REGION, fields)

    assert len(units) == 2
    assert all(unit.fields == ("wind", "currents") for unit in units)
    assert all(unit.region == tuple(round(value, 6) for value in REGION) for unit in units)
    ids = {unit.unit_id for unit in units}

    again = plan_acquisition_units(
        [window(start, start + timedelta(days=2))],
        REGION,
        (name for name in ("wind", "currents")),
    )
    assert {unit.unit_id for unit in again} == ids

    member = make_member(2019, 0, start, hours=24)
    mapping = units_for_members([member], REGION, (name for name in ("wind",)))
    expected = plan_acquisition_units([window(start, start + timedelta(days=1))], REGION, ("wind",))
    assert mapping["2019-+00"] == tuple(unit.unit_id for unit in expected)


def test_max_days_cap_landing_on_midnight_emits_no_empty_piece():
    start = moment(2019, 1, 1)
    # The one-day cap and the UTC midnight coincide exactly: one piece, no tail.
    assert split_unit_days([window(start, start + timedelta(days=1))], max_days=1) == (
        (start, moment(2019, 1, 2)),
    )

    pieces = split_unit_days([window(start, start + timedelta(days=3))], max_days=1)
    assert pieces == (
        (start, moment(2019, 1, 2)),
        (moment(2019, 1, 2), moment(2019, 1, 3)),
        (moment(2019, 1, 3), moment(2019, 1, 4)),
    )
    assert all(end > begin for begin, end in pieces)
    assert len(pieces) == len(set(pieces))


def test_era5_area_converts_our_region_order():
    # (west, south, east, north) -> ERA5's (north, west, south, east).
    assert era5_area((-130.0, 41.0, -118.4, 53.0)) == (53.0, -130.0, 41.0, -118.4)
    assert era5_area(REGION) == (50.0, -10.0, 40.0, 5.0)


def test_region_from_era5_area_inverts_the_conversion():
    assert region_from_era5_area((53.0, -130.0, 41.0, -118.4)) == (
        -130.0,
        41.0,
        -118.4,
        53.0,
    )
    assert region_from_era5_area(era5_area(REGION)) == REGION


@pytest.mark.parametrize(
    "bad",
    [(-1.0, 2.0), (-1.0, 2.0, 3.0, 4.0, 5.0), ("a", "b", "c", "d"), None],
)
def test_era5_area_rejects_malformed_regions(bad):
    with pytest.raises(ValueError):
        era5_area(bad)


def test_era5_area_does_not_silently_reorder_catalog_order():
    # Catalog order fed to a region helper is a caller bug, not a reorder.
    with pytest.raises(ValueError, match="ordered"):
        era5_area((50.0, -10.0, 40.0, 5.0))


@pytest.mark.parametrize(
    "bad",
    [
        (50.0, -10.0, 5.0),  # wrong length
        (41.0, -130.0, 53.0, -118.4),  # our order, not ERA5's
        (5.0, -10.0, 10.0, 50.0),  # north <= south
    ],
)
def test_region_from_era5_area_rejects_out_of_order_areas(bad):
    with pytest.raises(ValueError):
        region_from_era5_area(bad)


def _hourly_catalog(root, start, hours, area):
    import json

    entries = []
    for offset in range(hours):
        moment = start + timedelta(hours=offset)
        name = f"h{offset}.grib"
        (root / name).write_bytes(name.encode("ascii"))
        entries.append(
            {
                "artifact": name,
                "sha256": "0" * 64,
                "size": len(name),
                "start": moment.isoformat(),
                "end": moment.isoformat(),
                "area": list(area),
                "variables": ["10m_u_component_of_wind", "10m_v_component_of_wind"],
            }
        )
    return entries


def test_era5_coverage_reads_the_catalog_in_era5_order(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import sailworks.qt.historicalera5 as adapter

    start = datetime(2026, 5, 1, tzinfo=UTC)
    unit = SimpleNamespace(start_utc=start, end_utc=start + timedelta(hours=2), region=REGION)
    entries = _hourly_catalog(tmp_path, start, 2, (50.0, -10.0, 40.0, 5.0))
    (tmp_path / "era5-history.json").write_text(json.dumps(entries), encoding="utf8")

    assert adapter.covered(tmp_path, unit) is True


def test_era5_coverage_plans_in_era5_order(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import sailworks.qt.historicalera5 as adapter

    start = datetime(2026, 5, 1, tzinfo=UTC)
    unit = SimpleNamespace(start_utc=start, end_utc=start + timedelta(hours=6), region=REGION)
    plans = []

    class Thread:
        era5_key = "k"

        def is_alive(self):
            return False

    class Service:
        def submit(self, plan):
            plans.append(plan)
            return Thread()

        def cancel(self, plan):
            return None

        def key_for(self, plan):
            return "k"

        def error_for(self, key):
            return None

        def clear_error(self, key):
            return None

    # The first coverage check misses; the post-download one succeeds.
    answers = iter([False, True])
    monkeypatch.setattr(adapter, "covered", lambda root, unit: next(answers))
    coverage = adapter.Era5Coverage(
        SimpleNamespace(root=tmp_path), service_factory=lambda store: Service()
    )

    coverage.ensure([unit])

    assert len(plans) == 1
    north, west, south, east = plans[0].area
    assert (north, west, south, east) == (50.0, -10.0, 40.0, 5.0)
    assert north > south and west < east
