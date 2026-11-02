"""The existing current-routing preparation with toolkit-neutral UI callbacks."""

from weatherrouting import RoutingNoWindError
from .marine.chs import DownloadCancelledError
from .marine.preflight import check_current_coverage
from .marine.prefetch import CoveragePrefetch


def prepare_coverage(
    routing,
    manager,
    auto_download,
    cancel,
    report,
    confirm,
):
    environment = routing.current_environment
    if environment is None:
        return
    environment.cancel = cancel
    if routing.grib.get_wind_at(routing.time, *routing.position) is None:
        raise RoutingNoWindError(
            "Load a wind forecast covering the departure and start position."
        )
    report("Checking current coverage…")
    coverage = check_current_coverage(routing, auto_download)
    if coverage.needs_warning and (not confirm(coverage) or coverage.blocked):
        raise DownloadCancelledError()
    if cancel.is_set():
        raise DownloadCancelledError()
    if auto_download:
        environment.prefetch = CoveragePrefetch(
            manager, environment, coverage.download_gaps
        )
