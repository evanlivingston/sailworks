"""Application interaction handlers shared by Qt map widgets."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
import math

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
        selected = menu.exec(event.globalPos())
        if selected is poi or selected is point:
            self.navigation_request.emit(
                "poi" if selected is poi else "point", position.get_degrees()
            )
        if selected is start or selected is destination:
            self.route_location.emit(
                "start" if selected is start else "destination", position.get_degrees()
            )

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
