"""Display-only Currents and Tides tools over the shared marine models.

Downloads and saved-source management live in the Data Library
(see marinelibrary); these docks show visibility toggles and coverage.
"""

import json
from datetime import datetime, timezone

from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.marine.fields import Area, FieldSelection
from gweatherrouting.core.marine.models import Capability, utc_time
from gweatherrouting.core.marine.presentation import (
    current_coverage,
    stations_in_view,
    source_intervals,
)
from gweatherrouting.rendering.marinepresentation import (
    CURRENT_DENSITIES,
    chart_instant,
)
from .dockchrome import style_scrollarea
from .theme import symbol_icon


def text_label(text=""):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def interval(manifest):
    return "%s → %s UTC" % (
        utc_time(manifest["valid_start"]).strftime("%d %b %H:%M"),
        utc_time(manifest["valid_end"]).strftime("%d %b %H:%M"),
    )


class CoverageTarget(QComboBox):
    def __init__(self, core, canvas):
        super().__init__()
        self.core, self.canvas = core, canvas
        self.routes = []
        self.signature = None
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(12)
        self.refresh()

    def refresh(self):
        routes = list(self.core.trackManager) + list(self.core.routingManager)
        signature = tuple((id(route), route.name) for route in routes)
        if signature == self.signature:
            return
        selected = self.currentData()
        self.routes, self.signature = routes, signature
        self.blockSignals(True)
        self.clear()
        self.addItem("Current map area", None)
        for route in routes:
            self.addItem(route.name, route)
        self.setCurrentIndex(
            next((i + 1 for i, r in enumerate(routes) if r is selected), 0)
        )
        self.blockSignals(False)

    def area(self):
        self.refresh()
        route = self.currentData()
        if route is not None:
            return Area.from_points(route.to_list())
        northwest = self.canvas.convert_screen_to_geographic(0, 0).get_degrees()
        southeast = self.canvas.convert_screen_to_geographic(
            self.canvas.width(), self.canvas.height()
        ).get_degrees()
        return Area(
            (northwest[1] + 180) % 360 - 180,
            southeast[0],
            (southeast[1] + 180) % 360 - 180,
            northwest[0],
        )


class DownloadStart(QWidget):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.following = True
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.editor = QDateTimeEdit()
        self.editor.setTimeSpec(Qt.TimeSpec.UTC)
        self.editor.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.editor.setCalendarPopup(True)
        self.editor.setAccessibleName("Download start (UTC)")
        self.editor.dateTimeChanged.connect(self.edited)
        layout.addWidget(self.editor, 1)
        self.now = QToolButton()
        self.now.setIcon(symbol_icon("go-bottom-symbolic"))
        self.now.setToolTip("Select now (UTC)")
        self.now.clicked.connect(self.select_now)
        layout.addWidget(self.now)
        self.refresh()

    def edited(self):
        self.following = False

    def set_time(self, value):
        self.editor.blockSignals(True)
        self.editor.setDateTime(
            QDateTime.fromSecsSinceEpoch(int(value.timestamp()), Qt.TimeSpec.UTC)
        )
        self.editor.blockSignals(False)

    def refresh(self):
        if self.following:
            self.set_time(self.clock.time)

    def select_now(self):
        self.following = False
        self.set_time(datetime.now(timezone.utc))

    def instant(self):
        value = self.editor.dateTime()
        return datetime.fromtimestamp(value.toSecsSinceEpoch(), timezone.utc).replace(
            second=0, microsecond=0
        )


