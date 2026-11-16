"""Contract tests for apply_theme's per-application idempotency guard.

Qt allows one QApplication per process, so "a different app" is represented by
clearing the dynamic property that lives on the application object; that is the
same state a freshly constructed QApplication starts in.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from sailworks.qt import theme as theme_module


class _CountingThemeStyle(theme_module.ThemeStyle):
    """ThemeStyle that records how many times apply_theme builds one."""

    constructed = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).constructed += 1


def _application():
    return QApplication.instance() or QApplication([])


def _unthemed(app):
    if app.property(theme_module._THEMED):
        app.setProperty(theme_module._THEMED, False)
    return app


def _snapshot(app):
    palette = app.palette()
    return {
        "window": palette.color(QPalette.ColorRole.Window).name(),
        "base": palette.color(QPalette.ColorRole.Base).name(),
        "text": palette.color(QPalette.ColorRole.Text).name(),
        "text_disabled": palette.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
        ).name(),
        "base_disabled": palette.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base
        ).name(),
        "stylesheet": app.styleSheet(),
    }


def test_apply_theme_styles_an_application_only_once(monkeypatch):
    app = _unthemed(_application())
    monkeypatch.setattr(theme_module, "ThemeStyle", _CountingThemeStyle)
    _CountingThemeStyle.constructed = 0
    window = QWidget()
    label = QLabel("0", window)
    window.show()
    app.processEvents()

    theme_module.apply_theme(app)

    assert app.property(theme_module._THEMED) is True
    # A Qt dynamic property, not a Python attribute: it dies with the app.
    assert theme_module._THEMED not in vars(app)
    assert b"_sailworks_theme_applied" in {
        bytes(name) for name in app.dynamicPropertyNames()
    }
    assert _CountingThemeStyle.constructed == 1
    first_style = app.style()
    first = _snapshot(app)
    assert first["window"] == "#242424"
    assert first["stylesheet"]

    theme_module.apply_theme(app)

    assert _CountingThemeStyle.constructed == 1, "second call rebuilt the style"
    assert app.style() is first_style, "second call swapped the style again"
    assert _snapshot(app) == first
    assert window.isVisible()
    assert label.parent() is window


def test_a_fresh_application_is_themed_again(monkeypatch):
    # One QApplication per process: clearing the per-object property leaves the
    # app in the state a newly constructed one is in. The guard must be
    # per-app, so this must theme again.
    app = _unthemed(_application())
    monkeypatch.setattr(theme_module, "ThemeStyle", _CountingThemeStyle)
    _CountingThemeStyle.constructed = 0

    theme_module.apply_theme(app)
    assert _CountingThemeStyle.constructed == 1
    assert app.property(theme_module._THEMED) is True
    first_style = app.style()

    app.setProperty(theme_module._THEMED, False)
    theme_module.apply_theme(app)

    assert _CountingThemeStyle.constructed == 2
    assert app.property(theme_module._THEMED) is True
    assert app.style() is not first_style
