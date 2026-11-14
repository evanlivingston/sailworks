"""Application interaction handlers shared by Qt map widgets."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QCursor
import math

from sailworks.core.geo.mapshapes import (
    DEFAULT_COLOUR,
    DEFAULT_COLOURS,
    KIND_AVOID,
    KIND_NOTE,
    MIN_POLYGON_VERTICES,
    ShapeValidationError,
    colour_rgb,
    validate_vertices,
)

from .navigationcommands import SetShapeVerticesCommand
from .theme import MenuBuilder, ThemedMenu


class MapInteractionMixin:
    _CURSOR_POINTING = Qt.CursorShape.PointingHandCursor
    _CURSOR_DRAGGING = Qt.CursorShape.ClosedHandCursor
    _CURSOR_BASE = Qt.CursorShape.OpenHandCursor
    _CURSOR_PRECISE = Qt.CursorShape.CrossCursor

    def map_pointer_target(self):
        """The surface that owns the pointer cursor.

        The desktop viewport is a ``QQuickView`` (a ``QWindow``) laid out
        inside this widget, so the mouse events and the pointer cursor belong
        to that window; setting the cursor on the container changes nothing.
        Fall back to this widget when no QuickView is hosted.
        """
        presenter = getattr(self, "_weather_presenter", None)
        widget = getattr(presenter, "widget", None) if presenter is not None else None
        return widget if widget is not None else self

    def map_pointer_shape(self):
        """The cursor shape implied by the current map mode.

        A direct point move drags a persistent track mark under the pointer,
        so it shows the drag cursor.  Measure mode and routing/POI moves keep
        the precise crosshair they always had, and are never downgraded by a
        track-mark hover.
        """
        if getattr(self, "move_callback", None) is not None:
            if getattr(self, "_move_direct_point", False):
                return self._CURSOR_DRAGGING
            return self._CURSOR_PRECISE
        if getattr(self, "_measure_mode", False):
            return self._CURSOR_PRECISE
        hover = getattr(self, "_map_hover_mark", None)
        if hover is not None and getattr(self.navigation, "edit_track", None) is None:
            # Edit mode ended while a mark was still hovered; the highlight is
            # stale, so the pointer must not keep advertising it as actionable.
            hover = None
        if hover is not None:
            return self._CURSOR_POINTING
        return self._CURSOR_BASE

    def apply_map_cursor(self, shape=None):
        """Apply the map cursor to the event surface, once per change.

        Repeated pointer moves must not reissue ``setCursor`` on every event,
        so the last (target, shape) pair is cached.  Re-applying is automatic
        when the presenter swaps in a different QuickView window.
        """
        target = self.map_pointer_target()
        shape = self.map_pointer_shape() if shape is None else shape
        if getattr(self, "_map_cursor_state", None) == (target, shape):
            return False
        self._map_cursor_state = (target, shape)
        set_cursor = getattr(target, "setCursor", None)
        if callable(set_cursor):
            set_cursor(QCursor(shape))
        return True

    def _update_measurement_overlay(self):
        presenter = getattr(self, "_weather_presenter", None)
        for name in ("foreground", "overlay"):
            overlay = getattr(presenter, name, None)
            if overlay is not None:
                overlay.update()
        publish = getattr(self, "publish_route_overlay", None)
        if publish is not None:
            publish()

    def _clear_stale_track_mark_hover(self):
        """Retire a mark highlight that the current pointer mode no longer implies.

        The highlight lives in the navigation model, which the QML layer mirrors,
        so merely ignoring it here would leave the mark lit up.  Clearing it
        through ``set_track_mark_hover`` also republishes the overlay.
        """
        current = getattr(self, "_map_hover_mark", None)
        if current is None:
            navigation = getattr(self, "navigation", None)
            current = getattr(navigation, "hl_track_hover", None)
        if current is None:
            return False
        self.set_track_mark_hover(None)
        return True

    def _clear_measurement(self):
        self._measure_points = ()
        self._measure_preview_point = None
        self._measure_mode = False
        # A hover acquired before or during measurement advertised a clickable
        # mark, but returning to the pan cursor makes it stale.
        self._clear_stale_track_mark_hover()
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.apply_map_cursor()
        self.update()
        self._update_measurement_overlay()

    def contextMenuEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._cancel_cursor()
        self.cancel_move()
        position = self.convert_screen_to_geographic(event.pos().x(), event.pos().y())
        menu = ThemedMenu(self)
        builder = MenuBuilder(menu)
        poi = builder.add("Add POI here", key="P")
        point = builder.add("Add track point here", key="T")
        builder.section()
        start = builder.add("Set routing start here", key="S")
        destination = builder.add("Set routing destination here", key="D")
        # A legacy host may compose the interaction mixin without the shape
        # drawing half, so the entries only exist when the action can run.
        can_draw_shapes = self._shape_menu_available()
        note_shape = avoid_shape = None
        if can_draw_shapes:
            builder.section()
            note_shape = builder.add("Draw note polygon", key="N")
            avoid_shape = builder.add("Draw avoidance polygon", key="A")
        selected = menu.exec(event.globalPos())
        if selected is poi or selected is point:
            self.navigation_request.emit(
                "poi" if selected is poi else "point", position.get_degrees()
            )
        if selected is start or selected is destination:
            self.route_location.emit(
                "start" if selected is start else "destination", position.get_degrees()
            )
        if note_shape is not None and (
            selected is note_shape or selected is avoid_shape
        ):
            # The starting point is where the pointer already is, so the first
            # vertex lands under the menu that was just dismissed.
            self.begin_shape_drawing(
                KIND_NOTE if selected is note_shape else KIND_AVOID
            )
            self._record_shape_click(event.pos().x(), event.pos().y())

    def begin_move(self, callback, preview=None, restore=None, direct_point=False):
        self._cancel_cursor()
        self._cancel_inertia()
        self._finish_zoom_silent()
        self.move_callback = callback
        self._move_preview_callback = preview
        self._move_restore_callback = restore
        self._move_direct_point = bool(direct_point)
        self._move_press_position = None
        self.drag = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.apply_map_cursor()
        self.setFocus()
        self.update()

    def cancel_move(self):
        self._end_move(restore=True)

    def _end_move(self, restore):
        if (
            self.move_callback is None
            and getattr(self, "_move_preview_callback", None) is None
        ):
            return
        if restore:
            restore_callback = getattr(self, "_move_restore_callback", None)
            if restore_callback is not None:
                restore_callback()
        self.move_callback = None
        self._move_preview_callback = None
        self._move_restore_callback = None
        self._move_direct_point = False
        self._move_press_position = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.apply_map_cursor()
        clear = getattr(self, "clear_move_preview", None)
        if callable(clear):
            clear()
        publish = getattr(self, "publish_route_overlay", None)
        if callable(publish):
            publish()
        self.update()

    def _begin_move_drag(self, position):
        """Start a move gesture; the point relocates on release."""
        if self.move_callback is None:
            return False
        self._move_press_position = position
        return True

    def _move_preview(self, position):
        """Place the moving point under the cursor without committing."""
        preview = getattr(self, "_move_preview_callback", None)
        if preview is None:
            return False
        if getattr(self, "_move_direct_point", False):
            # A direct point drag relocates a persistent mark that the retained
            # scene previews itself, so it must project with the scene camera
            # and must not repaint the whole canvas on every pointer move.
            preview(self.scene_geographic_at(position))
            return True
        preview(
            self.convert_screen_to_geographic(position.x(), position.y()).get_degrees()
        )
        self.update()
        return True

    def _finish_move_drag(self, position):
        """Commit a move at the release position (or a plain click)."""
        if self.move_callback is None or self._move_press_position is None:
            return False
        callback = self.move_callback
        if getattr(self, "_move_direct_point", False):
            coordinates = self.scene_geographic_at(position)
        else:
            coordinates = self.convert_screen_to_geographic(
                position.x(), position.y()
            ).get_degrees()
        self._end_move(restore=False)
        callback(coordinates)
        return True

    _TRACK_MARK_HIT_RADIUS = 10.0

    def scene_projection(self):
        """The camera and pixel translation the scene draws content with.

        A retained scene pans by translating its content item and republishes
        the overlay camera only when that overlay rebases, so the widget's
        camera and the drawn marks can disagree by that translation.  Hit
        testing has to project with the camera the scene actually draws; the
        default is the widget camera with no translation.
        """
        return (
            float(self._center_lat),
            float(self._center_lon),
            float(self._zoom),
            0.0,
            0.0,
        )

    _MAX_MERCATOR_LATITUDE = 85.05112878

    def scene_geographic_at(self, position):
        """The geographic point under a position in the retained scene.

        Exact inverse of the projection that ``edit_track_mark_at`` hit tests
        with and that the QML track layer draws with: the same Web Mercator
        camera, pan translation and longitude wrapping.  Converting through the
        widget camera instead would drift by the scene pan, so a dragged mark
        would snap on release.
        """
        latitude, longitude, zoom, offset_x, offset_y = self.scene_projection()
        rect = self.viewport_rect()
        world = 256.0 * (2.0 ** float(zoom))
        dx = float(position.x()) - rect.width() / 2.0 - float(offset_x)
        dx -= round(dx / world) * world
        point_longitude = float(longitude) + dx * 360.0 / world
        point_longitude = (point_longitude + 180.0) % 360.0 - 180.0
        centre_latitude = max(
            -self._MAX_MERCATOR_LATITUDE,
            min(self._MAX_MERCATOR_LATITUDE, float(latitude)),
        )
        centre_y = self._mercator_y(centre_latitude, world)
        point_y = float(position.y()) - rect.height() / 2.0 - float(offset_y) + centre_y
        point_latitude = math.degrees(
            math.atan(math.sinh(math.pi * (1.0 - 2.0 * point_y / world)))
        )
        point_latitude = max(
            -self._MAX_MERCATOR_LATITUDE,
            min(self._MAX_MERCATOR_LATITUDE, point_latitude),
        )
        return (point_latitude, point_longitude)

    def edit_track_mark_at(self, position):
        """Index of the editing track's persistent mark under a position."""
        track = getattr(self.navigation, "edit_track", None)
        if track is None or not getattr(track, "visible", False):
            return None
        latitude, longitude, zoom, offset_x, offset_y = self.scene_projection()
        rect = self.viewport_rect()
        world = 256.0 * (2.0 ** float(zoom))
        centre_latitude = max(-85.05112878, min(85.05112878, float(latitude)))
        centre_y = self._mercator_y(centre_latitude, world)
        best = None
        for index, point in enumerate(track):
            point_latitude = max(-85.05112878, min(85.05112878, float(point[0])))
            point_longitude = float(point[1])
            dx = (point_longitude - float(longitude)) * world / 360.0
            dx -= round(dx / world) * world
            x = rect.width() / 2.0 + dx + float(offset_x)
            y = (
                rect.height() / 2.0
                + self._mercator_y(point_latitude, world)
                - centre_y
                + float(offset_y)
            )
            distance = math.hypot(position.x() - x, position.y() - y)
            if distance <= self._TRACK_MARK_HIT_RADIUS and (
                best is None or distance < best[1]
            ):
                best = (index, distance)
        return None if best is None else best[0]

    @staticmethod
    def _mercator_y(latitude, world):
        radians = math.radians(latitude)
        return world * (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0

    def set_track_mark_hover(self, index):
        """Highlight one persistent mark of the editing track, or clear it."""
        current = getattr(self.navigation, "hl_track_hover", None)
        index = None if index is None else int(index)
        self._map_hover_mark = index
        changed = current != index
        if changed:
            self.navigation.hl_track_hover = index
            publish = getattr(self, "publish_route_overlay", None)
            if callable(publish):
                publish()
        self.apply_map_cursor()
        return changed

    def keyPressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if getattr(self, "_shape_draft_kind", None) is not None:
            if event.key() == Qt.Key.Key_Escape:
                self.cancel_shape_drawing()
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.finish_shape_drawing()
                event.accept()
                return
        if getattr(self, "_shape_edit_uid", None) is not None:
            # Drawing owns the keys first; here edge edit mode answers them.
            if event.key() == Qt.Key.Key_Escape:
                cancel = getattr(self, "cancel_shape_edit", None)
                if cancel is not None:
                    cancel()
                    event.accept()
                    return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                finish = getattr(self, "finish_shape_edit", None)
                if finish is not None:
                    finish()
                    event.accept()
                    return
        if event.key() == Qt.Key.Key_I:
            self._cancel_cursor()
            self.cancel_move()
            self._measure_mode = True
            self._measure_points = ()
            self._measure_preview_point = None
            # Measure mode shows a crosshair, so any actionable mark highlight
            # from the previous mode is stale while measuring.
            self._clear_stale_track_mark_hover()
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.apply_map_cursor()
            self.setFocus()
            self.update()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and (
            getattr(self, "_measure_mode", False)
            or getattr(self, "_measure_points", ())
        ):
            self._clear_measurement()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.move_callback is not None:
            self.cancel_move()
            event.accept()
            return
        if (
            event.key() == Qt.Key.Key_Escape
            and getattr(getattr(self, "navigation", None), "edit_track", None)
            is not None
        ):
            # The navigation panel owns edit-mode state, so leave through it.
            cancel = getattr(self, "track_edit_cancel_requested", None)
            if cancel is not None:
                # Leaving edit mode retires the actionable marks, so the
                # pointer cue must fall back even if it never moves again.
                self.set_track_mark_hover(None)
                cancel.emit()
                event.accept()
                return
        super().keyPressEvent(event)

    def _record_measurement_click(self, x, y):
        if not getattr(self, "_measure_mode", False):
            return False
        point = self.convert_screen_to_geographic(x, y)
        self._measure_points = (
            *getattr(self, "_measure_points", ()),
            point.get_degrees(),
        )
        self._measure_preview_point = None
        if len(self._measure_points) == 2:
            self._measure_mode = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.apply_map_cursor()
        self.update()
        self._update_measurement_overlay()
        return True

    def _update_measurement_preview(self, x, y):
        if (
            not getattr(self, "_measure_mode", False)
            or len(getattr(self, "_measure_points", ())) != 1
        ):
            return False
        self._measure_preview_point = self.convert_screen_to_geographic(
            x, y
        ).get_degrees()
        self._update_measurement_overlay()
        return True

    def _measurement_distance_km(self):
        points = getattr(self, "_measure_points", ())
        if len(points) != 2:
            return None
        return self._distance_between_km(points[0], points[1])

    @staticmethod
    def _distance_between_km(first, second):
        (lat1, lon1), (lat2, lon2) = first, second
        lat1, lat2 = math.radians(lat1), math.radians(lat2)
        dlat, dlon = lat2 - lat1, math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        return 6371.0088 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class MapShapeDrawingMixin:
    # The window attaches its history before any shape edit starts; tests and
    # legacy hosts that never attach one keep the direct-save fallback.
    undo_stack = None
    """Click-to-draw editing and display for scene shapes.

    The pointer host (``gribviewport``) owns the mouse events, so it only
    forwards raw positions here; every vertex lands through the same Web
    Mercator inverse projection the retained scene uses. A draft stays local
    until ``ShapeCollection.create`` accepts it, so an unfinished or invalid
    polygon never reaches persistence or the tolerance geometry.
    """

    _SHAPE_CLOSE_RADIUS = 12.0
    _SHAPE_EDIT_RADIUS = 10.0
    _SHAPE_OUTLINE_COLOURS = dict(DEFAULT_COLOURS)

    # -- draft state ---------------------------------------------------

    def _shape_menu_available(self):
        """True when this host can start a polygon draft from the menu.

        The mixin is also composed by hosts that predate shape drawing, so the
        context menu asks before offering entries it cannot honour.
        """
        return callable(getattr(self, "begin_shape_drawing", None))

    def is_shape_drawing(self):
        return getattr(self, "_shape_draft_kind", None) is not None

    def shape_drawing_kind(self):
        return getattr(self, "_shape_draft_kind", None)

    def shape_error(self):
        return getattr(self, "_shape_error", "")

    def begin_shape_drawing(self, kind):
        """Start a polygon draft of ``kind``; the next click adds a vertex."""
        kind = KIND_AVOID if kind == KIND_AVOID else KIND_NOTE
        self._cancel_cursor()
        self.cancel_move()
        if getattr(self, "_measure_points", ()) or getattr(
            self, "_measure_mode", False
        ):
            self._clear_measurement()
        self._shape_draft_kind = kind
        self._shape_draft_points = []
        self._shape_draft_preview = None
        self._shape_error = ""
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.apply_map_cursor()
        self.setFocus()
        self.update()
        self._publish_shape_overlay()
        return True

    def cancel_shape_drawing(self):
        """Discard the draft; nothing was written to the collection."""
        if not self.is_shape_drawing():
            return False
        self._shape_draft_kind = None
        self._shape_draft_points = []
        self._shape_draft_preview = None
        self._shape_error = ""
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.apply_map_cursor()
        self.update()
        self._publish_shape_overlay()
        return True

    def _shape_manager(self):
        """The session's shape collection.

        The live viewport reaches the collection through the session
        controller's ``core``. Older duck-typed doubles expose it directly on
        the controller, which stays supported.
        """
        controller = getattr(self, "controller", None)
        core = getattr(controller, "core", None)
        manager = getattr(core, "shapeManager", None)
        if manager is not None:
            return manager
        return getattr(controller, "shapeManager", None)

    # -- edge edit mode -------------------------------------------------

    def is_shape_editing(self):
        return getattr(self, "_shape_edit_uid", None) is not None

    def shape_edit_uid(self):
        return getattr(self, "_shape_edit_uid", None)

    def _shape_element(self, uid):
        """The stored element for ``uid``, or ``None`` when it is gone."""
        if uid is None:
            return None
        manager = self._shape_manager()
        if manager is None:
            return None
        for element in manager:
            if getattr(element, "uid", None) == uid:
                return element
        return None

    def begin_shape_edit(self, uid):
        """Enter edge edit mode for the polygon ``uid``.

        Any other pointer mode is retired first: a draft would otherwise keep
        receiving clicks, and a move or measurement would keep the crosshair.
        """
        if uid is None:
            return False
        if self.is_shape_drawing():
            self.cancel_shape_drawing()
        cancel = getattr(self, "cancel_move", None)
        if callable(cancel):
            cancel()
        if getattr(self, "_measure_points", ()) or getattr(
            self, "_measure_mode", False
        ):
            self._clear_measurement()
        element = self._shape_element(uid)
        if element is None or not getattr(element, "is_polygon", True):
            return False
        try:
            vertices = list(element.vertices)
        except (TypeError, ValueError):
            return False
        if len(vertices) < MIN_POLYGON_VERTICES:
            return False

        self._shape_edit_uid = getattr(element, "uid", uid)
        self._shape_edit_edge = None
        self._shape_edit_point = None
        self._shape_edit_vertex = None
        self._shape_edit_drag_vertex = None
        self._shape_edit_drag_point = None
        self._shape_edit_drag_origin = None
        self._shape_edit_drag_vertices = None
        self._shape_error = ""
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.apply_map_cursor()
        self.setFocus()
        self.update()
        self._publish_shape_overlay()
        return True

    def cancel_shape_edit(self):
        """Leave edge edit mode; an uncommitted drag preview is dropped."""
        if not self.is_shape_editing():
            return False
        self._shape_edit_uid = None
        self._shape_edit_edge = None
        self._shape_edit_point = None
        self._shape_edit_vertex = None
        self._shape_edit_reset_drag()
        self._shape_error = ""
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.apply_map_cursor()
        self.update()
        self._publish_shape_overlay()
        return True

    def _shape_edit_reset_drag(self):
        """Drop the transient vertex drag; stored geometry is untouched."""
        self._shape_edit_drag_vertex = None
        self._shape_edit_drag_point = None
        self._shape_edit_drag_origin = None
        self._shape_edit_drag_vertices = None

    @staticmethod
    def _shape_nearest_on_segment(x, y, start, end):
        """Closest point on a screen segment and its distance to ``x, y``."""
        delta_x = end[0] - start[0]
        delta_y = end[1] - start[1]
        length_squared = delta_x * delta_x + delta_y * delta_y
        if length_squared <= 0.0:
            return start, math.hypot(x - start[0], y - start[1])
        t = ((x - start[0]) * delta_x + (y - start[1]) * delta_y) / length_squared
        t = max(0.0, min(1.0, t))
        point = (start[0] + t * delta_x, start[1] + t * delta_y)
        return point, math.hypot(x - point[0], y - point[1])

    def _shape_edit_edge_at(self, x, y):
        """``(edge index, geo point)`` for the nearest edge within reach.

        Both endpoints of every segment, including the closing one, are
        projected through the drawn camera; the hit point is then inverse
        projected so the preview and the inserted vertex sit on the edge the
        user sees rather than on a raw pointer position.
        """
        if not self.is_shape_editing():
            return None
        element = self._shape_element(getattr(self, "_shape_edit_uid", None))
        if element is None:
            return None
        try:
            vertices = [tuple(vertex) for vertex in element.vertices]
        except (TypeError, ValueError):
            return None
        if len(vertices) < MIN_POLYGON_VERTICES:
            return None
        screens = [self._shape_screen_point(vertex) for vertex in vertices]
        best = None
        for index in range(len(screens)):
            start = screens[index]
            end = screens[(index + 1) % len(screens)]
            if start is None or end is None:
                continue
            point, distance = self._shape_nearest_on_segment(
                float(x), float(y), start, end
            )
            if distance > self._SHAPE_EDIT_RADIUS:
                continue
            if best is None or distance < best[0]:
                best = (distance, index, point)
        if best is None:
            return None
        geo = self._shape_geographic_at(best[2][0], best[2][1])
        if geo is None:
            return None
        return best[1], tuple(geo)

    def _shape_edit_vertex_at(self, x, y):
        """Index of the edited polygon's corner under the pointer, or ``None``.

        Corners take precedence over edge insertion: a corner under the
        pointer is what the user can grab, so the edge preview is suppressed
        there even though both are within the same reach.
        """
        if not self.is_shape_editing():
            return None
        element = self._shape_element(getattr(self, "_shape_edit_uid", None))
        if element is None:
            return None
        try:
            vertices = [tuple(vertex) for vertex in element.vertices]
        except (TypeError, ValueError):
            return None
        best = None
        for index, vertex in enumerate(vertices):
            screen = self._shape_screen_point(vertex)
            if screen is None:
                continue
            distance = math.hypot(float(x) - screen[0], float(y) - screen[1])
            if distance <= self._SHAPE_EDIT_RADIUS and (
                best is None or distance < best[1]
            ):
                best = (index, distance)
        return None if best is None else best[0]

    def _update_shape_edit_hover(self, x, y):
        """Snap the preview to the nearest corner or edge, without committing."""
        if not self.is_shape_editing() or self.is_shape_vertex_dragging():
            return False
        vertex = self._shape_edit_vertex_at(x, y)
        if vertex is not None:
            if (
                vertex == getattr(self, "_shape_edit_vertex", None)
                and getattr(self, "_shape_edit_edge", None) is None
                and getattr(self, "_shape_edit_point", None) is None
            ):
                return True
            self._shape_edit_vertex = vertex
            self._shape_edit_edge = None
            self._shape_edit_point = None
            self._shape_error = ""
            self._publish_shape_overlay()
            return True

        previous_vertex = getattr(self, "_shape_edit_vertex", None)
        hit = self._shape_edit_edge_at(x, y)
        if hit is None:
            if (
                previous_vertex is None
                and getattr(self, "_shape_edit_edge", None) is None
                and getattr(self, "_shape_edit_point", None) is None
            ):
                return False
            self._shape_edit_vertex = None
            self._shape_edit_edge = None
            self._shape_edit_point = None
            self._shape_error = ""
            self._publish_shape_overlay()
            return True
        edge, point = hit
        if (
            previous_vertex is None
            and edge == getattr(self, "_shape_edit_edge", None)
            and point == getattr(self, "_shape_edit_point", None)
        ):
            return True
        self._shape_edit_vertex = None
        self._shape_edit_edge = edge
        self._shape_edit_point = point
        self._shape_error = ""
        self._publish_shape_overlay()
        return True

    # -- corner drag ----------------------------------------------------

    def is_shape_vertex_dragging(self):
        return getattr(self, "_shape_edit_drag_vertex", None) is not None

    def _clamp_shape_point(self, point):
        """Clamp a projected point to the drawable Mercator range."""
        limit = getattr(self, "_MAX_MERCATOR_LATITUDE", 85.05112878)
        latitude = max(-limit, min(limit, float(point[0])))
        longitude = (float(point[1]) + 180.0) % 360.0 - 180.0
        return (latitude, longitude)

    def _begin_shape_vertex_drag(self, index):
        """Remember a corner's original position; geometry commits on release."""
        element = self._shape_element(getattr(self, "_shape_edit_uid", None))
        if element is None:
            return False
        try:
            vertices = tuple(tuple(vertex) for vertex in element.vertices)
        except (TypeError, ValueError):
            return False
        if index < 0 or index >= len(vertices):
            return False
        self._shape_edit_drag_vertex = index
        self._shape_edit_drag_origin = vertices[index]
        self._shape_edit_drag_vertices = vertices
        self._shape_edit_drag_point = vertices[index]
        self._shape_edit_vertex = index
        self._shape_edit_edge = None
        self._shape_edit_point = None
        self._shape_error = ""
        self._publish_shape_overlay()
        return True

    def _shape_edit_press(self, x, y):
        """Press in edit mode: grab a corner, else insert on the preview edge.

        Returns ``True`` only when the press belongs to shape editing, so the
        pointer host can fall through to measurement or pan otherwise.
        """
        if not self.is_shape_editing():
            return False
        if self.is_shape_vertex_dragging():
            return True
        vertex = self._shape_edit_vertex_at(x, y)
        if vertex is not None:
            return self._begin_shape_vertex_drag(vertex)
        return self._record_shape_edit_click(x, y)

    def _shape_edit_move(self, x, y):
        """Move the dragged corner live, or update the hover preview."""
        if not self.is_shape_editing():
            return False
        index = getattr(self, "_shape_edit_drag_vertex", None)
        if index is None:
            return self._update_shape_edit_hover(x, y)
        point = self._shape_geographic_at(x, y)
        if point is None:
            return True
        point = self._clamp_shape_point(point)
        if point == getattr(self, "_shape_edit_drag_point", None):
            return True
        self._shape_edit_drag_point = point
        self._shape_error = ""
        self._publish_shape_overlay()
        return True

    def _shape_edit_release(self, x, y):
        """Commit the dragged corner once, validating the whole polygon."""
        if not self.is_shape_editing():
            return False
        index = getattr(self, "_shape_edit_drag_vertex", None)
        if index is None:
            return False
        point = self._shape_geographic_at(x, y)
        if point is not None:
            self._shape_edit_drag_point = self._clamp_shape_point(point)
        self._commit_shape_drag(getattr(self, "_shape_edit_drag_point", None))
        return True

    def _commit_shape_drag(self, point):
        """Store ``point`` as the dragged corner and persist it once.

        The drag preview is discarded either way. A rejected or unsaveable
        drop leaves the stored geometry untouched and records why, so Enter
        can keep edit mode open while the message is visible.
        """
        index = getattr(self, "_shape_edit_drag_vertex", None)
        origin = getattr(self, "_shape_edit_drag_origin", None)
        original = getattr(self, "_shape_edit_drag_vertices", None)
        element = self._shape_element(getattr(self, "_shape_edit_uid", None))
        manager = self._shape_manager()
        self._shape_edit_reset_drag()
        self._shape_edit_vertex = index
        if element is None or manager is None:
            self._shape_error = "shape storage is unavailable"
            self._publish_shape_overlay()
            return False
        if point is None or origin is None or original is None or index is None:
            self._publish_shape_overlay()
            return False
        if point == origin:
            # Dropping a corner where it started writes nothing.
            self._shape_error = ""
            self.update()
            self._publish_shape_overlay()
            return False

        updated = list(original)
        updated[index] = point
        try:
            # The command validates the full polygon before storing it, so a
            # crossing or degenerate drop never reaches memory or disk.
            validate_vertices(element.kind, tuple(updated))
        except ShapeValidationError as error:
            self._shape_error = str(error)
            self._publish_shape_overlay()
            return False

        self._shape_edit_edge = None
        self._shape_edit_point = None
        self._shape_error = ""
        error = self._push_shape_vertices(
            "Move shape vertex", element, manager, original, updated
        )
        if error:
            self._shape_error = error
            self._publish_shape_overlay()
            return False
        return True

    # -- shape vertex history ------------------------------------------

    def set_undo_stack(self, stack):
        """Attach the window's undo history to shape vertex edits."""
        self.undo_stack = stack

    def _shape_vertices_refreshed(self):
        """Repaint and announce geometry a command has already persisted."""
        self.update()
        self._publish_shape_overlay()
        changed = getattr(self, "shapes_changed", None)
        if changed is not None:
            changed.emit()

    def _store_shape_vertices(self, element, manager, vertices):
        """Direct, history-free write used when no undo stack is attached."""
        previous = tuple(tuple(vertex) for vertex in element.vertices)
        element.vertices = tuple(vertices)
        save = getattr(manager, "save", None)
        if callable(save):
            try:
                save()
            except OSError as error:
                try:
                    element.vertices = previous
                except ShapeValidationError:
                    pass
                return f"could not save the shape: {error}"
        self._shape_vertices_refreshed()
        return ""

    def _push_shape_vertices(self, label, element, manager, before, after):
        """Apply one vertex change through history, or directly without it.

        Returns the surfaceable error text, empty on success. A command that
        fails validation or persistence is stepped off the history so the
        stack keeps matching the geometry that is actually stored.
        """
        stack = getattr(self, "undo_stack", None)
        if stack is None:
            return self._store_shape_vertices(element, manager, after)
        command = SetShapeVerticesCommand(
            self._shape_vertices_refreshed,
            manager,
            element,
            before,
            after,
            label,
        )
        stack.push(command)
        if command.error:
            stack.undo()
            return command.error
        return ""

    def finish_shape_edit(self):
        """Commit an active corner drag, then leave edge edit mode.

        Enter and Escape both end edit mode. A rejected drop keeps the mode
        open so its error stays visible; otherwise leaving edit mode never
        writes geometry or adds a command by itself.
        """
        if not self.is_shape_editing():
            return False
        if self.is_shape_vertex_dragging():
            self._commit_shape_drag(getattr(self, "_shape_edit_drag_point", None))
            if self._shape_error:
                return False
        return self.cancel_shape_edit()

    def _record_shape_edit_click(self, x, y):
        """Insert a vertex on the edge under the pointer, then persist it.

        The insert is validated by the same routine the collection uses, so a
        click that would duplicate a vertex, cross an edge or leave the
        polygon invalid is refused with the geometry untouched.
        """
        if not self.is_shape_editing():
            return False
        hit = self._shape_edit_edge_at(x, y)
        if hit is None:
            return False
        edge, point = hit
        element = self._shape_element(getattr(self, "_shape_edit_uid", None))
        manager = self._shape_manager()
        if element is None or manager is None:
            self._shape_error = "shape storage is unavailable"
            self._publish_shape_overlay()
            return True

        vertices = [tuple(vertex) for vertex in element.vertices]
        if edge < 0 or edge >= len(vertices):
            self._shape_error = "the edge is no longer part of the shape"
            self._publish_shape_overlay()
            return True
        if any(
            abs(vertex[0] - point[0]) < 1e-9 and abs(vertex[1] - point[1]) < 1e-9
            for vertex in vertices
        ):
            self._shape_error = "a vertex already exists at that point"
            self._publish_shape_overlay()
            return True

        updated = list(vertices)
        updated.insert(edge + 1, point)
        try:
            validate_vertices(element.kind, tuple(updated))
        except ShapeValidationError as error:
            self._shape_error = str(error)
            self._publish_shape_overlay()
            return True

        previous = tuple(vertices)
        self._shape_edit_edge = None
        self._shape_edit_point = None
        self._shape_edit_vertex = None
        self._shape_error = ""
        error = self._push_shape_vertices(
            "Add shape vertex", element, manager, previous, updated
        )
        if error:
            self._shape_error = error
            self._publish_shape_overlay()
        return True

    def finish_shape_drawing(self):
        """Validate the draft, persist it and announce the new shape."""
        if not self.is_shape_drawing():
            return None
        kind = self._shape_draft_kind
        points = self._shape_points_for_finish()
        if len(points) < MIN_POLYGON_VERTICES:
            self._shape_error = (
                f"a polygon needs at least {MIN_POLYGON_VERTICES} vertices"
            )
            self._publish_shape_overlay()
            return None
        manager = self._shape_manager()
        if manager is None:
            self._shape_error = "shape storage is unavailable"
            self._publish_shape_overlay()
            return None
        try:
            shape = manager.create(tuple(points), kind=kind, save=True)
        except ShapeValidationError as error:
            # Keep the draft: the user can add a vertex or Escape to discard,
            # rather than losing the polygon to one bad edge.
            self._shape_error = str(error)
            self._publish_shape_overlay()
            return None

        self._shape_draft_kind = None
        self._shape_draft_points = []
        self._shape_draft_preview = None
        self._shape_error = ""
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.apply_map_cursor()
        self.update()
        self._publish_shape_overlay()
        changed = getattr(self, "shapes_changed", None)
        if changed is not None:
            changed.emit()
        return shape

    # -- pointer forwarding --------------------------------------------

    def _record_shape_click(self, x, y):
        """Add a vertex, or close the draft when the first vertex is hit."""
        if not self.is_shape_drawing():
            return False
        points = getattr(self, "_shape_draft_points", [])
        if points and self._shape_point_near(x, y, points[0]):
            self.finish_shape_drawing()
            return True
        point = self._shape_geographic_at(x, y)
        if point is None:
            return False
        points = list(points)
        points.append(point)
        self._shape_draft_points = points
        self._shape_draft_preview = None
        self._shape_error = ""
        self.update()
        self._publish_shape_overlay()
        return True

    def _record_shape_double_click(self, x, y):
        """Finish on the double-click that follows the last vertex."""
        if not self.is_shape_drawing():
            return False
        self.finish_shape_drawing()
        return True

    def _update_shape_preview(self, x, y):
        """Track the rubber-band edge without committing a vertex."""
        if not self.is_shape_drawing():
            return False
        point = self._shape_geographic_at(x, y)
        if point is None:
            return False
        if getattr(self, "_shape_draft_preview", None) == point:
            return True
        self._shape_draft_preview = point
        self._publish_shape_overlay()
        return True

    # -- projection helpers --------------------------------------------

    def _shape_geographic_at(self, x, y):
        scene_at = getattr(self, "scene_geographic_at", None)
        if callable(scene_at):
            return scene_at(QPointF(float(x), float(y)))
        convert = getattr(self, "convert_screen_to_geographic", None)
        if convert is None:
            return None
        return tuple(convert(float(x), float(y)).get_degrees())

    def _shape_screen_point(self, point):
        """Screen position of a vertex under the drawn camera and pan."""
        project = getattr(self, "scene_projection", None)
        rect_of = getattr(self, "viewport_rect", None)
        if not callable(project) or not callable(rect_of):
            return None
        latitude, longitude, zoom, offset_x, offset_y = project()
        rect = rect_of()
        world = 256.0 * (2.0 ** float(zoom))
        limit = getattr(self, "_MAX_MERCATOR_LATITUDE", 85.05112878)
        point_latitude = max(-limit, min(limit, float(point[0])))
        centre_latitude = max(-limit, min(limit, float(latitude)))
        dx = (float(point[1]) - float(longitude)) * world / 360.0
        dx -= round(dx / world) * world
        return (
            rect.width() / 2.0 + dx + float(offset_x),
            rect.height() / 2.0
            + self._mercator_y(point_latitude, world)
            - self._mercator_y(centre_latitude, world)
            + float(offset_y),
        )

    def _shape_point_near(self, x, y, point):
        screen = self._shape_screen_point(point)
        if screen is None:
            return False
        return (
            math.hypot(float(x) - screen[0], float(y) - screen[1])
            <= self._SHAPE_CLOSE_RADIUS
        )

    def _shape_points_for_finish(self):
        """Drop the repeated vertices a closing click or a double-click adds."""
        points = list(getattr(self, "_shape_draft_points", []))
        while len(points) > 1 and points[-1] == points[-2]:
            points.pop()
        if len(points) > 1 and points[-1] == points[0]:
            points.pop()
        return points

    # -- overlay payload -----------------------------------------------

    def _publish_shape_overlay(self):
        """Rebuild and republish the scene overlay after a shape edit."""
        self._viewport_overlay_cache = None
        publish = getattr(self, "publish_route_overlay", None)
        if callable(publish):
            publish(force=True)

    def shape_draft_payload(self):
        kind = getattr(self, "_shape_draft_kind", None)
        if kind is None:
            return None
        preview = getattr(self, "_shape_draft_preview", None)
        return {
            "kind": kind,
            "points": [
                list(point) for point in getattr(self, "_shape_draft_points", [])
            ],
            "preview": None if preview is None else list(preview),
            "colour": list(self._SHAPE_OUTLINE_COLOURS.get(kind, DEFAULT_COLOUR)),
            "error": getattr(self, "_shape_error", ""),
        }

    def shape_edit_payload(self):
        """The selected polygon, hovered corner and edge preview.

        ``edge`` indexes the selected polygon's segment ring, so the closing
        segment is included as the last edge. ``point`` is the geo-snapped
        insertion point the canvas projects again every frame; it is ``None``
        while a corner is hovered or the pointer is away from the shape.
        ``vertex`` is the hovered corner index. ``drag`` carries the corner
        being dragged and its live position, so the canvas moves the handle
        without a second geometry path; it is ``None`` outside a drag.
        """
        uid = getattr(self, "_shape_edit_uid", None)
        if uid is None:
            return None
        edge = getattr(self, "_shape_edit_edge", None)
        point = getattr(self, "_shape_edit_point", None)
        drag_index = getattr(self, "_shape_edit_drag_vertex", None)
        drag_point = getattr(self, "_shape_edit_drag_point", None)
        drag = None
        if drag_index is not None:
            drag = {
                "index": drag_index,
                "point": None if drag_point is None else list(drag_point),
            }
        return {
            "uid": uid,
            "edge": edge,
            "point": None if point is None else list(point),
            "vertex": getattr(self, "_shape_edit_vertex", None),
            "dragging": drag_index is not None,
            "drag": drag,
            "error": getattr(self, "_shape_error", ""),
        }

    def map_shapes_overlay_payload(self):
        """Drawable shapes, plus the draft, for the canvas overlay layer.

        Only the metadata a label needs is published; geometry stays the
        collection's. ``key`` is what the overlay cache is invalidated on, so
        it carries the collection revision and each element revision. A colour
        edit bumps the element revision, so the cache cannot serve a stale
        colour.

        ``enabled`` gates routing, not drawing: a disabled shape is still
        drawn, styled apart through ``disabled`` so the difference is visible
        without the shape disappearing. Visibility alone decides whether the
        overlay includes it.
        """
        manager = self._shape_manager()
        shapes = []
        revision = 0
        if manager is not None:
            revision = getattr(manager, "revision", 0) or 0
            for element in manager:
                if not self._shape_effective_visible(element):
                    continue
                try:
                    data = element.to_map_shape()
                except (ShapeValidationError, TypeError, ValueError):
                    continue
                colour = colour_rgb(getattr(data, "colour", ""), data.kind)
                shapes.append(
                    {
                        "id": data.id,
                        "name": data.name,
                        "note": data.note,
                        "kind": data.kind,
                        "label": data.name or data.note,
                        "vertices": [[lat, lon] for lat, lon in data.vertices],
                        "revision": getattr(element, "revision", 0),
                        "colour": list(colour),
                        "disabled": not bool(getattr(element, "enabled", True)),
                    }
                )
        return {
            "revision": revision,
            "shapes": shapes,
            "draft": self.shape_draft_payload(),
            "edit": self.shape_edit_payload(),
            "key": self.map_shapes_key(),
        }

    def map_shapes_key(self):
        """Cache key over the collection, its elements, draft and edit hover."""
        manager = self._shape_manager()
        if manager is None:
            return (0, (), self._shape_draft_key(), self._shape_edit_key())
        elements = tuple(
            (
                getattr(element, "uid", ""),
                getattr(element, "revision", 0),
                bool(getattr(element, "visible", True)),
                bool(getattr(element, "enabled", True)),
            )
            for element in manager
        )
        return (
            getattr(manager, "revision", 0) or 0,
            elements,
            self._shape_draft_key(),
            self._shape_edit_key(),
        )

    def _shape_draft_key(self):
        if not self.is_shape_drawing():
            return None
        return (
            getattr(self, "_shape_draft_kind", None),
            tuple(getattr(self, "_shape_draft_points", [])),
            getattr(self, "_shape_draft_preview", None),
            getattr(self, "_shape_error", ""),
        )

    def _shape_edit_key(self):
        if not self.is_shape_editing():
            return None
        return (
            getattr(self, "_shape_edit_uid", None),
            getattr(self, "_shape_edit_edge", None),
            getattr(self, "_shape_edit_point", None),
            getattr(self, "_shape_edit_vertex", None),
            getattr(self, "_shape_edit_drag_vertex", None),
            getattr(self, "_shape_edit_drag_point", None),
            getattr(self, "_shape_error", ""),
        )

    def _shape_effective_visible(self, element):
        """Scene ownership decides visibility; the element flag is the mirror.

        ``SceneModel.apply_visibility`` pushes the effective flag onto the
        element, so the element value already covers hidden groups. A shape
        that the scene has registered is read from the scene directly, so a
        group changed without a re-push is still respected.
        """
        visible = bool(getattr(element, "visible", True))
        scene = getattr(getattr(self, "controller", None), "scene", None)
        lookup = getattr(scene, "item", None)
        effective = getattr(scene, "effective_visible", None)
        if not callable(lookup) or not callable(effective):
            return visible
        for key in (getattr(element, "uid", None), getattr(element, "name", None)):
            if key and lookup("shape", key) is not None:
                return bool(effective("shape", key))
        return visible
