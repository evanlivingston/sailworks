"""The ``--enc-scene-layer`` launcher flag and its viewport wiring.

No GUI: the launcher's own parser is the default's home, so the flag being off
by default and the scene layer superseding the raster ENC source are both
checkable without constructing a viewport.  The GUI construction itself (the
item parented into ``encSceneLayer`` and its publication) is not covered here.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from sailworks.qt.encscenelayer import resolve_enc_modes
from sailworks.qt.gribviewport import viewport_argument_parser, viewport_layer_flags


def test_enc_scene_layer_defaults_off():
    args = viewport_argument_parser().parse_args([])
    assert args.enc_scene_layer is False
    assert args.chart_layer is False
    assert args.enc_chart_layer is False
    assert viewport_layer_flags(args) == (False, False, False)


def test_scene_layer_implies_the_chart_layer():
    args = viewport_argument_parser().parse_args(["--enc-scene-layer"])
    assert viewport_layer_flags(args) == (True, False, True)


def test_scene_layer_forces_the_raster_enc_source_off():
    args = viewport_argument_parser().parse_args(
        ["--enc-chart-layer", "--enc-scene-layer"]
    )
    chart_layer, enc_chart_layer, enc_scene_layer = viewport_layer_flags(args)
    assert (chart_layer, enc_chart_layer, enc_scene_layer) == (True, False, True)
    # The same resolution authority the viewport's own constructor uses, so a
    # cell is never rasterized and drawn as a scene at once.
    assert resolve_enc_modes(True, True, True) == (True, False, True)
    assert resolve_enc_modes(False, False, True) == (True, False, True)


def test_raster_enc_flag_is_untouched_without_the_scene_layer():
    args = viewport_argument_parser().parse_args(["--enc-chart-layer"])
    assert viewport_layer_flags(args) == (True, True, False)


def test_scene_layer_construction_is_gated_off_by_default():
    pytest.importorskip("PySide6.QtQuick")
    from sailworks.qt.gribviewport import _viewport_widget_class

    viewport = _viewport_widget_class()
    parameters = inspect.signature(viewport.__init__).parameters
    # The constructor default is off, and the resolution it shares with the
    # launcher keeps the scene layer absent unless the flag asked for it.
    assert parameters["enc_scene_layer"].default is False
    assert viewport_layer_flags(
        SimpleNamespace(chart_layer=False, enc_chart_layer=False, enc_scene_layer=False)
    ) == (False, False, False)
