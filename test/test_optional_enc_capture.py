"""The ENC capture is opt-in and must never block an application launch."""

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from sailworks.qt import __main__ as launcher


def test_launcher_imports_without_the_capture_module(monkeypatch):
    # Blocking the module with None makes any import of it raise ImportError.
    monkeypatch.setitem(sys.modules, "sailworks.qt.enc_capture", None)
    monkeypatch.delitem(sys.modules, "sailworks.qt.__main__", raising=False)

    module = importlib.import_module("sailworks.qt.__main__")

    assert callable(module.main)
    assert callable(module.install_optional_capture)
    # The capture callable is resolved lazily, not bound at module import.
    assert "install_capture" not in vars(module)


def test_unset_capture_directory_does_no_work(monkeypatch):
    monkeypatch.delenv("SAILWORKS_ENC_CAPTURE", raising=False)
    monkeypatch.setitem(sys.modules, "sailworks.qt.enc_capture", None)

    assert launcher.install_optional_capture(Mock(), Mock(), Mock()) is None


def test_missing_capture_module_warns_instead_of_raising(monkeypatch, capsys):
    monkeypatch.setenv("SAILWORKS_ENC_CAPTURE", "/tmp/does-not-matter")
    monkeypatch.setitem(sys.modules, "sailworks.qt.enc_capture", None)

    assert launcher.install_optional_capture(Mock(), Mock(), Mock()) is None

    warning = capsys.readouterr().err
    assert "SAILWORKS_ENC_CAPTURE" in warning
    assert warning.count("warning:") == 1
    assert "continuing without capture" in warning


def test_configured_capture_directory_installs_capture(monkeypatch):
    installed = []

    def install_capture(app, window, controller):
        installed.append((app, window, controller))
        return "capture"

    monkeypatch.setenv("SAILWORKS_ENC_CAPTURE", "/tmp/does-not-matter")
    monkeypatch.setitem(
        sys.modules,
        "sailworks.qt.enc_capture",
        SimpleNamespace(install_capture=install_capture),
    )
    app, window, controller = Mock(), Mock(), Mock()

    assert launcher.install_optional_capture(app, window, controller) == "capture"
    assert installed == [(app, window, controller)]
