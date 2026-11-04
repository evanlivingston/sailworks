"""The Advanced routing disclosure must explain each setting in plain words."""

import weatherrouting

from sailworks.qt.routingpanel import (
    PARAMETER_HELP,
    SAFETY_HELP,
    InfoButton,
    RoutingPanel,
)
from test.qt_analysis_fakes import app


def test_parameter_help_covers_every_builtin_algorithm_parameter():
    keys = set()
    for entry in weatherrouting.list_routing_algorithms():
        for key in entry["class"].PARAMS:
            keys.add(key)
            assert key in PARAMETER_HELP, key
            # A restated label is not help; require a full sentence.
            assert len(PARAMETER_HELP[key]) > 80, key
    assert keys


def test_safety_help_covers_every_routing_limit():
    assert set(SAFETY_HELP) == {
        "limit_wind",
        "motor_fallback",
        "motor_speed",
        "wave_proxy",
    }
    for text in SAFETY_HELP.values():
        assert len(text) > 80


def test_info_button_opens_a_wrapped_popover_instead_of_a_tooltip():
    app()
    button = RoutingPanel._info_button(SAFETY_HELP["wave_proxy"])
    assert isinstance(button, InfoButton)
    assert button.info_text() == SAFETY_HELP["wave_proxy"]
    # A native tooltip renders the text as an unreadable single line, so the
    # explanation lives in a wrapped popover instead.
    assert button.toolTip() == ""

    button._open_popup()
    assert button._popup is not None
    assert button._popup.wordWrap()
    assert 220 <= button._popup.maximumWidth() <= 360
    assert button._popup.text() == SAFETY_HELP["wave_proxy"]

    button._hide_popup()
    assert button._popup is None
