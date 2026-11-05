"""Wave-only GRIB metadata must still produce a display source."""

from datetime import datetime, timezone
from types import SimpleNamespace

from sailworks.rendering.displayforecast import inspect_forecasts

RUN = datetime(2025, 1, 1, tzinfo=timezone.utc)


class FakeRecord:
    def __init__(self, valid):
        self.reference = RUN.timestamp()
        self.valid = valid
        self.identity = (RUN.timestamp(), ("7", "0", ()), ("0", "0", "wave"), (), "")


class FakeGrib:
    def __init__(self, *args):
        self.path = args[0]
        self.sampling = SimpleNamespace(
            records=(FakeRecord(RUN.timestamp()), FakeRecord(RUN.timestamp() + 10800))
        )

    @staticmethod
    def parse_metadata(path):
        return SimpleNamespace(
            path=str(path),
            name="GFSWAVE.grib",
            centre="NOAA",
            bounds=(0, 0, 1, 1),
            start_time=RUN,
            last_forecast=0.0,
            model_name="GFSWAVE",
        )

    def close(self):
        pass


def test_wave_only_grib_builds_a_display_source(tmp_path, monkeypatch):
    import sailworks.core.grib as grib

    monkeypatch.setattr(grib, "Grib", FakeGrib)
    path = tmp_path / "gfswave.grib2"
    path.write_bytes(b"wave")
    forecasts, errors = inspect_forecasts(((str(path), ("a" * 64, 4)),))
    assert errors == ""
    assert len(forecasts) == 1
    forecast = forecasts[0]
    assert forecast.model_name == "GFSWAVE"
    assert forecast.first_valid == RUN.timestamp()
    assert forecast.last_valid == RUN.timestamp() + 10800
    assert forecast.valid_times == (RUN.timestamp(), RUN.timestamp() + 10800)
