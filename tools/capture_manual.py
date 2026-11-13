"""Capture user-manual screenshots from the real Sailworks window.

Every image in the manual is produced by driving the application itself, so a
screenshot cannot drift away from the UI it documents: regenerate with
``make manual`` and review the changed PNGs in the commit.

Usage::

    .venv/bin/python tools/capture_manual.py --list
    .venv/bin/python tools/capture_manual.py --all
    .venv/bin/python tools/capture_manual.py --scene panel-routing
    .venv/bin/python tools/capture_manual.py --check

``--check`` does not open a window: it verifies that every committed image
still matches its manifest entry, which is what CI runs to catch a hand-edited
or missing screenshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "docs/source/_static/images/manual"
MANIFEST_NAME = "manifest.json"
PANEL_WIDTH = 460
PANEL_HEIGHT = 900
PANEL_WINDOW = (1440, 900)
SETTLE_SECONDS = 1.5
CHART_SETTLE_SECONDS = 5.0
SETTLE_DEADLINE = 20.0

Widget = object


@dataclass(frozen=True)
class Scene:
    """One screenshot: a named navigation sequence and what to grab."""

    name: str
    title: str
    description: str
    prepare: Callable[[Widget], Widget]


def _pump(seconds: float = 0.2) -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)


def _select_tab(tab_bar, text: str) -> None:
    for index in range(tab_bar.count()):
        if tab_bar.tabText(index) == text:
            tab_bar.setCurrentIndex(index)
            return
    raise RuntimeError(f"Tab {text!r} not found in {tab_bar!r}")


def _show_panel(window, name: str) -> None:
    window._show_panel(name)
    window.panels[name].set_collapsed(False, animate=False)


def _fit_panel_height(wrapper, limit: int = PANEL_HEIGHT) -> None:
    """Give a panel enough height for its content, capped for the docs page."""

    from PySide6.QtWidgets import QScrollArea

    body = wrapper.body
    header = wrapper.header.height()
    hint = body.sizeHint().height()
    areas = [body] if isinstance(body, QScrollArea) else body.findChildren(QScrollArea)
    for area in areas[:1]:
        inner = area.widget()
        if inner is not None and inner.layout() is not None:
            extra = body.sizeHint().height() - area.sizeHint().height()
            hint = max(hint, inner.layout().totalSizeHint().height() + extra)
    height = min(max(hint, 240), limit)
    wrapper.setFixedHeight(height)
    if isinstance(body, QScrollArea):
        body.setFixedHeight(height - header)


def _panel_scene(
    name: str, title: str, description: str, configure=None, panel: str = None
) -> Scene:
    panel = panel or name

    def prepare(window):
        _show_panel(window, panel)
        side = window._panel_side(panel) or window._panel_home.get(panel, "right")
        for other in window.panels:
            if other == panel:
                continue
            if (window._panel_side(other) or window._panel_home.get(other)) == side:
                window._set_panel_visible(other, False)
        stack = window._panel_stacks[side]
        stack.setMinimumWidth(PANEL_WIDTH)
        stack.setMaximumWidth(PANEL_WIDTH)
        window.resize(*PANEL_WINDOW)
        _pump(0.3)
        if configure is not None:
            configure(window)
        wrapper = window.panels[panel]
        _fit_panel_height(wrapper)
        return wrapper

    return Scene(f"panel-{name}", title, description, prepare)


def _navigation_tracks(window):
    _show_panel(window, "navigation")
    _select_tab(window.navigation_panel.tabs, "Tracks")


def _navigation_routings(window):
    _show_panel(window, "navigation")
    _select_tab(window.navigation_panel.tabs, "Routings")


def _chart(window):
    window.reset_panels()
    for stack in window._panel_stacks.values():
        stack.setMinimumWidth(300)
        stack.setMaximumWidth(560)
    window.resize(*PANEL_WINDOW)
    _pump(0.4)
    presenter = window.map._weather_services.weather_presenter
    widget = presenter.widget
    widget.set_layer_visible("satellite", True)
    _pump(CHART_SETTLE_SECONDS)
    _pump(CHART_SETTLE_SECONDS)
    return widget


def _data_library_charts(window):
    window._ensure_library()
    dialog = window.library_dialog
    dialog.resize(1040, 700)
    dialog.show()
    dialog.show_charts(2)  # Downloads && base maps
    return dialog


def _data_library_forecasts(window):
    window._ensure_library()
    dialog = window.library_dialog
    dialog.resize(1040, 700)
    dialog.show()
    _select_tab(dialog.tabs.tabBar(), "Forecasts")
    return dialog


SCENES = (
    Scene(
        "chart-pnwo",
        "Chart with satellite basemap",
        "Satellite imagery under the chart for the Pacific NW Offshore course and boats.",
        _chart,
    ),
    _panel_scene(
        "navigation",
        "Navigation panel",
        "Tracks, routings, POIs and AIS targets, with the Tracks tab selected.",
        _navigation_tracks,
    ),
    _panel_scene(
        "navigation-routings",
        "Navigation panel: routings",
        "Saved and calculated routes, with the legs of the selected route.",
        _navigation_routings,
        panel="navigation",
    ),
    _panel_scene(
        "routing",
        "Routing panel",
        "Polar, departure and limit options, plus the Calculate route button.",
    ),
    _panel_scene(
        "weather",
        "Weather panel",
        "Forecast selection and the displayed weather fields.",
    ),
    _panel_scene(
        "polar-selector",
        "Polar selector",
        "Choose the polar used for routing and performance readouts.",
    ),
    Scene(
        "data-library-charts",
        "Data Library: charts and downloads",
        "The Downloads && base maps section that installs coastlines and seamarks.",
        _data_library_charts,
    ),
    Scene(
        "data-library-forecasts",
        "Data Library: forecasts",
        "Weather model, region and time range for a forecast download.",
        _data_library_forecasts,
    ),
)

SCENES_BY_NAME = {scene.name: scene for scene in SCENES}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(out_dir: Path) -> Path:
    return out_dir / MANIFEST_NAME


def _read_manifest(out_dir: Path) -> dict:
    path = _manifest_path(out_dir)
    if not path.exists():
        return {"scenes": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(out_dir: Path, scenes: list[dict]) -> None:
    scenes = [entry for entry in scenes if entry["name"] in SCENES_BY_NAME]
    payload = {
        "generator": "tools/capture_manual.py",
        "window": f"{PANEL_WINDOW[0]}x{PANEL_WINDOW[1]}",
        "scenes": sorted(scenes, key=lambda entry: entry["name"]),
    }
    _manifest_path(out_dir).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


class _IsolatedApp:
    """A window backed by a temporary profile, like the Qt integration tests."""

    def __init__(self) -> None:
        self.stack = ExitStack()

    def __enter__(self) -> Widget:
        from unittest.mock import patch

        from PySide6.QtWidgets import QApplication

        from sailworks.qt.theme import apply_theme

        self.temporary = tempfile.TemporaryDirectory()
        self.stack.callback(self.temporary.cleanup)
        self.path = Path(self.temporary.name)

        from sailworks.core.marine.manager import MarineManager

        self.stack.enter_context(
            patch(
                "sailworks.core.core.MarineManager",
                side_effect=lambda _path, **kwargs: MarineManager(
                    self.path / "marine", **kwargs
                ),
            )
        )
        for target in (
            "sailworks.core.storage.Storage.load_or_save_default",
            "sailworks.core.storage.Storage.save",
            "sailworks.core.polarmanager.PolarManager.refresh_local_polars",
        ):
            self.stack.enter_context(patch(target))

        from sailworks.qt.controller import SessionController
        from sailworks.qt.window import PreviewWindow

        self.app = QApplication.instance() or QApplication([])
        apply_theme(self.app)
        self.controller = SessionController(
            recording_directory=self.path / "recordings",
            load_charts=False,
            analysis_root=self.path / "analysis",
        )
        polars = self.controller.core.polar_manager
        polars.polars_files = ["first317.pol"]
        polars.polars = ["first317.pol"]
        polars.get_path = lambda name: str(ROOT / "sailworks/data/polars" / name)
        self.controller.timer.stop()
        self.window = PreviewWindow(
            self.controller, workspace_path=self.path / "workspace-qt.json"
        )
        self.window.resize(*PANEL_WINDOW)
        self.window.show()
        from manual_seed import center_chart, seed

        seed(self.window)
        center_chart(self.window)
        self._settle()
        return self.window

    def __exit__(self, *exc_info) -> None:
        self.window.close()
        self.app.processEvents()
        self.stack.close()

    def _settle(self) -> None:
        deadline = time.monotonic() + SETTLE_DEADLINE
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not getattr(self.controller.data.task, "busy", False):
                break
            time.sleep(0.02)
        _pump(SETTLE_SECONDS)


def _grab(widget):
    content = getattr(widget, "contentItem", None)
    if callable(content) and hasattr(widget, "grabWindow"):
        # A Quick view's grabWindow() can block on the render thread; grabbing
        # the content item is asynchronous and safe.
        from PySide6.QtCore import QEventLoop, QTimer

        result = content().grabToImage()
        loop = QEventLoop()
        result.ready.connect(loop.quit)
        QTimer.singleShot(15000, loop.quit)
        loop.exec()
        image = result.image()
        if not image.isNull():
            return image
    return widget.grab().toImage()


def _capture(scene: Scene, out_dir: Path, window) -> dict:
    target = scene.prepare(window)
    _pump(0.4)
    image = _grab(target)
    if image.isNull():
        raise RuntimeError(f"Scene {scene.name!r} produced an empty image")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{scene.name}.png"
    if not image.save(str(path), "PNG"):
        raise RuntimeError(f"Could not write {path}")
    return {
        "name": scene.name,
        "title": scene.title,
        "description": scene.description,
        "file": path.name,
        "sha256": _sha256(path),
        "width": image.width(),
        "height": image.height(),
    }


def _check(out_dir: Path) -> int:
    manifest = _read_manifest(out_dir)
    entries = {entry["name"]: entry for entry in manifest.get("scenes", [])}
    problems = []
    for name in SCENES_BY_NAME:
        entry = entries.get(name)
        if entry is None:
            problems.append(f"{name}: missing manifest entry")
            continue
        path = out_dir / entry["file"]
        if not path.exists():
            problems.append(f"{name}: missing file {path.name}")
        elif _sha256(path) != entry["sha256"]:
            problems.append(f"{name}: {path.name} does not match the manifest")
    for extra in sorted(set(entries) - set(SCENES_BY_NAME)):
        problems.append(f"{extra}: manifest entry has no scene")
    for problem in problems:
        print(f"stale: {problem}", file=sys.stderr)
    print(
        f"manual screenshots checked: {len(SCENES_BY_NAME)}; problems: {len(problems)}"
    )
    return 1 if problems else 0


def _capture_all(names, out_dir: Path) -> int:
    if not names:
        print("No scene selected; use --all or --scene NAME", file=sys.stderr)
        return 2
    with _IsolatedApp() as window:
        entries = {
            entry["name"]: entry for entry in _read_manifest(out_dir).get("scenes", [])
        }
        captured = 0
        for name in names:
            scene = SCENES_BY_NAME[name]
            entry = _capture(scene, out_dir, window)
            entries[scene.name] = entry
            captured += 1
            print(f"captured {entry['file']} ({entry['width']}x{entry['height']})")
        _write_manifest(out_dir, list(entries.values()))
    print(f"wrote {captured} screenshots to {out_dir}")
    return 0


def _list() -> int:
    for scene in SCENES:
        print(f"{scene.name:24} {scene.title}")
        print(f"{'':24} {scene.description}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scene", action="append", default=[], metavar="NAME")
    parser.add_argument("--all", action="store_true", help="capture every scene")
    parser.add_argument("--list", action="store_true", help="list scenes and exit")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify committed images against the manifest without a window",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if args.list:
        return _list()
    if args.check:
        return _check(args.out)

    unknown = [name for name in args.scene if name not in SCENES_BY_NAME]
    if unknown:
        parser.error(f"unknown scene(s): {', '.join(unknown)}")
    names = list(SCENES_BY_NAME) if args.all else args.scene
    return _capture_all(names, args.out)


if __name__ == "__main__":
    sys.exit(main())
