"""The viewport's ENC scene presentation across camera changes.

A zoom must not blank the chart: the previous picture stays up while its
replacement is composed, labelled as updating - never presented as current.
A failed, cancelled, superseded or idle update still withdraws it.

Only the GUI-thread side is exercised here; the layer's own state machine is
covered in ``test_enc_scene_layer.py``.  The methods are bound to a stub so no
window or GPU item is needed.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

from sailworks.qt.gribviewport import _viewport_widget_class


class _Item:
    """The GPU item's GUI-side surface: visibility and the presented scene."""

    def __init__(self):
        self.visible = True
        self.scene = "previous"
        self.x = 0.0
        self.y = 0.0
        self.compose_margin = None

    def setVisible(self, visible):
        self.visible = bool(visible)

    def set_view_scene(self, scene):
        self.scene = scene

    def set_compose_margin(self, margin):
        self.compose_margin = margin

    def setX(self, value):
        self.x = float(value)

    def setY(self, value):
        self.y = float(value)


class _Weather:
    def __init__(self):
        self.props = {}

    def setProperty(self, name, value):
        self.props[name] = value


def _widget(layer_state="ready", error=""):
    widget_class = _viewport_widget_class()
    widget = SimpleNamespace(
        _disposed=False,
        _enc_scene_item=_Item(),
        _enc_scene_generation=0,
        _enc_scene_ready_generation=None,
        _enc_scene_pending_generation=None,
        _enc_scene_state="ready",
        _enc_scene_provisional=False,
        _enc_scene_layer=True,
        _enc_scene_model=SimpleNamespace(state=layer_state, error=error),
        _enc_scene_camera=None,
        _map_pan_reference=None,
        _camera_dpr=1.0,
        _weather=_Weather(),
    )
    for name in (
        "_withdraw_enc_scene",
        "_withdraw_enc_scene_if_current",
        "_mark_enc_scene_updating",
        "_enc_scene_published",
        "_enc_scene_state_changed",
        "_update_enc_status",
        "_place_enc_scene",
    ):
        setattr(widget, name, MethodType(getattr(widget_class, name), widget))
    return widget


def test_camera_change_keeps_the_previous_scene_and_says_updating():
    widget = _widget(layer_state="ready")

    # set_camera's camera-change branch calls exactly this.
    widget._mark_enc_scene_updating()

    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "previous"
    # The retained frame is explicitly provisional, not stated as current -
    # the layer may still be reporting its old ready state.
    assert widget._weather.props["encStatus"] == "ENC scene updating…"


def test_ready_publication_replaces_the_provisional_frame():
    widget = _widget()
    widget._mark_enc_scene_updating()

    widget._enc_scene_published((7, "replacement", {}))

    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "replacement"
    assert widget._enc_scene_generation == 7
    assert widget._enc_scene_provisional is False
    assert widget._weather.props["encStatus"] == ""
    # The publication's ring is adopted before the buffers are built; this
    # payload carries no metadata, so the item keeps its default.
    assert widget._enc_scene_item.compose_margin is None
    # No camera is attached to this payload and no pan reference is retained,
    # so the camera-absolute scene cancels nothing.
    assert (widget._enc_scene_item.x, widget._enc_scene_item.y) == (0.0, 0.0)


def test_failed_update_still_withdraws_the_previous_scene():
    widget = _widget(layer_state="unavailable", error="ENC charts are disabled")
    widget._mark_enc_scene_updating()
    assert widget._enc_scene_item.visible is True

    widget._enc_scene_state_changed("unavailable")

    assert widget._enc_scene_item.visible is False
    assert widget._enc_scene_provisional is False
    assert widget._weather.props["encStatus"] == "ENC charts are disabled"


def test_a_scene_with_no_explanation_still_says_it_is_unavailable():
    # The app must never leave the ENC overlay silently blank: an unavailable
    # scene withdraws its picture and labels the chart, even when the layer
    # reports no reason of its own.
    widget = _widget(layer_state="unavailable", error="")
    widget._mark_enc_scene_updating()

    widget._enc_scene_state_changed("unavailable")

    assert widget._enc_scene_item.visible is False
    assert widget._weather.props["encStatus"] == "ENC scene unavailable"


def test_idle_update_still_withdraws_the_previous_scene():
    widget = _widget()
    widget._mark_enc_scene_updating()

    widget._enc_scene_state_changed("idle")

    assert widget._enc_scene_item.visible is False


def test_cancelled_or_superseded_publication_still_withdraws():
    widget = _widget()
    widget._mark_enc_scene_updating()

    # ``None`` is the layer withdrawing a superseded or cancelled scene that it
    # cannot attribute to a presented generation.
    widget._enc_scene_published(None)

    assert widget._enc_scene_item.visible is False
    assert widget._enc_scene_provisional is False


# --------------------------------------------------- generation-qualified


def _present(widget, generation, partial=False):
    """Publish the way the layer does: the message names its generation."""
    metadata = {"partial": True} if partial else {}
    widget._enc_scene_published((generation, f"scene-{generation}", metadata))


def test_a_superseded_partial_cannot_blank_the_presented_scene():
    # The reported bug: a partial is published, a newer camera supersedes it,
    # and the superseded worker's cancellation must not take the picture down.
    widget = _widget()
    _present(widget, 7, partial=True)
    assert widget._enc_scene_item.visible is True

    widget._enc_scene_pending_generation = 8  # a newer scene is composing
    widget._enc_scene_published((7, None, None))

    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "scene-7"
    assert widget._enc_scene_ready_generation == 7


def test_a_withdrawal_for_another_generation_does_not_hide_the_presented_scene():
    widget = _widget()
    _present(widget, 7)

    widget._enc_scene_published((6, None, None))

    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "scene-7"
    assert widget._enc_scene_ready_generation == 7


def test_a_withdrawal_for_the_presented_generation_hides_it():
    widget = _widget()
    _present(widget, 7)

    widget._enc_scene_published((7, None, None))

    assert widget._enc_scene_item.visible is False
    assert widget._enc_scene_ready_generation is None
    assert widget._enc_scene_provisional is False


def test_a_promoted_partial_is_not_hidden_by_a_later_stale_withdrawal():
    widget = _widget()
    _present(widget, 7, partial=True)
    _present(widget, 7)  # the complete scene for the same generation lands
    assert widget._enc_scene_item.scene == "scene-7"

    widget._enc_scene_published((5, None, None))

    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "scene-7"
    assert widget._enc_scene_ready_generation == 7
