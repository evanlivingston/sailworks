"""The all-points oracle must open the selected GRIB, not sample metadata."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def gate():
    path = Path(__file__).resolve().parents[1] / "tools/check_all_points_wind.py"
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("all_points_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize("wind", [True, False])
def test_oracle_opens_filtered_reader_and_closes_it(gate, monkeypatch, wind):
    from gweatherrouting.core import grib, gribsampling
    from gweatherrouting.rendering import weathercache

    choice = SimpleNamespace(path="fixture.grib2", display_identity=(123, ("member",)))
    metadata = SimpleNamespace(
        path=choice.path,
        name="HRRR",
        centre="7",
        bounds=(),
        start_time=123,
        last_forecast=18,
        model_name="hrrr",
    )
    monkeypatch.setattr(grib.Grib, "parse_metadata", Mock(return_value=metadata))
    source = SimpleNamespace(close=Mock())
    reader = Mock(return_value=source)
    monkeypatch.setattr(weathercache, "DisplayWeatherSource", reader)
    sampler = SimpleNamespace(
        indices=[
            SimpleNamespace(
                records=[SimpleNamespace(element="UGRD" if wind else "PRMSL")]
            )
        ]
    )
    sample = Mock(return_value=sampler)
    monkeypatch.setattr(gribsampling, "WeatherSampler", sample)
    if wind:
        with gate.selected_wind_sampler(choice) as result:
            assert result == (sampler, source)
            source.close.assert_not_called()
    else:
        with pytest.raises(ValueError, match="no gridded wind"):
            with gate.selected_wind_sampler(choice):
                pytest.fail("A pressure-only source must not satisfy the oracle")
    sample.assert_called_once_with((source,))
    assert reader.call_args.kwargs["display_identity"] == choice.display_identity
    assert reader.call_args.args[0] == choice.path
    source.close.assert_called_once_with()


def test_readiness_rejects_pending_wrong_source_and_stale_time(gate):
    choice = SimpleNamespace(
        key="selected", path="fixture.grib2", display_identity=(1, 2)
    )
    selection = SimpleNamespace(
        loading=False, selected_key=choice.key, sources=lambda: (choice,)
    )
    controller = SimpleNamespace(
        display_forecast=selection, time_control=SimpleNamespace(time=123)
    )
    presenter = SimpleNamespace(
        _source=choice,
        widget=SimpleNamespace(
            _camera=SimpleNamespace(time=123),
            _model=SimpleNamespace(paths=(choice.path,), display_identity=(1, 2)),
        ),
    )
    assert gate.current_forecast(controller, presenter, choice)
    selection.loading = True
    assert not gate.current_forecast(controller, presenter, choice)
    selection.loading = False
    presenter._source = None
    assert not gate.current_forecast(controller, presenter, choice)
    presenter._source = choice
    presenter.widget._camera.time = 122
    assert not gate.current_forecast(controller, presenter, choice)
    presenter.widget._camera.time = 123
    presenter.widget._model.display_identity = (1, 3)
    assert not gate.current_forecast(controller, presenter, choice)
