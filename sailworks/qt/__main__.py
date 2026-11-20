"""Launch the Qt application; also retained as the sailworks-qt alias."""

import argparse
import os
import sys
from typing import cast


def install_optional_capture(app, window, controller):
    """Install the ENC capture only when SAILWORKS_ENC_CAPTURE names a directory.

    The capture module ships separately from the application, so importing it
    is deferred until the environment opts in. A missing module must not stop
    the launch: report it once and continue without capture.
    """
    capture_dir = os.environ.get("SAILWORKS_ENC_CAPTURE")
    if not capture_dir:
        return None
    try:
        from .enc_capture import install_capture
    except ImportError as error:
        print(
            "warning: SAILWORKS_ENC_CAPTURE is set but the ENC capture module "
            f"is unavailable ({error}); continuing without capture for "
            f"{capture_dir}",
            file=sys.stderr,
        )
        return None
    return install_capture(app, window, controller)


def main(argv=None):
    import multiprocessing

    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Sailworks")
    parser.add_argument("--log", help="Load an interpreted NMEA log")
    parser.add_argument(
        "--early-weather",
        action="store_true",
        help="Experimental: defer unused plots and initialize charts after weather preparation",
    )
    parser.add_argument(
        "--map-renderer",
        choices=("cpu", "gpu"),
        help="Override the saved map renderer for this session (GPU is experimental)",
    )

    parser.add_argument(
        "--gpu-resident-fields",
        action="store_true",
        help=(
            "Experimental: keep decoded numerical fields resident in the GPU "
            "presenter under a stable identity, with an explicit budget"
        ),
    )
    parser.add_argument(
        "--native-weather-fields",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Use the native weather texture cache (default: on); "
            "--no-native-weather-fields restores Image sources"
        ),
    )
    args = parser.parse_args(argv)
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
    except ImportError:
        parser.error("Qt requires PySide6; reinstall Sailworks with its dependencies")
    from sailworks import build_label

    from .controller import SessionController
    from .window import PreviewWindow
    from .theme import apply_theme

    app = cast(QApplication, QApplication.instance()) or QApplication([sys.argv[0]])
    label = build_label()
    app.setApplicationName("Sailworks")
    app.setApplicationDisplayName(f"Sailworks — {label}" if label else "Sailworks")
    apply_theme(app)
    controller = SessionController(
        map_renderer=args.map_renderer,
        early_weather=args.early_weather,
        gpu_resident_fields=args.gpu_resident_fields,
        gpu_streamlines=True,
        native_weather_fields=args.native_weather_fields,
        defer_analysis=True,
    )
    window = PreviewWindow(controller)
    app.aboutToQuit.connect(window.workspace.close)
    if window.timeline is not None:
        app.aboutToQuit.connect(window.timeline.dispose)
    app.aboutToQuit.connect(window.dispose_marine)
    app.aboutToQuit.connect(controller.close)
    window.show()
    # The first frame is on screen, so the published-forecast analysis can now
    # start: its imports and reads must not compete with the first paint.
    QTimer.singleShot(0, controller.start_model_analysis)
    if controller.chart_startup is not None:
        controller.chart_startup.start(window._active_map())
    controller.start_connections()
    if args.log:
        controller.load_path(args.log)
    # Inert unless SAILWORKS_ENC_CAPTURE names a directory.
    install_optional_capture(app, window, controller)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