class CurrentSources(QWidget):
    def __init__(self, store, compact=True):
        super().__init__()
        self.store = store
        self.syncing = False
        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.timeout.connect(self.reload)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["Use / Downloaded model", "Resolution", "Valid interval (UTC)"]
        )
        self.tree.setRootIsDecorated(True)
        self.tree.setMinimumHeight(150)
        self.tree.setColumnHidden(1, compact)
        self.tree.setColumnHidden(2, compact)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemChanged.connect(self.toggle)
        self.tree.itemSelectionChanged.connect(self.selected)
        layout.addWidget(self.tree)
        self.remove = QPushButton("Remove selected download")
        self.remove.setToolTip("Expand a model and select a download to remove it.")
        self.remove.clicked.connect(self.remove_selected)
        layout.addWidget(self.remove)
        self.status = text_label()
        layout.addWidget(self.status)
        self.reload()

    def reload(self):
        self.reload_timer.stop()
        selected = self.tree.currentItem()
        selected_key = selected.data(0, Qt.ItemDataRole.UserRole) if selected else None
        roots = [
            self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())
        ]
        selected_group = (
            selected.data(0, Qt.ItemDataRole.UserRole + 1) if selected else None
        )
        expanded = {
            item.data(0, Qt.ItemDataRole.UserRole + 1)
            for item in roots
            if item is not None and item.isExpanded()
        }
        groups: dict[tuple, list] = {}
        for field in self.store.fields.values():
            m = field.manifest()
            key = (m["provider"], m["model"], m["resolution_m"])
            groups.setdefault(key, []).append(field)
        self.syncing = True
        self.tree.clear()
        for key, fields in sorted(
            groups.items(), key=lambda pair: (pair[0][2], pair[0][1], pair[0][0])
        ):
            group_key = json.dumps(key)
            identifiers = tuple(f.id for f in fields)
            enabled = sum(i not in self.store.excluded for i in identifiers)
            intervals = source_intervals([f.manifest() for f in fields])
            parent = QTreeWidgetItem(
                self.tree,
                [
                    "%s\n%d download%s · %g m\n%s"
                    % (
                        key[1],
                        len(fields),
                        "" if len(fields) == 1 else "s",
                        key[2],
                        intervals,
                    ),
                    "%g m" % key[2],
                    intervals,
                ],
            )
            parent.setData(0, Qt.ItemDataRole.UserRole, identifiers)
            parent.setData(0, Qt.ItemDataRole.UserRole + 1, group_key)
            state = (
                Qt.CheckState.Checked
                if enabled == len(fields)
                else (
                    Qt.CheckState.PartiallyChecked
                    if enabled
                    else Qt.CheckState.Unchecked
                )
            )
            parent.setCheckState(0, state)
            parent.setToolTip(
                0, json.dumps(fields[0].manifest(), indent=2, ensure_ascii=False)
            )
            parent.setExpanded(group_key in expanded)
            for field in sorted(
                fields,
                key=lambda f: (
                    f.manifest()["valid_start"],
                    f.manifest().get("run") or f.manifest().get("retrieved_at", ""),
                    f.id,
                ),
                reverse=True,
            ):
                m = field.manifest()
                run = m.get("run") or m.get("retrieved_at")
                run = utc_time(run).strftime("%d %b %H:%M UTC") if run else "unknown"
                child = QTreeWidgetItem(
                    parent,
                    [
                        "%s\nRun %s · %s" % (interval(m), run, field.id[:8]),
                        "%g m" % key[2],
                        interval(m),
                    ],
                )
                child.setData(0, Qt.ItemDataRole.UserRole, field.id)
                child.setCheckState(
                    0,
                    (
                        Qt.CheckState.Unchecked
                        if field.id in self.store.excluded
                        else Qt.CheckState.Checked
                    ),
                )
                child.setToolTip(0, json.dumps(m, indent=2, ensure_ascii=False))
                if selected_key == field.id:
                    parent.setExpanded(True)
                    self.tree.setCurrentItem(child)
            if selected_group == group_key:
                self.tree.setCurrentItem(parent)
        self.syncing = False
        self.selected()

    def toggle(self, item, column):
        if self.syncing or column != 0:
            return
        value = item.data(0, Qt.ItemDataRole.UserRole)
        identifiers = (value,) if isinstance(value, str) else value
        enabled = item.checkState(0) != Qt.CheckState.Unchecked
        self.save(lambda: [self.store.enable(i, enabled) for i in identifiers])

    def selected(self):
        item = self.tree.currentItem()
        self.remove.setEnabled(
            bool(item and isinstance(item.data(0, Qt.ItemDataRole.UserRole), str))
        )

    def remove_selected(self):
        item = self.tree.currentItem()
        identifier = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if isinstance(identifier, str):
            self.save(lambda: self.store.remove(identifier))

    def save(self, operation):
        try:
            operation()
            self.status.clear()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
        # Keep the QTreeWidgetItem alive until its itemChanged signal returns.
        self.reload_timer.start(0)


