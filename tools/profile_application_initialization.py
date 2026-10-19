"""Bounded offline initialization attribution on a private startup fixture."""

import argparse
import cProfile
import json
import os
from pathlib import Path
import pstats
import socket
import time

import benchmark_startup_rendering as startup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    manifest = startup.verify_snapshot(args.snapshot)
    sandbox = args.directory / "sandbox"
    startup.prepare_sandbox(args.snapshot, sandbox)
    os.environ.update(
        HOME=str(sandbox / "home"),
        XDG_DATA_HOME=str(sandbox / "xdg-data"),
        XDG_CONFIG_HOME=str(sandbox / "xdg-config"),
        XDG_CACHE_HOME=str(sandbox / "xdg-cache"),
    )

    def blocked(*_args, **_kwargs):
        raise RuntimeError("Network disabled in initialization attribution")

    socket.socket.connect = socket.socket.connect_ex = blocked
    profile = cProfile.Profile()
    profile.enable()
    began = time.perf_counter()
    from PySide6.QtWidgets import QApplication
    from gweatherrouting.core.marine.credentials import SystemCredentialStore
    from gweatherrouting.qt.controller import SessionController
    from gweatherrouting.qt.window import PreviewWindow

    SystemCredentialStore.load = lambda _self: None
    imported = time.perf_counter()
    app = QApplication([])
    controller = SessionController(offline=True, load_charts=not args.no_charts)
    controlled = time.perf_counter()
    window = PreviewWindow(controller)
    constructed = time.perf_counter()
    window.show()
    app.processEvents()
    shown = time.perf_counter()
    profile.disable()
    rows = pstats.Stats(profile).stats
    result = dict(
        snapshot_sha256=manifest["content_sha256"],
        load_charts=not args.no_charts,
        imports_seconds=imported - began,
        controller_seconds=controlled - imported,
        window_seconds=constructed - controlled,
        show_process_events_seconds=shown - constructed,
        total_seconds=shown - began,
        profile=[
            dict(
                file=Path(key[0]).name,
                line=key[1],
                function=key[2],
                calls=value[1],
                self_seconds=value[2],
                cumulative_seconds=value[3],
            )
            for key, value in sorted(
                rows.items(), key=lambda item: item[1][3], reverse=True
            )[:60]
        ],
    )
    (args.directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    window.workspace.close()
    window.timeline.dispose()
    window.dispose_marine()
    window.map.dispose()
    window.log_map.dispose()
    controller.close()
    window.close()


if __name__ == "__main__":
    main()
