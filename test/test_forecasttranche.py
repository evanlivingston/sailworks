from datetime import datetime, timezone

import pytest

from gweatherrouting.core.forecasttranche import (
    AnalysisSelection,
    Artifact,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)


def fixture_tranche(artifact):
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    config = ModelConfiguration(
        "GFS", ("wind",), (0, 1, 2, 3), "native", 6, (0, 3, 6), 3
    )
    entry = ModelEntry(
        "gfs-run",
        "GFS",
        run,
        "deterministic",
        ("wind",),
        ("10m",),
        (0, 1, 2, 3),
        "0.25deg",
        (run, run.replace(hour=3), run.replace(hour=6)),
        (artifact,),
        run,
    )
    return ForecastTranche("batch-1", run, (config,), (entry,))


def test_roundtrip_and_nested_immutability():
    a = Artifact("artifacts/" + "a" * 64, "a" * 64, 2)
    tranche = fixture_tranche(a)
    assert ForecastTranche.from_json(tranche.to_json()) == tranche
    with pytest.raises(ValueError):
        ForecastTranche.from_json({**tranche.to_json(), "version": 2})
    with pytest.raises((AttributeError, TypeError)):
        tranche.entries[0].fields[0] = "pressure"
    original = ["wind"]
    config = ModelConfiguration(
        "GFS", original, [0, 1, 2, 3], "native", 6, [0, 3, 6], 3
    )
    original[0] = "pressure"
    assert config.fields == ("wind",)


def test_config_default_steps_and_rejects_invalid_values():
    config = ModelConfiguration("GFS", ["wind"], [0, 170, 10, -170], "native", 6)
    assert config.steps == ()
    for region in ([float("nan"), 0, 1, 1], [0, 0, 0, 1], [0, 0, 1, 0]):
        with pytest.raises(ValueError):
            ModelConfiguration("GFS", ["wind"], region, "native", 6)
    with pytest.raises(ValueError):
        ModelConfiguration("GFS", ["unsupported"], [0, 0, 1, 1], "native", 6)
    with pytest.raises(ValueError):
        ModelConfiguration("GFS", "wind", [0, 0, 1, 1], "native", 6)


def test_reject_invalid_ids_times_and_duplicate_entries():
    with pytest.raises(ValueError):
        AnalysisSelection("../bad", "entry")
    a = Artifact("artifacts/" + "a" * 64, "a" * 64, 2)
    t = fixture_tranche(a)
    with pytest.raises(ValueError):
        ForecastTranche("batch-2", datetime.now(), t.configurations, t.entries)
    with pytest.raises(ValueError):
        ForecastTranche("batch-2", t.refreshed_at, t.configurations, t.entries * 2)
    with pytest.raises(ValueError):
        Artifact("../escape", "a" * 64, 2)


def test_required_acquisition_and_region_resolution_consistency():
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    artifact = Artifact("artifacts/" + "b" * 64, "b" * 64, 1)
    with pytest.raises(TypeError):
        ModelEntry(
            "entry",
            "GFS",
            run,
            "control",
            ("wind",),
            ("10m",),
            (0, -180, 10, 180),
            "0.25deg",
            (run,),
            (artifact,),
        )
    base = fixture_tranche(artifact)
    assert base.entries[0].acquired_at == run
    for actual, resolution in (
        ((1, 1, 10, 3), "0.25deg"),
        ((0, 4, 10, 8), "0.25deg"),
        ((0, 1, 10, 3), "1deg"),
    ):
        with pytest.raises(ValueError):
            entry = ModelEntry(
                "entry",
                "GFS",
                run,
                "control",
                ("wind",),
                ("10m",),
                actual,
                resolution,
                (run,),
                (artifact,),
                run,
            )
            ForecastTranche("other", run, base.configurations, (entry,))
    config = ModelConfiguration(
        "GFS", ("wind",), (0, 170, 10, -170), "native", 0, (), 0
    )
    containing = ModelEntry(
        "crossing",
        "GFS",
        run,
        "control",
        ("wind",),
        ("10m",),
        (-1, 160, 11, -160),
        "1deg",
        (run,),
        (artifact,),
        run,
    )
    ForecastTranche("crossing", run, (config,), (containing,))
    with pytest.raises(ValueError):
        outside = ModelEntry(
            "outside",
            "GFS",
            run,
            "control",
            ("wind",),
            ("10m",),
            (-1, 160, 11, 170),
            "1deg",
            (run,),
            (artifact,),
            run,
        )
        ForecastTranche("outside", run, (config,), (outside,))


def test_acquisition_is_required_aware_and_bounded_by_refresh():
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    artifact = Artifact("artifacts/" + "c" * 64, "c" * 64, 1)
    with pytest.raises(TypeError):
        ModelEntry(
            "entry",
            "GFS",
            run,
            "control",
            ("wind",),
            ("10m",),
            (0, 1, 2, 3),
            "0.25deg",
            (run,),
            (artifact,),
        )
    with pytest.raises(ValueError):
        ModelEntry(
            "entry",
            "GFS",
            run,
            "control",
            ("wind",),
            ("10m",),
            (0, 1, 2, 3),
            "0.25deg",
            (run,),
            (artifact,),
            datetime(2025, 1, 1),
        )
    entry = ModelEntry(
        "entry",
        "GFS",
        run,
        "control",
        ("wind",),
        ("10m",),
        (0, 1, 2, 3),
        "0.25deg",
        (run,),
        (artifact,),
        run,
    )
    config = ModelConfiguration("GFS", ("wind",), (0, 1, 2, 3), "native", 0, (), 0)
    with pytest.raises(ValueError):
        ForecastTranche("early", run.replace(day=1), (config,), (entry,), version=True)
    too_late = ModelEntry(
        "late",
        "GFS",
        run,
        "control",
        ("wind",),
        ("10m",),
        (0, 1, 2, 3),
        "0.25deg",
        (run,),
        (artifact,),
        run.replace(day=2),
    )
    with pytest.raises(ValueError):
        ForecastTranche("early", run, (config,), (too_late,))
    refreshed = run.replace(day=2)
    ForecastTranche("later", refreshed, (config,), (entry,))
