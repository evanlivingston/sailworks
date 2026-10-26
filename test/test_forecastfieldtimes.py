from datetime import datetime, timezone

import pytest

from gweatherrouting.core.forecasttranche import Artifact, ModelEntry


def entry(field_times):
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    artifact = Artifact("artifacts/" + "a" * 64, "a" * 64, 1)
    return ModelEntry(
        "entry",
        "GFS",
        run,
        "deterministic",
        ("wind", "pressure"),
        ("10-HTGL", "0-MSL"),
        (0, 1, 2, 3),
        "0.25deg",
        (run, run.replace(hour=3)),
        (artifact,),
        run,
        field_times,
    )


def test_field_times_roundtrip_and_payload_is_immutable():
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    mutable_times = [run, run.replace(hour=3)]
    raw = [["wind", mutable_times], ["pressure", [run]]]
    value = entry(raw)
    mutable_times.clear()
    raw.clear()
    assert value.field_times == (
        ("wind", (run, run.replace(hour=3))),
        ("pressure", (run,)),
    )
    restored = ModelEntry.from_json(value.to_json())
    assert restored == value
    legacy = value.to_json()
    del legacy["field_times"]
    assert ModelEntry.from_json(legacy).field_times == ()


@pytest.mark.parametrize(
    "field_times",
    [
        (("wind", ()), ("pressure", (datetime(2025, 1, 1, tzinfo=timezone.utc),))),
        (("wind", (datetime(2025, 1, 1, tzinfo=timezone.utc),)),),
        (
            ("wind", (datetime(2025, 1, 1),)),
            ("pressure", (datetime(2025, 1, 1, tzinfo=timezone.utc),)),
        ),
        (
            (
                "wind",
                (
                    datetime(2025, 1, 1, tzinfo=timezone.utc),
                    datetime(2025, 1, 1, 3, tzinfo=timezone.utc),
                ),
            ),
            ("pressure", (datetime(2025, 1, 1, 6, tzinfo=timezone.utc),)),
        ),
        (
            ("wind", (datetime(2025, 1, 1, tzinfo=timezone.utc),)),
            ("pressure", (datetime(2025, 1, 1, tzinfo=timezone.utc),)),
            ("bogus", (datetime(2025, 1, 1, tzinfo=timezone.utc),)),
        ),
        (
            ("wind", (datetime(2025, 1, 1, tzinfo=timezone.utc),)),
            ("pressure", (datetime(2025, 1, 1, tzinfo=timezone.utc),)),
            ("pressure", (datetime(2025, 1, 1, 3, tzinfo=timezone.utc),)),
        ),
        (
            ("wind", (datetime(2025, 1, 1, tzinfo=timezone.utc), 3)),
            ("pressure", (datetime(2025, 1, 1, tzinfo=timezone.utc),)),
        ),
    ],
)
def test_invalid_per_field_coverage_rejected(field_times):
    with pytest.raises(ValueError):
        entry(field_times)
