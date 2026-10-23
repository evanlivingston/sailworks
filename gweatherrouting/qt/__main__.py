"""Launch the Qt application; also retained as the gweatherrouting-qt alias."""

import argparse
import sys
from typing import cast


def main(argv=None):
    parser = argparse.ArgumentParser(description="GWeatherRouting")
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
        "--offline",
        action="store_true",
        help="Do not open saved instrument connections",
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
        from PySide6.QtWidgets import QApplication
    except ImportError:
        parser.error(
            "Qt requires PySide6; reinstall GWeatherRouting with its dependencies"
        )
    from .controller import SessionController
    from .window import PreviewWindow
    from .theme import apply_theme

    app = cast(QApplication, QApplication.instance()) or QApplication([sys.argv[0]])
    app.setApplicationName("GWeatherRouting")
    app.setApplicationDisplayName("GWeatherRouting")
    apply_theme(app)
    controller = SessionController(
        offline=args.offline,
        map_renderer=args.map_renderer,
        early_weather=args.early_weather,
        gpu_resident_fields=args.gpu_resident_fields,
        native_weather_fields=args.native_weather_fields,
    )
    window = PreviewWindow(controller)
    app.aboutToQuit.connect(window.workspace.close)
    app.aboutToQuit.connect(window.timeline.dispose)
    app.aboutToQuit.connect(window.dispose_marine)
    app.aboutToQuit.connect(controller.close)
    window.show()
    if controller.chart_startup is not None:
        controller.chart_startup.start(window._active_map())
    controller.start_connections()
    if args.log:
        controller.load_path(args.log)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
