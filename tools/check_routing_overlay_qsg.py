#!/usr/bin/env python3
"""Real-window gate for the retained QSG routing item.

Builds the C++ Qt Quick check (if needed), runs it in a real Qt Quick window
with the software backend, and re-emits its fidelity and retention metrics.

The item is built against the Homebrew Qt that the application must load via
DYLD_FRAMEWORK_PATH; a plugin from a different Qt build cannot load into
PySide6's bundled Qt (verified symbol mismatch).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "routing" / "qt"
BUILD = ROOT / "build" / "routing-qt"
BINARY = BUILD / "sailworks_routing_qsg_check"


def _qt_prefix() -> str | None:
    configured = os.environ.get("SAILWORKS_QT_PREFIX")
    if configured:
        return configured
    for candidate in ("/opt/homebrew/opt/qt", "/usr/local/opt/qt"):
        if Path(candidate).is_dir():
            return candidate
    return None


def _build_if_needed() -> None:
    if BINARY.is_file():
        return
    prefix = _qt_prefix()
    if prefix is None:
        raise RuntimeError(
            "Qt C++ SDK not found; set SAILWORKS_QT_PREFIX to a Qt6 installation"
        )
    subprocess.run(
        [
            "cmake",
            "-S",
            str(SOURCE),
            "-B",
            str(BUILD),
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_PREFIX_PATH={prefix}",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["cmake", "--build", str(BUILD), "--parallel", "4"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def measure() -> dict:
    _build_if_needed()
    environment = dict(os.environ)
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    environment.setdefault("QT_QUICK_BACKEND", "software")
    result = subprocess.run(
        [str(BINARY)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "retained QSG check failed: "
            + (result.stderr.strip() or f"exit {result.returncode}")
        )
    report = json.loads(result.stdout)
    retained = report["retained"]
    report["retained"]["geometry_rebuilds_during_pan"] = (
        retained["geometry_rebuilds_after_pan"]
        - retained["geometry_rebuilds_after_first_frame"]
    )
    report["retained"]["geometry_rebuilds_during_style"] = (
        retained["geometry_rebuilds_after_style"]
        - retained["geometry_rebuilds_after_first_frame"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the measurement JSON")
    args = parser.parse_args()

    measured = measure()
    text = json.dumps(measured, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
