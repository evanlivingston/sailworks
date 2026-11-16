from datetime import datetime

from sailworks.core.geo.timerange import (
    FALLBACK_SPEED_KMH,
    MINIMUM_SPAN_SECONDS,
    clamp_bounds,
    element_time_bounds,
    point_time,
    selection_time_bounds,
    to_epoch,
)


class Element:
    def __init__(self, points=(), length_nm=0.0, default_departure=None, **attrs):
        self.points = list(points)
        self.length_nm = length_nm
        self.default_departure = default_departure
        for name, value in attrs.items():
            setattr(self, name, value)

    def length(self):
        return self.length_nm


def test_to_epoch_accepts_datetimes_numbers_and_iso_strings():
    stamp = datetime(2026, 6, 1, 9, 0, 0).timestamp()

    assert to_epoch(datetime(2026, 6, 1, 9, 0, 0)) == stamp
    assert to_epoch(stamp) == stamp
    assert to_epoch("2026-06-01T09:00:00") == stamp
    assert to_epoch(None) is None
    assert to_epoch("not a time") is None
    assert to_epoch([1]) is None


def test_point_times_win_over_other_time_sources():
    start = 1_780_000_000.0
    element = Element(
        points=[(51.0, -1.0, start), (52.0, -2.0, start + 7200.0)],
        length_nm=25.0,
        default_departure="2026-06-01T09:00:00",
        timestamps=[start + 86_400.0],
    )

    assert point_time(element.points[0]) == start
    assert point_time((51.0, -1.0)) is None
    assert element_time_bounds(element) == (start, start + 7200.0)


def test_departure_and_length_derive_an_end_at_the_fallback_speed():
    start = datetime(2026, 6, 1, 9, 0, 0).timestamp()
    element = Element(length_nm=25.0, default_departure="2026-06-01T09:00:00")

    start_epoch, end_epoch = element_time_bounds(element)

    assert FALLBACK_SPEED_KMH == 2.0
    assert start_epoch == start
    # 25 nm at 2 km/h: 23.15 h.
    assert end_epoch - start_epoch == 83_340.0


def test_timestamps_are_used_when_points_carry_no_time():
    start = 1_780_000_000.0
    element = Element(
        points=[(51.0, -1.0), (52.0, -2.0)],
        timestamps=[start + 50.0, start, start + 100.0],
    )

    assert element_time_bounds(element) == (start, start + 100.0)


def test_element_without_time_data_has_no_bounds():
    assert element_time_bounds(Element(points=[(51.0, -1.0)])) is None
    assert element_time_bounds(Element(default_departure=None)) is None


def test_selection_unions_element_bounds():
    first = 1_780_000_000.0
    second = first + 10_000.0
    elements = [
        Element(timestamps=[first + 60.0, first]),
        Element(timestamps=[second + 60.0, second]),
        Element(points=[(51.0, -1.0)]),
    ]

    assert selection_time_bounds(elements) == (first, second + 60.0)
    assert selection_time_bounds([Element(points=[(51.0, -1.0)])]) is None


def test_zero_width_bounds_get_a_minimum_span():
    start = 1_780_000_000.0
    element = Element(points=[(51.0, -1.0, start), (52.0, -2.0, start)])
    departure = datetime(2026, 6, 1, 9, 0, 0).timestamp()
    point_only = Element(default_departure="2026-06-01T09:00:00")

    assert element_time_bounds(element) == (start, start + MINIMUM_SPAN_SECONDS)
    assert element_time_bounds(point_only) == (
        departure,
        departure + MINIMUM_SPAN_SECONDS,
    )


def test_to_epoch_parses_the_wind_only_routing_time_format():
    assert (
        to_epoch("11/08/2026, 12:01:00") == datetime(2026, 11, 8, 12, 1, 0).timestamp()
    )


def test_clamp_bounds_keeps_a_region_inside_the_limit():
    assert clamp_bounds((10.0, 20.0), (0.0, 100.0)) == (10.0, 20.0)
    assert clamp_bounds((10.0, 20.0), (10.0, 20.0)) == (10.0, 20.0)
    assert clamp_bounds((0.0, 500.0), (100.0, 200.0)) == (100.0, 200.0)


def test_clamp_bounds_trims_partial_overlap_at_either_end():
    assert clamp_bounds((10.0, 20.0), (15.0, 100.0)) == (15.0, 20.0)
    assert clamp_bounds((10.0, 20.0), (0.0, 12.0)) == (10.0, 12.0)


def test_clamp_bounds_reports_disjoint_and_unknown_regions():
    # Decades of history against a current forecast have nothing to show.
    assert clamp_bounds((10.0, 20.0), (30.0, 40.0)) is None
    assert clamp_bounds((30.0, 40.0), (10.0, 20.0)) is None
    assert clamp_bounds(None, (0.0, 1.0)) is None


def test_clamp_bounds_leaves_a_region_alone_without_a_limit():
    assert clamp_bounds((10.0, 20.0), None) == (10.0, 20.0)
    assert clamp_bounds(None, None) is None
