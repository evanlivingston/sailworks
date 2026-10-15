"""Automatic source discovery for geographic areas, independent of station names."""

import tempfile
import threading
from dataclasses import dataclass
from datetime import timedelta

from .chs import DownloadCancelledError
from .models import utc_time
from .nosofs import NOSOFS_DOMAINS, NOSOFSProvider
from .spatialproviders import (
    CIOPS_DOMAINS,
    CIOPSProvider,
    CopernicusProvider,
    DownloadClient,
)


@dataclass(frozen=True)
class CoverageTask:
    provider: str
    domain: str
    area: object
    resolution_m: int
    name: str


class CoveragePlanner:
    def __init__(
        self, regional=None, worldwide=None, cache_directory=None, coastal=None
    ):
        self.regional = regional or CIOPSProvider()
        self.worldwide = worldwide or CopernicusProvider()
        self.coastal = coastal or NOSOFSProvider()
        self.cache_directory = cache_directory

    def plan(self, area):
        tasks = []
        for part in area.parts():
            for domain, (_, label, _, resolution, bounds) in CIOPS_DOMAINS.items():
                if part.intersects(bounds):
                    tasks.append(
                        CoverageTask(
                            "eccc-ciops", domain, part, resolution, "CIOPS-" + label
                        )
                    )
            for domain, (label, resolution, bounds) in NOSOFS_DOMAINS.items():
                if part.intersects(bounds):
                    tasks.append(
                        CoverageTask("noaa-nosofs", domain, part, resolution, label)
                    )
            tasks.append(
                CoverageTask(
                    "copernicus-marine",
                    "global",
                    part,
                    9000,
                    "Worldwide ocean currents + tides",
                )
            )
        return tuple(tasks)

    def download(self, area, start, end, cancel=None, progress=None):  # noqa: C901
        start, end = utc_time(start), utc_time(end)
        if not timedelta(0) < end - start <= timedelta(days=7):
            raise ValueError("Choose a coverage window up to seven days")
        cancel = cancel or threading.Event()
        fields, issues = [], []
        with tempfile.TemporaryDirectory(prefix="gwr-currents-") as directory:
            client = DownloadClient(
                directory, cancel, cache_directory=self.cache_directory
            )
            for task in self.plan(area):
                if cancel.is_set():
                    raise DownloadCancelledError()
                if progress:
                    progress("Downloading " + task.name)
                try:
                    if task.provider == "eccc-ciops":
                        field = self.regional.download(
                            task.domain, task.area, start, end, client, progress
                        )
                    elif task.provider == "noaa-nosofs":
                        field = self.coastal.download(
                            task.domain, task.area, start, end, client, progress
                        )
                    else:
                        field = self.worldwide.download(
                            task.area, start, end, directory, cancel, progress
                        )
                    fields.append(field)
                except DownloadCancelledError:
                    raise
                except Exception as error:
                    # A failed region must not hide usable coverage from other sources.
                    issues.append(task.name + ": " + str(error))
        if cancel.is_set():
            raise DownloadCancelledError()
        return tuple(fields), tuple(issues)
