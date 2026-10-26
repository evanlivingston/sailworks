import pytest

from gweatherrouting.core.forecastrefresh import (
    RefreshCoordinator,
    RefreshExecutionError,
)
from gweatherrouting.core.tranchestore import TrancheStore
from test.test_forecastrefresh import FakeSource, config, coordinator


def test_download_path_must_remain_inside_owned_stage(tmp_path):
    external = tmp_path / "outside.grib"
    external.write_bytes(b"outside")

    class EscapingSource(FakeSource):
        def download(self, identifier, directory, progress, cancel=None):
            return str(external)

    service, _ = coordinator(TrancheStore(tmp_path / "store"), (config(),))
    service.factories = {"GFS": lambda: EscapingSource("GFS")}
    service.configure((config(),), "GFS")
    with pytest.raises(RefreshExecutionError, match="outside its owned stage"):
        service.refresh()
    assert not list((tmp_path / "store" / "manifests").glob("*.json"))
