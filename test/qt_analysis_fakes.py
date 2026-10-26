from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from test.analysis_qt_fixtures import FakeAnalysisState


def app():
    return QApplication.instance() or QApplication([])


class FakeAnalysis(QObject):
    changed = Signal()

    def __init__(self, root, configuration=None, active=None):
        super().__init__()
        self.state = FakeAnalysisState(root, active)
        self.configuration = configuration
        self.configuration_error = ""
        self.active = active
        self.busy = False
        self.loading = False
        self.progress = 0
        self.error = ""
        self.status = "Forecast ready" if active else "No published forecast selected"
        self.configure_calls = []
        self.refresh_calls = 0
        self.operation_id = 0
        self.selected = []
        self.cancel_calls = 0

    def configure(self, configurations, refresh_model_id):
        value = SimpleNamespace(
            configurations=tuple(configurations), refresh_model_id=refresh_model_id
        )
        self.configuration = value
        self.configure_calls.append(value)

    def select_model(self, model_id):
        self.selected.append(model_id)

    def close(self):
        pass


class FakeSession(QObject):
    def __init__(self, analysis):
        super().__init__()
        self.prepared = analysis
        self.analysis = analysis
        self.prepare_calls = 0

    def prepare_model_analysis(self):
        self.prepare_calls += 1
        return self.prepared
