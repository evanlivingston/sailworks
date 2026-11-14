"""Historical-year ensemble statistics: completion, quantiles and bootstrap."""

import pytest

from sailworks.core.historicalroutingstats import (
    COMPLETE_OUTCOMES,
    MemberResult,
    complete_years,
    completion_curve,
    completion_label,
    duration_hours,
    quantile,
    representative,
    sample_guardrails,
    target_completion,
    year_block_bootstrap,
)


def make_member(year: int, offset_days: int, outcome: str, duration=None) -> MemberResult:
    return MemberResult(
        member_id=f"{year}-{offset_days:+03d}",
        year=year,
        offset_days=offset_days,
        outcome=outcome,
        duration_hours=duration,
    )


def make_year(year: int, offset_days: int, specs) -> tuple[MemberResult, ...]:
    offsets = list(range(-offset_days, offset_days + 1))
    assert len(specs) == len(offsets)
    members = []
    for offset, (outcome, duration) in zip(offsets, specs):
        members.append(make_member(year, offset, outcome, duration))
    return tuple(members)


def test_complete_year_detection_requires_every_offset_and_determinate_outcomes():
    offset_days = 1
    results = []
    results.extend(make_year(2020, offset_days, [("finished", 10.0)] * 3))
    results.extend(
        [make_member(2021, -1, "finished", 10.0), make_member(2021, 0, "finished", 12.0)]
    )
    results.extend(
        make_year(
            2022,
            offset_days,
            [("finished", 10.0), ("no_finish", None), ("failed", None)],
        )
    )
    results.extend(make_year(2023, offset_days, [("no_finish", None)] * 3))
    years, mapping = complete_years(results, offset_days)
    assert years == [2020, 2023]
    assert sorted(mapping) == [2020, 2023]
    assert mapping[2020] == make_year(2020, offset_days, [("finished", 10.0)] * 3)
    assert [item.offset_days for item in mapping[2023]] == [-1, 0, 1]
    assert set(COMPLETE_OUTCOMES) == {"finished", "no_finish"}


def test_complete_year_detection_rejects_duplicate_offsets():
    offset_days = 1
    four = [
        make_member(2020, -1, "finished", 10.0),
        make_member(2020, 0, "finished", 10.0),
        make_member(2020, 0, "finished", 10.0),
        make_member(2020, 1, "finished", 10.0),
    ]
    assert complete_years(four, offset_days)[0] == []
    three = [
        make_member(2021, -1, "finished", 10.0),
        make_member(2021, 0, "finished", 10.0),
        make_member(2021, 0, "finished", 10.0),
    ]
    assert complete_years(three, offset_days)[0] == []


def test_completion_curve_is_monotone_and_keeps_no_finish_mass():
    offset_days = 1
    mapping = {
        2020: make_year(
            2020, offset_days, [("finished", 10.0), ("finished", 20.0), ("finished", 30.0)]
        ),
        2021: make_year(2021, offset_days, [("no_finish", None)] * 3),
    }
    curve = completion_curve(mapping, offset_days, 60.0)
    assert len(curve) == 11
    assert curve[0] == (0.0, 0.0)
    assert curve[-1] == (60.0, 0.5)
    fractions = [fraction for _, fraction in curve]
    assert fractions == sorted(fractions)
    assert all(0.0 <= fraction <= 1.0 for fraction in fractions)
    assert fractions[-1] < 1.0

    zero_start = {
        2020: make_year(
            2020, offset_days, [("finished", 0.0), ("finished", 10.0), ("finished", 20.0)]
        ),
        2021: make_year(2021, offset_days, [("no_finish", None)] * 3),
    }
    assert completion_curve(zero_start, offset_days, 60.0)[0][1] == pytest.approx(1 / 6)


def test_completion_curve_endpoint_may_be_off_grid():
    mapping = {
        2020: make_year(2020, 1, [("finished", 10.0), ("finished", 61.0), ("no_finish", None)])
    }
    curve = completion_curve(mapping, 1, 61, 6.0)
    assert [moment for moment, _ in curve] == [
        0.0,
        6.0,
        12.0,
        18.0,
        24.0,
        30.0,
        36.0,
        42.0,
        48.0,
        54.0,
        60.0,
        61.0,
    ]
    fractions = [fraction for _, fraction in curve]
    assert fractions == sorted(fractions)
    assert curve[-1][1] == pytest.approx(2 / 3)
    with pytest.raises(ValueError):
        completion_curve(mapping, 1, float("inf"))
    with pytest.raises(ValueError):
        completion_curve(mapping, 1, float("nan"))
    with pytest.raises(ValueError):
        completion_curve(mapping, 1, 60.0, 0.0)


def test_years_are_weighted_equally():
    offset_days = 1
    mapping = {
        2020: make_year(2020, offset_days, [("finished", 10.0)] * 3),
        2021: make_year(2021, offset_days, [("no_finish", None)] * 3),
    }
    assert target_completion(mapping, offset_days, 168.0) == pytest.approx(0.5)
    assert completion_curve(mapping, offset_days, 168.0)[-1][1] == pytest.approx(0.5)


