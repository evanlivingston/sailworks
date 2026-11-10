#!/usr/bin/env python3
"""Build the retained QSG QML module for the PySide6 Qt runtime.

The C++ item is compiled against a Qt C++ SDK (headers and CMake package),
then its Qt framework dependencies are rewritten to the frameworks shipped with
PySide6. That makes the module loadable by the application without
DYLD_FRAMEWORK_PATH, because every Qt framework it links already exists in the
running process.

Usage:
    python tools/build_routing_qsg_module.py [--qt-prefix PATH] [--output DIR]
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "routing" / "qt"
BUILD = ROOT / "build" / "routing-qt"
DEFAULT_OUTPUT = ROOT / "build" / "routing-qsg-module"
PLUGIN = "libsailworks_routing_qmlplugin.so"


def _pyside_qt_lib() -> Path:
    import PySide6

    candidate = Path(PySide6.__file__).resolve().parent / "Qt" / "lib"
    if not candidate.is_dir():
        raise RuntimeError(f"PySide6 Qt libraries not found at {candidate}")
    return candidate


def _qt_prefix(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in ("/opt/homebrew/opt/qt", "/usr/local/opt/qt"):
        if Path(candidate).is_dir():
            return candidate
    raise RuntimeError(
        "Qt C++ SDK not found; pass --qt-prefix or set SAILWORKS_QT_PREFIX"
    )


def _build(prefix: str) -> None:
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


def _relink(module: Path, pyside_lib: Path) -> list[str]:
    report = subprocess.run(
        ["otool", "-L", str(module)], capture_output=True, text=True, check=True
    )
    rewritten = []
    for line in report.stdout.splitlines()[1:]:
        dependency = line.strip().split(" (")[0]
        match = re.search(r"/([A-Za-z0-9_]+)\.framework/Versions/[^/]+/\1$", dependency)
        if match is None:
            continue
        name = match.group(1)
        replacement = pyside_lib / f"{name}.framework/Versions/A/{name}"
        if not replacement.exists():
            continue
        subprocess.run(
            ["install_name_tool", "-change", dependency, str(replacement), str(module)],
            check=True,
        )
        rewritten.append(name)
    return rewritten


def build_module(qt_prefix: str | None = None, output: Path | None = None) -> Path:
    prefix = _qt_prefix(qt_prefix)
    module_dir = (output or DEFAULT_OUTPUT) / "SailworksRouting"
    _build(prefix)
    plugin = BUILD / "qml" / "SailworksRouting" / PLUGIN
    if not plugin.is_file():
        raise RuntimeError(f"plugin not built: {plugin}")
    module_dir.mkdir(parents=True, exist_ok=True)
    target = module_dir / PLUGIN
    shutil.copy2(plugin, target)
    shutil.copy2(SOURCE / "qmldir", module_dir / "qmldir")
    _relink(target, _pyside_qt_lib())

    remaining = subprocess.run(
        ["otool", "-L", str(target)], capture_output=True, text=True, check=True
    ).stdout
    if "/opt/homebrew/" in remaining or "/usr/local/opt/" in remaining:
        raise RuntimeError("module still links a Qt SDK outside PySide6")
    return module_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qt-prefix", help="Qt C++ SDK prefix")
    parser.add_argument("--output", type=Path, help="module parent directory")
    args = parser.parse_args()
    module_dir = build_module(args.qt_prefix, args.output)
    print(module_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
