"""The viewport's ENC scene presentation across camera changes.

A zoom must not blank the chart: the previous picture stays up while its
replacement is composed, labelled as updating - never presented as current.
A failed, cancelled or superseded update still withdraws it; an ``idle``
deferral (the weather worker is busy) keeps the presented frame.

Only the GUI-thread side is exercised here; the layer's own state machine is
covered in ``test_enc_scene_layer.py``.  The methods are bound to a stub so no
window or GPU item is needed.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

from sailworks.qt.gribviewport import (
    ViewportCamera,
    _viewport_widget_class,
    enc_scene_covers_view,
)


class _Item:
    """The GPU item's GUI-side surface: visibility and the presented scene."""

    def __init__(self, covers_view=True, reaches_view=None):
        self.visible = True
        self.scene = "previous"
        self.x = 0.0
        self.y = 0.0
        self.compose_margin = None
        # What the presented frame's mapped bbox does against the current
        # viewport; a real ``EncMeshItem`` answers from its own geometry.
        self.covers_view = covers_view
        # Whether any drawn cell still lands in the viewport.  It defaults to
        # the coverage answer, so a stub that covers also reaches.
        self.reaches_view = covers_view if reaches_view is None else reaches_view

    def setVisible(self, visible):
        self.visible = bool(visible)

    def set_view_scene(self, scene):
        self.scene = scene

    def scene_covers_view(self):
        return self.covers_view

    def scene_reaches_view(self):
        return self.reaches_view

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


