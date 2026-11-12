"""Import and identity checks for the pinned native routing bridge."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "native" / "routing" / "upstream"
BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        ROOT / "build" / "native-routing" / "python",
    )
)


def _bridge():
    if not BUILD_PYTHON.is_dir():
        pytest.fail(
            "native routing module build directory is missing: "
            f"{BUILD_PYTHON}. Configure and build with "
            "`cmake -S native/routing -B build/native-routing "
            '-Dpybind11_DIR="$(python -m pybind11 --cmakedir)"` and '
            "`cmake --build build/native-routing --parallel`."
        )
    sys.path.insert(0, str(BUILD_PYTHON))
    import sailworks_native_routing

    return sailworks_native_routing


def _tree_identity() -> tuple[str, int]:
    entries = []
    for path in UPSTREAM.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(UPSTREAM).as_posix()
        if any(part.startswith(".") for part in relative.split("/")):
            continue
        entries.append((relative, path))
    entries.sort()
    manifest = b"".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n".encode()
        for relative, path in entries
    )
    return hashlib.sha256(manifest).hexdigest(), len(entries)


def test_module_imports_and_reports_pin():
    bridge = _bridge()
    identity = bridge.identity()

    assert identity["source"]["repository"] == "rgleason/weather_routing_pi"
    assert identity["source"]["commit"] == "4df1b519e75e664dd72eed62dbb62b4a3e02b8d1"
    assert identity["versions"]["engine"] == "1.2.1"
    assert identity["versions"]["engine_cmake_project"] == "1.1.0"
    assert identity["versions"]["original_engine_cmake_project"] == "0.1.0"
    assert "unverified upstream label" in identity["versions"]["original_engine_label"]
    assert "vendored tree" in identity["versions"]["version_scheme"]
    patches = identity["local_patches"]
    assert patches["count"] > 0
    assert len(patches["entries"]) == patches["count"]
    assert patches["documentation"].endswith("LOCAL_PATCHES.md")
    assert patches["byte_identical_to_pinned_commit"] is False
    assert "not an upstream byte-identical copy" in patches["policy"]
    assert all(
        entry["documentation"] == patches["documentation"]
        for entry in patches["entries"]
    )
    assert identity["bridge_version"] == "0.1.0"
    assert identity["licenses"]["engine"] == "Apache-2.0"
    assert identity["licenses"]["original_engine"] == "GPL-3.0-or-later"
    assert identity["licenses"]["original_engine_zlib_dependency"] == "Zlib"


def test_tree_identity_is_reproducible():
    bridge = _bridge()
    expected_hash, expected_count = _tree_identity()
    identity = bridge.identity()

    assert identity["source"]["tree_sha256"] == expected_hash
    assert identity["source"]["tree_file_count"] == expected_count
    assert len(expected_hash) == 64
    assert expected_count > 0


def test_capabilities_disclose_absent_geometry_and_time():
    bridge = _bridge()
    identity = bridge.identity()
    capabilities = identity["capabilities"]

    assert capabilities["native_calculation"] is True
    assert capabilities["deterministic_synthetic"] is True
    assert capabilities["independent_replay_validation"] is True
    assert capabilities["cancellation_without_accepted_completion"] is True
    assert (
        capabilities["cancellation_scope"]
        == "pre-cancelled request only; no mid-route cancellation"
    )
    assert capabilities["progress"] is True
    assert capabilities["progress_updates"] is True
    assert capabilities["progress_source"] == "engine"
    assert "RoutingProgressUpdate" in capabilities["progress_contract"]
    assert "No percentage is synthesized" in capabilities["progress_contract"]
    assert capabilities["progress_stages"] == [
        "preflight",
        "current_coverage",
        "forward_isochrone",
        "reverse_recovery",
        "frontier_recovery",
        "graph_fallback",
        "validation",
        "complete",
    ]
    assert capabilities["professional_incumbent_wired"] is True
    assert capabilities["professional_parity_claimed"] is False
    assert capabilities["gui"] is False
    assert capabilities["opencpn"] is False
    assert capabilities["wxwidgets"] is False
    assert capabilities["bundled_shoreline_data"] is False
    assert capabilities["gshhg_data"] is False
    assert capabilities["real_forecast_acquisition"] is False
    assert capabilities["production_routing_default"] is False

    assert identity["geometry"]["provider"] == "open-water"
    assert identity["geometry"]["shoreline_data"] is False
    assert identity["geometry"]["depth_data"] is False
    assert identity["geometry"]["chart_hazards"] is False
    assert identity["time"]["real_forecast"] is False
    assert identity["time"]["absolute_timestamps"] is True
    assert identity["fixture"]["id"] == "native-routing-synthetic-v1"
    assert identity["fixture"]["polar_twa_degrees"] == [30, 60, 90, 120, 150, 180]
    assert 0 not in identity["fixture"]["polar_twa_degrees"]
    assert "clamps" in identity["fixture"]["polar_note"]
    assert identity["platforms"]["verified"] == []
    assert identity["missing"]["unknown_or_absent"]
