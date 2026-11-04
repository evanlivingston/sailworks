"""Helpers for text rendered by Qt widgets."""

from __future__ import annotations


def qt_text(value) -> str:
    """Escape a literal ampersand for mnemonic-aware Qt widgets.

    QLabel, QPushButton, QCheckBox, QAction and tab titles treat a single
    ``&`` as an accelerator marker. Doubling it displays the literal ``&``.
    """
    return str(value).replace("&", "&&")