def _widget(layer_state="ready", error="", covers_view=True, reaches_view=None):
    widget_class = _viewport_widget_class()
    widget = SimpleNamespace(
        _disposed=False,
        _enc_scene_item=_Item(covers_view, reaches_view),
        _enc_scene_generation=0,
        _enc_scene_ready_generation=None,
        _enc_scene_partial_generation=None,
        _enc_scene_pending_generation=None,
        _enc_scene_state="ready",
        _enc_scene_provisional=False,
        _enc_scene_layer=True,
        _enc_scene_model=SimpleNamespace(state=layer_state, error=error),
        _enc_scene_camera=None,
        # The scene publication resolves any deferred raster composition
        # against the widget's camera; no chart composition runs in this stub.
        _camera=None,
        _chart_timer=None,
        _map_pan_reference=None,
        _camera_dpr=1.0,
        _weather=_Weather(),
        _enc_scene_retry_timer=None,
    )
    for name in (
        "_withdraw_enc_scene",
        "_withdraw_enc_scene_if_current",
        "_mark_enc_scene_updating",
        "_enc_scene_published",
        "_enc_scene_frame_reaches_view",
        "_enc_scene_partial_supersedes",
        "_enc_scene_state_changed",
        "_schedule_enc_scene_retry",
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


def test_idle_deferral_keeps_the_presented_scene():
    # ``idle`` is what the layer reports when the weather worker is busy, which
    # is guaranteed during a pan.  It defers composition; it does not empty the
    # scene, so the frame stays up and is labelled updating.
    widget = _widget()
    _present(widget, 7)
    widget._mark_enc_scene_updating()

    widget._enc_scene_state_changed("idle")

    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "scene-7"
    assert widget._enc_scene_ready_generation == 7
    assert widget._weather.props["encStatus"] == "ENC scene updating…"


def test_idle_with_nothing_presented_still_withdraws():
    widget = _widget()
    widget._enc_scene_item.visible = True

    widget._enc_scene_state_changed("idle")

    assert widget._enc_scene_item.visible is False


def test_unavailable_keeps_a_retained_complete_scene():
    widget = _widget(layer_state="unavailable")
    widget._enc_scene_model.published = (
        7,
        "retained",
        {"stale": True, "complete": True},
    )
    _present(widget, 7)
    widget._mark_enc_scene_updating()

    widget._enc_scene_state_changed("unavailable")

    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "scene-7"
    assert widget._enc_scene_ready_generation == 7


def test_cancelled_or_superseded_publication_still_withdraws():
    widget = _widget()
    widget._mark_enc_scene_updating()

    # ``None`` is the layer withdrawing a superseded or cancelled scene that it
    # cannot attribute to a presented generation.
    widget._enc_scene_published(None)

    assert widget._enc_scene_item.visible is False
    assert widget._enc_scene_provisional is False


# --------------------------------------------------- generation-qualified


def _present(widget, generation, partial=False, camera=None, vertices=0, cells=None):
    """Publish the way the layer does: the message names its generation."""
    metadata = {"partial": True} if partial else {}
    if partial:
        # The layer's ``_scene_fields`` always reports the snapshot's own
        # vertex count; only a snapshot with geometry may replace a frame.
        metadata["vertices"] = vertices
    if camera is not None:
        metadata["camera"] = camera
    if cells is not None:
        metadata["cells_order"] = tuple(cells)
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
    # The partial is drawn, but it is not current content: only a complete
    # publication may claim the view.
    assert widget._enc_scene_ready_generation is None
    assert widget._enc_scene_partial_generation == 7


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


# ------------------------------------------------------- never blank


def test_a_complete_frame_that_reaches_the_view_is_never_replaced_by_a_partial():
    widget = _widget(covers_view=True)
    _present(widget, 7)

    _present(widget, 8, partial=True)

    # The finished frame is still the picture; the partial is progress only.
    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "scene-7"
    assert widget._enc_scene_ready_generation == 7
    assert widget._enc_scene_partial_generation is None
    assert widget._weather.props["encStatus"] == "ENC scene updating…"


def test_a_partial_for_the_demanded_camera_does_not_replace_a_reaching_frame():
    # A zoom-out leaves the retained frame drawing into the viewport at the
    # earlier camera's scale.  A partial composed for the demanded camera was
    # treated as the newer picture; swapping it in reset the drawn cells to
    # whatever had resolved by then, which is the pan-time collapse.
    widget = _widget(covers_view=True, reaches_view=True)
    older = ViewportCamera(
        center_lat=48.00842, center_lon=-124.74315, zoom=13.0, width=960, height=693
    )
    _present(widget, 7, camera=older)
    assert widget._enc_scene_camera == older

    current = ViewportCamera(
        center_lat=48.00842, center_lon=-124.74315, zoom=12.0, width=960, height=693
    )
    widget._camera = current
    _present(widget, 8, partial=True, camera=current, vertices=129105)

    assert widget._enc_scene_item.scene == "scene-7"
    assert widget._enc_scene_ready_generation == 7
    assert widget._enc_scene_partial_generation is None
    # Retained, but labelled: the new camera has an update in flight.
    assert widget._enc_scene_provisional is True
    assert widget._weather.props["encStatus"] == "ENC scene updating…"
    # Not both invisible: the presented frame claims the view.
    assert enc_scene_covers_view(widget._enc_scene_item) is True


def test_a_partial_takes_the_screen_only_when_the_retained_frame_cannot_reach():
    # A pan past the composed ring maps the retained frame outside the item;
    # keeping it is then a blank chart, so the partial is drawn - explicitly
    # as a partial, never as coverage.
    widget = _widget(covers_view=False, reaches_view=False)
    older = ViewportCamera(
        center_lat=48.00842, center_lon=-124.74315, zoom=13.0, width=960, height=693
    )
    _present(widget, 7, camera=older, cells=("c1", "c2", "c3", "c4", "c5", "c6"))

    current = ViewportCamera(
        center_lat=47.60, center_lon=-124.74315, zoom=13.0, width=960, height=693
    )
    widget._camera = current
    _present(widget, 8, partial=True, camera=current, vertices=129105, cells=("c9",))

    assert widget._enc_scene_item.scene == "scene-8"
    # A partial never claims the view: the complete generation is still 7.
    assert widget._enc_scene_ready_generation == 7
    assert widget._enc_scene_partial_generation == 8
    assert widget._enc_scene_provisional is True
    assert widget._weather.props["encStatus"] == "ENC scene updating…"
    # It is drawn as incomplete, so the raster still covers the viewport.
    assert enc_scene_covers_view(widget._enc_scene_item) is False


def test_a_held_partial_does_not_rewrite_the_presented_cell_set():
    # The cell identity a caller reads belongs to the frame on screen, not to
    # the partial that was held back.
    widget = _widget(covers_view=True, reaches_view=True)
    covering = ("c1", "c2", "c3", "c4", "c5", "c6")
    _present(widget, 7, cells=covering)

    current = ViewportCamera(
        center_lat=48.00842, center_lon=-124.74315, zoom=12.0, width=960, height=693
    )
    widget._camera = current
    _present(widget, 8, partial=True, camera=current, vertices=4000, cells=("c1", "c2"))

    assert widget._enc_scene_cells_order == covering
    assert widget._enc_scene_item.scene == "scene-7"


def test_a_pan_sequence_never_shrinks_the_presented_scene():
    # The owner's report: squares disappear and re-appear while panning.  Each
    # pan step demanded a new generation and its partial, with only the cells
    # resolved so far, replaced the presented frame.  The frame must stay while
    # any of its cells still lands in the viewport.
    widget = _widget(covers_view=True, reaches_view=True)
    covering = ("c1", "c2", "c3", "c4", "c5", "c6")
    start = ViewportCamera(
        center_lat=37.84, center_lon=-122.48, zoom=13.7105, width=1512, height=827
    )
    widget._camera = start
    _present(widget, 7, camera=start, vertices=278070, cells=covering)
    assert widget._enc_scene_ready_generation == 7

    for step, generation in enumerate(range(8, 12), start=1):
        widget._camera = ViewportCamera(
            center_lat=37.84 - 0.004 * step,
            center_lon=-122.48 - 0.004 * step,
            zoom=13.7105,
            width=1512,
            height=827,
        )
        # The snapshot resolved two of the six cells so far.
        _present(
            widget,
            generation,
            partial=True,
            camera=widget._camera,
            vertices=93939,
            cells=("c1", "c2"),
        )
        assert widget._enc_scene_item.scene == "scene-7"
        assert widget._enc_scene_cells_order == covering
        assert widget._enc_scene_partial_generation is None

    # The complete scene for the demanded camera is still free to land.
    _present(widget, 4, camera=widget._camera, vertices=261720, cells=covering)
    assert widget._enc_scene_item.scene == "scene-4"
    assert widget._enc_scene_ready_generation == 4


def test_a_partial_for_the_retained_camera_does_not_replace_it():
    # Same camera: the ready frame is already the current picture, so a partial
    # rebuild stays progress only and does not redraw the chart.
    widget = _widget(covers_view=True)
    camera = ViewportCamera(
        center_lat=48.00842, center_lon=-124.74315, zoom=12.0, width=960, height=693
    )
    widget._camera = camera
    _present(widget, 7, camera=camera)

    _present(widget, 8, partial=True, camera=camera, vertices=129105)

    assert widget._enc_scene_item.scene == "scene-7"
    assert widget._enc_scene_ready_generation == 7
    assert widget._enc_scene_partial_generation is None
    assert widget._weather.props["encStatus"] == "ENC scene updating…"


def test_a_partial_without_geometry_never_replaces_a_reaching_frame():
    # A partial whose resolved cells drew nothing must not blank the frame that
    # still reaches the view, even for a new camera.
    widget = _widget(covers_view=True)
    older = ViewportCamera(
        center_lat=48.00842, center_lon=-124.74315, zoom=13.0, width=960, height=693
    )
    _present(widget, 7, camera=older)

    current = ViewportCamera(
        center_lat=48.00842, center_lon=-124.74315, zoom=12.0, width=960, height=693
    )
    widget._camera = current
    _present(widget, 8, partial=True, camera=current, vertices=0)

    assert widget._enc_scene_item.scene == "scene-7"
    assert widget._enc_scene_partial_generation is None


def test_a_partial_is_drawn_when_no_frame_reaches_the_demanded_view():
    # A zoom-out left the composed frame's ring: the retained complete scene
    # maps entirely outside the viewport, so keeping it is a blank chart.  The
    # resolved cells are drawn instead, explicitly as loading content.
    widget = _widget(covers_view=False)
    _present(widget, 7)

    _present(widget, 8, partial=True)

    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "scene-8"
    # The complete generation on screen is still 7: a partial never claims the
    # view, it only fills it while the demanded scene builds.
    assert widget._enc_scene_ready_generation == 7
    assert widget._enc_scene_partial_generation == 8
    assert widget._enc_scene_provisional is True
    assert widget._weather.props["encStatus"] == "ENC scene updating…"


def test_successive_partials_of_the_presented_generation_keep_updating():
    widget = _widget(covers_view=False)
    _present(widget, 7)

    _present(widget, 8, partial=True)
    _present(widget, 8, partial=True)

    assert widget._enc_scene_item.scene == "scene-8"
    assert widget._enc_scene_partial_generation == 8


def test_the_complete_scene_clears_the_partial_and_becomes_current():
    widget = _widget(covers_view=False)
    _present(widget, 7)
    _present(widget, 8, partial=True)

    _present(widget, 8)

    assert widget._enc_scene_item.scene == "scene-8"
    assert widget._enc_scene_ready_generation == 8
    assert widget._enc_scene_partial_generation is None
    assert widget._enc_scene_provisional is False
    assert widget._weather.props["encStatus"] == ""


def test_a_loading_partial_survives_an_idle_deferral():
    widget = _widget(covers_view=False)
    _present(widget, 8, partial=True)
    assert widget._enc_scene_ready_generation is None

    widget._enc_scene_state_changed("idle")

    assert widget._enc_scene_item.visible is True
    assert widget._enc_scene_item.scene == "scene-8"
    assert widget._weather.props["encStatus"] == "ENC scene updating…"
