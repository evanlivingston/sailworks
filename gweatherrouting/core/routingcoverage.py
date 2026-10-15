"""The existing current-routing preparation with toolkit-neutral UI callbacks."""

from weatherrouting import RoutingNoWindError
from .marine.chs import DownloadCancelledError
from .marine.preflight import check_current_coverage
from .marine.prefetch import CoveragePrefetch
from .marine.routing import DataGap
from .marine.windcoverage import WindCoverageRecovery


def prepare_coverage(
    routing,
    manager,
    auto_download,
    cancel,
    report,
    confirm,
    save_gribs,
    forecast_selection=None,
):
    environment = routing.current_environment
    if environment is None:
        return
    environment.cancel = cancel
    if auto_download or forecast_selection is not None:
        recovery = (
            WindCoverageRecovery(routing, save=save_gribs)
            if forecast_selection is None
            else WindCoverageRecovery(
                routing, save=save_gribs, selection=forecast_selection
            )
        )
        environment.wind_recovery = recovery
        report("Checking latest wind forecast…")
        recovery.refresh()
        gaps = recovery.missing(
            [DataGap(tuple(routing.position), routing.time, "wind", "no_coverage")]
        )
        if gaps:
            report("Downloading wind data for departure…")
            recovery.recover(gaps)
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
    if environment.wind_recovery is not None:
        environment.wind_recovery.prefetch()
