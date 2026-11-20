"""Auto-mark Qt test modules.

Qt/QSG tests must run in their own pytest process: a combined-process run
hits a native SIGTRAP. Marking them ``qt`` lets the runner split the suite
with ``pytest -m "not qt"`` followed by ``pytest -m qt``.
"""

from pathlib import Path

import pytest

_QT_MODULE_CACHE: dict[Path, bool] = {}


def _imports_pyside6(path: Path) -> bool:
    cached = _QT_MODULE_CACHE.get(path)
    if cached is None:
        try:
            cached = "PySide6" in path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            cached = False
        _QT_MODULE_CACHE[path] = cached
    return cached


def pytest_itemcollected(item):
    if _imports_pyside6(Path(str(item.path))):
        item.add_marker(pytest.mark.qt)
