"""Preview the sidebar panel chrome and collapse arrows in isolation.

This builds only the dock chrome and representative panel content, so the
sidebar can be inspected and clicked without starting the full application,
map, weather or forecast stack.

Run it with the project virtual environment:

    .venv/bin/python tools/preview_sidebar.py

The left side shows stacked lone panels; the right side shows tabbed panels.
Every panel header has a triangle arrow that collapses that panel's body.
"""

import argparse
import sys
from pathlib import Path

# Run from the checked-out tree, not an installed copy of sailworks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDockWidget,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sailworks.qt.collapsiblepanel import CollapsiblePanel
from sailworks.qt.dockchrome import style_dock, style_scrollarea
from sailworks.qt.dropdown import Dropdown
from sailworks.qt.panelstack import PanelStack
from sailworks.qt.theme import apply_theme, section_label


def _scroll(content):
    area = QScrollArea()
    style_scrollarea(area)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(content)
    return area


def _page():
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(12, 8, 12, 12)
    layout.setSpacing(8)
    return page, layout


def _row(label, widget):
    row = QHBoxLayout()
    row.addWidget(QLabel(label))
    row.addWidget(widget, 1)
    return row


def _navigation_panel():
    page, layout = _page()
    pages = QTabWidget()
    pages.setDocumentMode(True)
    tracks = QListWidget()
    tracks.addItems(["Track 1 · 138 nm", "Track 2 · 42 nm"])
    routings = QListWidget()
    routings.addItems(["Routing · J120 hugo · t+2h"])
    pois = QListWidget()
    pois.addItems(["Start line", "Windward mark"])
    pages.addTab(tracks, "Tracks")
    pages.addTab(routings, "Routings")
    pages.addTab(pois, "POIs")
    layout.addWidget(pages, 1)
    return page


def _races_panel():
    page, layout = _page()
    actions = QHBoxLayout()
    for text in ("Import race…", "Review from start", "Weather"):
        actions.addWidget(QPushButton(text))
    layout.addLayout(actions)
    races = QListWidget()
    races.addItems(["pnwos2026", "spring-series"])
    layout.addWidget(races, 1)
    return page


def _nmea_panel():
    page, layout = _page()
    layout.addWidget(section_label("Instruments"))
    layout.addWidget(QPushButton("Instruments"))
    source = Dropdown("Source")
    source.addItems(["Loaded log", "Live data source"])
    layout.addWidget(section_label("Data source"))
    layout.addWidget(source)
    track = Dropdown("GPS track source")
    track.addItems(["Auto (most fixes)", "GPS", "Manual"])
    layout.addWidget(track)
    layout.addStretch()
    return page


def _routing_panel():
    page, layout = _page()
    layout.addWidget(section_label("Route setup"))
    form = QFormLayout()
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    polar = Dropdown("Polar")
    polar.addItems(["first317.pol", "j120.pol"])
    algorithm = Dropdown("Algorithm")
    algorithm.addItems(["Isochrone", "Greedy"])
    form.addRow(polar)
    form.addRow(algorithm)
    form.addRow(Dropdown("Departure (local)"))
    layout.addLayout(form)
    layout.addWidget(section_label("Routing limits"))
    form = QFormLayout()
    form.addRow(Dropdown("Max wind"))
    form.addRow("Check coastlines", QCheckBox())
    layout.addLayout(form)
    layout.addStretch()
    return page


def _weather_panel():
    page, layout = _page()
    layout.addWidget(section_label("Wind"))
    layout.addWidget(QCheckBox("Show wind"))
    style = Dropdown("Style")
    style.addItems(["Barbs", "Streamlines"])
    layout.addWidget(style)
    opacity = QSlider(Qt.Orientation.Horizontal)
    opacity.setValue(70)
    layout.addLayout(_row("Wind opacity", opacity))
    layout.addWidget(section_label("Waves"))
    layout.addWidget(QCheckBox("Show waves"))
    layout.addWidget(section_label("Isobars"))
    layout.addWidget(QCheckBox("Show isobars"))
    layout.addStretch()
    return page


def _dock(window, title, name, content, area):
    dock = QDockWidget(title, window)
    dock.setObjectName(name)
    style_dock(dock)
    dock.setWidget(CollapsiblePanel(title, _scroll(content)))
    window.addDockWidget(area, dock)
    return dock


def build_gallery():
    """Build and return the preview window; the caller shows and runs it."""
    window = QMainWindow()
    window.setWindowTitle("Sailworks sidebar preview")
    window.resize(1080, 760)
    window.setDockNestingEnabled(True)
    window.setTabPosition(
        Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North
    )
    placeholder = QLabel("Map viewport")
    placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
    placeholder.setProperty("secondary", True)
    window.setCentralWidget(placeholder)

    navigation = _dock(
        window,
        "Navigation",
        "preview-navigation",
        _navigation_panel(),
        Qt.DockWidgetArea.LeftDockWidgetArea,
    )
    races = _dock(
        window,
        "Races",
        "preview-races",
        _races_panel(),
        Qt.DockWidgetArea.LeftDockWidgetArea,
    )
    window.splitDockWidget(navigation, races, Qt.Orientation.Vertical)
    window.resizeDocks([navigation, races], [420, 280], Qt.Orientation.Vertical)

    nmea = _dock(
        window,
        "NMEA",
        "preview-nmea",
        _nmea_panel(),
        Qt.DockWidgetArea.RightDockWidgetArea,
    )
    routing = _dock(
        window,
        "Routing",
        "preview-routing",
        _routing_panel(),
        Qt.DockWidgetArea.RightDockWidgetArea,
    )
    weather = _dock(
        window,
        "Weather",
        "preview-weather",
        _weather_panel(),
        Qt.DockWidgetArea.RightDockWidgetArea,
    )
    window.tabifyDockWidget(nmea, routing)
    window.tabifyDockWidget(nmea, weather)
    window.resizeDocks([nmea], [380], Qt.Orientation.Horizontal)
    nmea.raise_()

    window.statusBar().showMessage(
        "Click the triangle arrow in a panel header to collapse that panel"
    )
    return window


def build_stack_gallery():
    """Prototype the Blender-style auto-height panel stack."""
    window = QMainWindow()
    window.setWindowTitle("Sailworks panel stack preview")
    window.resize(1080, 760)
    central = QWidget()
    layout = QHBoxLayout(central)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    placeholder = QLabel("Map viewport")
    placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
    placeholder.setProperty("secondary", True)
    layout.addWidget(placeholder, 1)

    stack = PanelStack()
    stack.setFixedWidth(380)
    for title, content in (
        ("Navigation", _navigation_panel()),
        ("Races", _races_panel()),
        ("NMEA", _nmea_panel()),
        ("Routing", _routing_panel()),
        ("Weather", _weather_panel()),
    ):
        stack.add_panel(CollapsiblePanel(title, content))
    layout.addWidget(stack)
    window.setCentralWidget(central)
    window.statusBar().showMessage(
        "Auto-height panels · click a header or arrow to collapse · "
        "drag a header to reorder"
    )
    return window


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stack",
        action="store_true",
        help="Preview the auto-height panel stack instead of dock panels",
    )
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    apply_theme(app)
    window = build_stack_gallery() if args.stack else build_gallery()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
