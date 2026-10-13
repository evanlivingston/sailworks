"""Small, versioned workspace preferences, separate from instrument history."""

import json
import math
import os
import tempfile
from pathlib import Path

from .storage import DATA_DIR


def number(value, default, minimum, maximum):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return default
    return max(minimum, min(maximum, value))


def mapping(value):
    return value if isinstance(value, dict) else {}


def window_geometry(value, workareas):
    """Fit saved window bounds onto an available monitor's work area."""
    value = mapping(value)
    x = int(number(value.get("x"), 0, -100000, 100000))
    y = int(number(value.get("y"), 0, -100000, 100000))
    width = int(number(value.get("width"), 1280, 200, 20000))
    height = int(number(value.get("height"), 768, 100, 20000))
    if workareas:

        def distance(area):
            ax, ay, aw, ah = area
            return (
                max(ax - x, x - (ax + aw - 80), 0) ** 2
                + max(ay - y, y - (ay + ah - 40), 0) ** 2
            )

        ax, ay, aw, ah = min(workareas, key=distance)
        width, height = min(width, aw), min(height, ah)
        x, y = max(ax, min(x, ax + aw - width)), max(ay, min(y, ay + ah - height))
    return {"x": x, "y": y, "width": width, "height": height}


class WorkspaceStateStore:
    VERSION = 1

    def __init__(self, path=None):
        self.path = (
            Path(path) if path is not None else Path(DATA_DIR) / "workspace.json"
        )

    def load(self):
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("version") == self.VERSION:
                return mapping(saved.get("state"))
        except (OSError, ValueError):
            pass
        return {}

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".workspace-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(
                    {"version": self.VERSION, "state": state},
                    stream,
                    allow_nan=False,
                    indent=2,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
