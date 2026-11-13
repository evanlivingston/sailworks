"""Cancellation must never deliver an accepted completion."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        ROOT / "build" / "native-routing" / "python",
    )
)


def _bridge():
    if not BUILD_PYTHON.is_dir():
        pytest.fail(f"native routing module build directory is missing: {BUILD_PYTHON}")
    sys.path.insert(0, str(BUILD_PYTHON))
    import sailworks_native_routing

    return sailworks_native_routing


@pytest.mark.parametrize("engine", ["main", "original", "professional"])
def test_cancelled_request_is_not_an_accepted_completion(engine):
    bridge = _bridge()
    result = bridge.synthetic_cancelled_route(engine)

    assert result["engine"] == engine
    assert result["status"] == "cancelled"
    assert result["accepted"] is False
    assert result["validation"]["passed"] is False
    assert result["independent_validation"]["passed"] is False


@pytest.mark.parametrize("engine", ["main", "original", "professional"])
def test_cancelled_request_is_deterministic(engine):
    bridge = _bridge()
    first = bridge.synthetic_cancelled_route(engine)
    second = bridge.synthetic_cancelled_route(engine)

    assert first["status"] == second["status"] == "cancelled"
    assert first["digest"] == second["digest"]
    assert first["accepted"] is second["accepted"] is False


def test_capabilities_state_the_cooperative_cancellation_mechanism():
    bridge = _bridge()
    scope = bridge.identity()["capabilities"]["cancellation_scope"]

    assert "cooperative mid-route stop" in scope
    assert "progress checkpoint" in scope
    assert "termination" in scope
    assert "never publishes an accepted or partial route" in scope