def test_quantile_uses_type_seven_interpolation():
    assert quantile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)
    assert quantile([4, 3, 2, 1], 0.5) == pytest.approx(2.5)
    assert quantile([1, 2], 0.0) == pytest.approx(1.0)
    assert quantile([1, 2], 1.0) == pytest.approx(2.0)
    assert quantile([], 0.5) is None
    with pytest.raises(ValueError):
        quantile([1, 2, 3], -0.1)
    with pytest.raises(ValueError):
        quantile([1, 2, 3], 1.1)


def test_representative_picks_an_observed_member_with_the_documented_tie_break():
    offset_days = 1
    mapping = {
        2019: make_year(
            2019, offset_days, [("finished", 10.0), ("finished", 20.0), ("finished", 30.0)]
        ),
        2020: make_year(
            2020, offset_days, [("finished", 40.0), ("finished", 50.0), ("finished", 60.0)]
        ),
    }
    chosen = representative(mapping, offset_days, 0.5)
    assert chosen is not None
    assert chosen in mapping[2019]
    assert chosen.year == 2019
    assert chosen.offset_days == 1
    assert chosen.duration_hours == 30.0
    assert representative(mapping, offset_days, 0.0).duration_hours == 10.0
    assert representative(mapping, offset_days, 1.0).duration_hours == 60.0
    unfinished = {2019: make_year(2019, offset_days, [("no_finish", None)] * 3)}
    assert representative(unfinished, offset_days, 0.5) is None


def test_target_completion_on_a_hand_built_fixture():
    offset_days = 1
    mapping = {
        2020: make_year(2020, offset_days, [("finished", 10.0)] * 3),
        2021: make_year(
            2021, offset_days, [("finished", 10.0), ("no_finish", None), ("no_finish", None)]
        ),
    }
    assert target_completion(mapping, offset_days, 100.0) == pytest.approx(2 / 3)
    assert target_completion(mapping, offset_days, 5.0) == 0.0
    assert target_completion({}, offset_days, 100.0) == 0.0


def test_year_block_bootstrap_needs_samples_and_variation():
    offset_days = 1
    single = {2020: make_year(2020, offset_days, [("finished", 10.0)] * 3)}
    assert year_block_bootstrap(single, offset_days, 168.0) is None

    nine_years = {}
    for index, year in enumerate(range(2011, 2020)):
        finished = 1 + index % 3
        specs = [("finished", 10.0)] * finished + [("no_finish", None)] * (3 - finished)
        nine_years[year] = make_year(year, offset_days, specs)
    assert year_block_bootstrap(nine_years, offset_days, 168.0) is None

    uniform = {}
    for year in range(2009, 2021):
        uniform[year] = make_year(year, offset_days, [("finished", 10.0)] * 3)
    assert year_block_bootstrap(uniform, offset_days, 168.0) is None


def test_year_block_bootstrap_is_deterministic_and_brackets_the_point_estimate():
    offset_days = 1
    mapping = {}
    for index, year in enumerate(range(2009, 2021)):
        finished = 1 + index % 3
        specs = [("finished", 10.0)] * finished + [("no_finish", None)] * (3 - finished)
        mapping[year] = make_year(year, offset_days, specs)
    point = target_completion(mapping, offset_days, 168.0)
    first = year_block_bootstrap(mapping, offset_days, 168.0, replicates=2000, seed=7)
    second = year_block_bootstrap(mapping, offset_days, 168.0, replicates=2000, seed=7)
    assert first is not None
    assert first == second
    lower, upper = first
    assert lower <= point <= upper
    assert lower < upper


def test_sample_guardrails_bands():
    assert sample_guardrails(0) == "none"
    assert sample_guardrails(1) == "descriptive"
    assert sample_guardrails(4) == "descriptive"
    assert sample_guardrails(5) == "target_only"
    assert sample_guardrails(9) == "target_only"
    assert sample_guardrails(10) == "interval"
    assert sample_guardrails(19) == "interval"
    assert sample_guardrails(20) == "tails"
    assert sample_guardrails(64) == "tails"


def test_completion_label_formatting():
    assert completion_label(8, 168) == "Router completed within 168 h across 8 complete years"
    assert completion_label(1, 72) == "Router completed within 72 h across 1 complete year"


def test_duration_hours_rejects_missing_and_negative_values():
    with pytest.raises(ValueError):
        duration_hours(make_member(2020, 0, "finished", None))
    with pytest.raises(ValueError):
        duration_hours(make_member(2020, 0, "finished", -1.0))
    assert duration_hours(make_member(2020, 0, "finished", 12.5)) == 12.5
    assert duration_hours(make_member(2020, 0, "no_finish", None)) is None
    assert duration_hours(make_member(2020, 0, "failed", None)) is None
