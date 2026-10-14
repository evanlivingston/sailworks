"""Launch the optional Qt preview without changing the default GTK entrypoint."""

import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="GWeatherRouting Qt frontend preview")
    parser.add_argument("--log", help="Load an interpreted NMEA log")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not open saved instrument connections",
    )
    args = parser.parse_args(argv)
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        parser.error(
            "Qt preview needs Python 3.10+ and the qt extra: pip install '.[qt]'"
        )
    from .controller import SessionController
    from .window import PreviewWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("GWeatherRouting Qt Preview")
    controller = SessionController(offline=args.offline)
    window = PreviewWindow(controller)
    app.aboutToQuit.connect(controller.close)
    window.show()
    controller.start_connections()
    if args.log:
        controller.load_path(args.log)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
