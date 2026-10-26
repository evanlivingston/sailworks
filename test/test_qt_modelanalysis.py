from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from gweatherrouting.qt.analysisdisplay import BoundDisplayForecastSelection
from gweatherrouting.qt.controller import SessionController


def test_session_starts_in_published_model_mode_without_legacy_fallback(tmp_path):
    app = QApplication.instance() or QApplication([])
    session = SessionController(load_charts=False, analysis_root=tmp_path)
    analysis = session.analysis
    assert analysis is session.models_analysis
    assert isinstance(session.display_forecast, BoundDisplayForecastSelection)

    if analysis.busy or analysis.loading:
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)

        def changed():
            if not analysis.busy and not analysis.loading:
                loop.quit()

        analysis.changed.connect(changed)
        timer.timeout.connect(loop.quit)
        timer.start(5000)
        loop.exec()
        analysis.changed.disconnect(changed)
    assert not analysis.busy and not analysis.loading
    assert not hasattr(session, "use_legacy_weather")
    session.close()
