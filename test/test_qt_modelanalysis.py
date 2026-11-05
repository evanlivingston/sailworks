from datetime import timedelta
from types import SimpleNamespace

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from sailworks.core.forecasttranche import AnalysisSelection
from sailworks.core.tranchestore import TrancheStore
from sailworks.qt.analysisdisplay import BoundDisplayForecastSelection
from sailworks.qt.controller import SessionController
from sailworks.qt.weatherpanel import ForecastModelSelector
from test.test_forecaststate import ACQUIRED, publish_tranche


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


def test_model_choices_bind_a_shared_run_to_the_newest_tranche(tmp_path):
    store = TrancheStore(tmp_path)
    older, _ = publish_tranche(store, "older", ACQUIRED, ("GFS", "HRRR"))
    newer, newer_entries = publish_tranche(
        store, "newer", ACQUIRED + timedelta(hours=2), ("GFS", "HRRR")
    )
    store.activate(AnalysisSelection(newer.tranche_id, newer_entries[1].entry_id))
    analysis = SimpleNamespace(state=SimpleNamespace(store=store))

    choices = ForecastModelSelector._choices(analysis)

    targets = {model: target for _label, model, target in choices}
    assert targets["GFS"] == (newer.tranche_id, newer_entries[0].entry_id)
    assert older.tranche_id != targets["GFS"][0]
