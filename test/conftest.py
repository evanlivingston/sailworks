"""Auto-mark Qt test modules and give the session one Qt application.

Qt/QSG tests are marked ``qt`` so the runner can split the suite with
``pytest -m "not qt"`` followed by ``pytest -m qt``: a combined-process run
still hits a native SIGTRAP in one of the Qt modules, so splitting remains the
supported way to run them all.

The session application is created here regardless, before the first test
runs.  Modules that need only a GUI application guard their own creation with
``QGuiApplication.instance() or QGuiApplication([])``.  When such a module runs
before a widget test, that guard leaves a ``QGuiApplication`` behind and the
later ``QApplication`` is fatal in Qt.  A widget-capable ``QApplication`` owned
by the session makes every module's guard a no-op, so an accidental combined
run no longer aborts at the first ``QWidget``.
"""

from pathlib import Path

import pytest

import os

# Set before any test module imports: modules call ``os.environ.setdefault``
# too, but the application must be created with a platform already chosen.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_QT_MODULE_CACHE: dict[Path, bool] = {}
_APPLICATION = None


def _imports_pyside6(path: Path) -> bool:
    cached = _QT_MODULE_CACHE.get(path)
    if cached is None:
        try:
            cached = "PySide6" in path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            cached = False
        _QT_MODULE_CACHE[path] = cached
    return cached


def _session_application():
    """One application for the session, preferring the widget-capable one."""
    global _APPLICATION
    if _APPLICATION is not None:
        return _APPLICATION
    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication
    except ImportError:  # PySide6 absent: the modules skip themselves.
        return None
    instance = QCoreApplication.instance()
    if instance is None:
        instance = QApplication([])
    _APPLICATION = instance
    return instance


def pytest_itemcollected(item):
    if _imports_pyside6(Path(str(item.path))):
        item.add_marker(pytest.mark.qt)


def pytest_sessionstart(session):
    # Runs before collection, so before any test module is imported.  Creating
    # the application here (rather than after collection) means a module whose
    # own guard would leave a ``QGuiApplication`` behind never gets the chance:
    # the widget-capable instance already exists and every guard is a no-op.
    _session_application()