class MarinePanel(QWidget):
    def __init__(
        self, controller, canvas, kind, settings_callback, library_callback=None
    ):
        super().__init__()
        self.controller, self.canvas, self.kind = controller, canvas, kind
        self.manager = controller.core.marine_manager
        self.clock = controller.time_control
        self.closed = False
        self._ordered_current_fields: tuple | None = None
        self.signature = None
        self.settings_callback = settings_callback
        self.library_callback = library_callback
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = style_scrollarea(QScrollArea())
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        controls = QVBoxLayout(content)
        controls.setContentsMargins(12, 8, 12, 8)
        self.toggle = QCheckBox(
            "Current arrows" if kind == "currents" else "Show station markers"
        )
        self.toggle.setChecked(True)
        self.toggle.toggled.connect(self.set_display)
        row = QHBoxLayout()
        row.addWidget(self.toggle, 1)
        self.settings = QToolButton()
        self.settings.setIcon(symbol_icon("document-properties-symbolic"))
        self.settings.setToolTip("Marine settings")
        self.settings.clicked.connect(lambda: settings_callback(kind))
        row.addWidget(self.settings)
        controls.addLayout(row)
        if kind == "currents":
            density_row = QHBoxLayout()
            density_row.addWidget(text_label("Field arrow density"))
            self.current_density = QComboBox()
            self.current_density.setAccessibleName("Current field arrow density")
            for key, label, _spacing in CURRENT_DENSITIES:
                self.current_density.addItem(label, key)
            self.current_density.setToolTip(
                "Spacing of gridded current arrows: Sparse 128 px, Standard 64 px, "
                "Dense 32 px, Maximum 16 px. Station arrows are unchanged."
            )
            self.refresh_density()
            self.current_density.currentIndexChanged.connect(self.set_density)
            density_row.addWidget(self.current_density)
            controls.addLayout(density_row)
        self.coverage = text_label()
        controls.addWidget(self.coverage)
        self.coverage_detail = text_label()
        if kind == "currents":
            controls.addWidget(self.coverage_detail)
        else:
            self.coverage_detail.hide()
        if kind == "tides":
            controls.addWidget(
                text_label("Water level at stations · metres above source datum")
            )
        self.manage = QPushButton("Manage downloads…")
        self.manage.setToolTip("Download data and manage saved sources")
        self.manage.clicked.connect(self.open_library)
        controls.addWidget(self.manage)
        controls.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.clock.connect("time_change", self.invalidate)
        self.manager.connect("changed", self.invalidate)
        self.manager.fields.connect("changed", self.invalidate_fields)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def open_library(self):
        if self.library_callback is not None:
            self.library_callback(self.kind)
        else:
            self.settings_callback(self.kind)

    def set_display(self, enabled):
        self.canvas.marine.set_display(**{self.kind: enabled})

    def refresh_density(self):
        index = self.current_density.findData(
            self.controller.settings.marineCurrentDensity
        )
        self.current_density.blockSignals(True)
        self.current_density.setCurrentIndex(
            index if index >= 0 else self.current_density.findData("standard")
        )
        self.current_density.blockSignals(False)

    def set_density(self, *_):
        settings = self.controller.settings
        previous = settings.marineCurrentDensity
        try:
            settings["marineCurrentDensity"] = self.current_density.currentData()
        except OSError as exc:
            settings.__dict__["marineCurrentDensity"] = previous
            dict.__setitem__(settings, "marineCurrentDensity", previous)
            QMessageBox.warning(self, "Unable to save setting", str(exc))
        self.refresh_density()
        self.controller.data.redraw()

    def invalidate(self, *_):
        if not self.closed:
            self.signature = None
            self.refresh()

    def invalidate_fields(self, *_):
        self._ordered_current_fields = None
        self.invalidate()

    def refresh(self):
        if self.closed:
            return
        instant = chart_instant(self.clock.time)
        signature = (
            self.canvas.camera_state.center_lat,
            self.canvas.camera_state.center_lon,
            self.canvas.camera_state.zoom,
            self.canvas.width(),
            self.canvas.height(),
            instant,
            bool(self.manager.coverage_planner.worldwide.credentials),
        )
        if signature == self.signature:
            return
        self.signature = signature
        if self.kind == "currents":
            selection = None
            if not getattr(self, "_uncached_coverage_fields", False):
                if self._ordered_current_fields is None:
                    self._ordered_current_fields = self.manager.fields.snapshot().fields
                # Only immutable ordering is retained. Per-query used/missing
                # bookkeeping remains fresh, and sampling is never cached.
                selection = FieldSelection(self._ordered_current_fields)
            available, title, detail = current_coverage(
                self.manager,
                signature[0],
                (signature[1] + 180) % 360 - 180,
                instant,
                selection=selection,
            )
            self.coverage_detail.setText(detail)
        else:
            selection = self.manager.snapshot(Capability.WATER_LEVEL)
            stations = {d.station.id: d.station for d in selection.datasets}
            visible = stations_in_view(stations.values(), self.canvas)
            available = sum(
                selection.sample(s.id, instant).status == "available" for s in visible
            )
            title = (
                "%d of %d stations in view have predictions" % (available, len(visible))
                if visible
                else "No downloaded tide predictions in view"
            )
        self.coverage.setText("● " + title)
        self.coverage.setStyleSheet("color: " + ("#36965c" if available else "#bd862e"))

    def dispose(self):
        if self.closed:
            return
        self.closed = True
        self._ordered_current_fields = None
        self.timer.stop()
        self.clock.disconnect("time_change", self.invalidate)
        self.manager.disconnect("changed", self.invalidate)
        self.manager.fields.disconnect("changed", self.invalidate_fields)
