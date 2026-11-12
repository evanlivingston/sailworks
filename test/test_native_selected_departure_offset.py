"""Known open axis from #162: the +3 h departure scenario on the frozen fixture.

The frozen synthetic forecast covers hours 0, 3 and 6. A three-hour departure
offset leaves only 2.5 h of usable horizon for the same destination, and the
native engine returns the structured ``no_feasible_route`` outcome. This is an
expected calculation outcome: the worker publishes it, it is not a crash or a
dropped result, and the schema keeps it non-delivered and non-exportable.

The test records the input identity (start, destination, departure, scenario,
forecast/polar fingerprints and the remaining duration window) so the outcome
can be reproduced without inventing a route or an ETA.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR))

from test_native_routing_fixture import START  # noqa: E402
from test_native_selected_worker import (  # noqa: E402
    BUILD_PYTHON,
    LegacyAlgorithm,
    _forecast_inputs,
    _job,
    _selected_forecast,
    _wait,
)

from sailworks.core.marine.nativerequest import freeze_native_requests  # noqa: E402
from sailworks.core.marine.nativeworker import NativeRoutingWorker  # noqa: E402
from sailworks.core.marine.routingnative import to_routing_result  # noqa: E402
from sailworks.core.marine.routingresult import (  # noqa: E402
    export_blockers,
    export_eligible,
)
from sailworks.core.routingjob import RouteRequest  # noqa: E402
from sailworks.core.routingoptions import RouteScenario, scenario_request  # noqa: E402

FORECAST_FINGERPRINT = (
    "5500bb085f6a9d4702095412e61849bc8b7843d956d94dd65df5e7f285fa5caa"
)
POLAR_FINGERPRINT = "4e56ccd31cc29540ae32615c9c32a3b73016f63ac07719127f5ee578e65dccb2"
START_POSITION = (48.25, -123.65)
DESTINATION = (48.29, -123.61)
DEPARTURE = START + timedelta(hours=3, minutes=30)
REMAINING_DURATION_SECONDS = 9000
SCENARIO_NAME = "scenario-3h_t+3h"


def _bridge():
    if not BUILD_PYTHON.is_dir():
        pytest.fail(
            "native routing module build directory is missing: "
            f"{BUILD_PYTHON}. Configure and build it before running this test."
        )
    sys.path.insert(0, str(BUILD_PYTHON))
    import sailworks_native_routing

    return sailworks_native_routing


def _three_hour_job(tmp_path):
    with _forecast_inputs(tmp_path) as source:
        binding = _selected_forecast(tmp_path, source)
    base = _job(tmp_path, binding)
    request = RouteRequest(
        name="scenario-3h",
        start=base.start,
        destination=base.destination,
        departure=datetime.fromtimestamp(base.departure_epoch_s, timezone.utc),
        polar_file=str(
            Path(__file__).resolve().parents[1] / "sailworks/data/polars/test.pol"
        ),
        algorithm=LegacyAlgorithm,
        coastline_checks=False,
    )
    scenario = scenario_request(request, RouteScenario(time_offset_hours=3))
    return freeze_native_requests(
        request, (scenario,), binding, (), None, engine="main"
    )[0]


def test_three_hour_departure_is_structured_and_not_exportable(tmp_path, capsys):
    job = _three_hour_job(tmp_path)
    worker = NativeRoutingWorker()
    try:
        generation = worker.submit(job)
        status = _wait(worker, timeout=180)
    finally:
        worker.shutdown()

    # Published, not dropped; completed transport, not a worker crash.
    assert status.generation == generation
    assert status.state == "completed", status.message
    run = status.payload
    assert run is not None
    assert run["calculation_id"] == job.calculation_id

    # A structured engine outcome, with the input identity that produced it.
    assert run["engine"] == "main"
    assert run["status"] == "no_feasible_route"
    assert run["accepted"] is False
    assert run["leg_count"] == 0
    assert run["legs"] == []
    assert run["message"], "structured no-route outcome has no diagnostic message"
    request = run["request"]
    assert request["start"] == {
        "latitude": START_POSITION[0],
        "longitude": START_POSITION[1],
    }
    assert request["destination"] == {
        "latitude": DESTINATION[0],
        "longitude": DESTINATION[1],
    }
    assert request["departure"] == int(DEPARTURE.timestamp())
    assert request["maximum_route_duration_seconds"] == REMAINING_DURATION_SECONDS
    assert run["scenario"]["name"] == SCENARIO_NAME
    fingerprints = run["input_fingerprints"]
    assert fingerprints["forecast"] == FORECAST_FINGERPRINT
    assert fingerprints["polar"] == POLAR_FINGERPRINT
    assert tuple(fingerprints["start"]) == START_POSITION
    assert tuple(fingerprints["destination"]) == DESTINATION

    # Schema conversion preserves the outcome as unknown validation and keeps
    # the result diagnostic: no route, no delivery and no export path.
    result = to_routing_result(
        run,
        calculation_id=str(uuid4()),
        generation=1,
        sequence=0,
        created_epoch_s=int(time.time()),
        identity=_bridge().identity(),
    )
    assert result.status == "no_feasible_route"
    assert result.outcome.accepted is False
    assert result.outcome.delivered is False
    assert result.outcome.validation.state == "unknown"
    assert result.outcome.validation.independent is False
    assert export_eligible(result) is False
    assert export_blockers(result)

    with capsys.disabled():
        print(
            "departure-offset outcome (#162/#150): "
            f"start={START_POSITION} destination={DESTINATION} "
            f"departure={int(DEPARTURE.timestamp())} scenario={SCENARIO_NAME} "
            f"remaining_duration_s={REMAINING_DURATION_SECONDS} "
            f"forecast={FORECAST_FINGERPRINT[:12]} "
            f"polar={POLAR_FINGERPRINT[:12]} status={run['status']} "
            f"accepted={run['accepted']} delivered={result.outcome.delivered} "
            f"validation={result.outcome.validation.state} exportable=False "
            "(structured expected outcome, not a crash)"
        )
