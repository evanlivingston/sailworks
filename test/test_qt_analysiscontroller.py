from threading import Event

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from gweatherrouting.core.forecasttranche import ModelConfiguration
from gweatherrouting.qt.analysiscontroller import AnalysisController
from test.analysis_qt_fixtures import FakeAnalysisState, make_snapshot


def app():
    return QApplication.instance() or QApplication([])


def wait_for(predicate, application, timeout=2000):
    if predicate():
        return
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(0)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    poll.start()
    timer.start(timeout)
    loop.exec()
    poll.stop()
    timer.stop()
    if not predicate():
        pytest.fail("Qt worker result was not delivered")


class DeferredThread:
    pending = []

    def __init__(self, *, target, **kwargs):
        self.target = target
        type(self).pending.append(self)

    def start(self):
        pass

    def run(self):
        from threading import Thread

        worker = Thread(target=self.target)
        worker.start()
        worker.join(2)
        assert not worker.is_alive()


def test_startup_required_fields_and_refresh_preserves_published_snapshot(tmp_path):
    application = app()
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    controller = AnalysisController(state=state)
    wait_for(lambda: not controller.loading, application)
    original = controller.snapshot()
    with pytest.raises(ValueError, match="required fields"):
        controller.snapshot(("pressure",))
    state.next_refresh = make_snapshot(tmp_path, "tranche-b", "GFS")
    assert controller.refresh()
    assert controller.active is original
    wait_for(lambda: not controller.loading, application)
    assert controller.snapshot().tranche.tranche_id == "tranche-b"
    state.next_refresh = None
    previous = controller.active
    assert controller.refresh()
    wait_for(lambda: not controller.loading, application)
    assert controller.active is previous
    assert controller.error == "refresh failed"
    controller.close()


def test_unavailable_startup_and_close_cancel_late_worker_result(tmp_path):
    application = app()
    empty = AnalysisController(state=FakeAnalysisState(tmp_path))
    wait_for(lambda: not empty.loading, application)
    assert empty.active is None and "active forecast" in empty.error.lower()
    with pytest.raises(RuntimeError):
        empty.snapshot()
    empty.close()

    entered, release = Event(), Event()
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    next_snapshot = make_snapshot(tmp_path, "late", "HRRR")
    original_refresh = state.refresh

    def slow_refresh(cancel=None, progress=None, action_token=None):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("refresh test was not released")
        state.next_refresh = next_snapshot
        return original_refresh(
            cancel=cancel, progress=progress, action_token=action_token
        )

    state.refresh = slow_refresh
    controller = AnalysisController(state=state)
    wait_for(lambda: not controller.loading, application)
    old = controller.active
    controller.refresh()
    assert entered.wait(2)
    old_invalidation_count = state.invalidations
    controller.close()
    release.set()
    controller._thread.join(2)
    assert not controller._thread.is_alive()
    application.processEvents()
    assert controller.active is old
    assert state.active is old
    assert state.invalidations == old_invalidation_count + 1


def test_progress_report_exposes_current_model_and_percentage(tmp_path):
    application = app()
    controller = AnalysisController(
        state=FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    )
    wait_for(lambda: not controller.loading, application)

    controller._generation += 1
    controller._progress_token = "progress-token"
    controller._progress(controller._generation, "progress-token", ("HRRR", 42))

    assert controller.status == "Downloading HRRR…"
    assert controller.progress == 42
    controller.close()


def test_select_retires_old_snapshot_and_restores_it_on_failure(tmp_path):
    application = app()
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    controller = AnalysisController(state=state)
    wait_for(lambda: not controller.loading, application)
    old = controller.active
    assert controller.select_model("HRRR")
    assert controller.active is None
    wait_for(lambda: not controller.loading, application)
    assert controller.active.entry.model_id == "HRRR"
    state.select_model = lambda _, **kwargs: (_ for _ in ()).throw(
        ValueError("selection failed")
    )
    old = controller.active
    controller.select_model("GFS")
    wait_for(lambda: not controller.loading, application)
    assert controller.active is old
    assert controller.error == "selection failed"
    controller.close()


def test_configuration_persists_intent_without_replacing_active_snapshot(tmp_path):
    application = app()
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    controller = AnalysisController(state=state)
    wait_for(lambda: not controller.loading, application)
    old = controller.active
    configuration = ModelConfiguration("GFS", ("wind",), (0, -1, 1, 1), "native", 0)
    controller.configure((configuration,), "GFS")
    assert state.configuration.refresh_model_id == "GFS"
    assert controller.active is old
    controller.close()


def test_root_injection_uses_only_the_temporary_analysis_store(tmp_path):
    application = app()
    controller = AnalysisController(root=tmp_path)
    wait_for(lambda: not controller.loading, application)
    assert controller.state.store.root == tmp_path.resolve()
    assert controller.active is None
    unavailable = controller.error or controller.status
    assert "active" in unavailable.lower() or "published" in unavailable.lower()
    controller.close()


def test_backend_action_order_is_reserved_before_deferred_workers_start(tmp_path):
    application = app()
    DeferredThread.pending = []
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    controller = AnalysisController(state=state, thread_factory=DeferredThread)
    startup = DeferredThread.pending.pop(0)
    startup.run()
    application.processEvents()
    assert controller.active.entry.model_id == "GFS"

    # A is reserved first but deliberately not started. B reserves later and
    # commits first; starting A afterward must not invalidate or replace B.
    controller.select_model("HRRR")
    older = DeferredThread.pending.pop(0)
    controller.select_model("GFS")
    newer = DeferredThread.pending.pop(0)
    newer.run()
    committed_generation = state.action_generation
    older.run()
    application.processEvents()
    assert state.action_generation == committed_generation
    assert state.active.entry.model_id == "GFS"
    assert controller.active.entry.model_id == "GFS"
    controller.close()


def test_configure_after_backend_commit_accepts_queued_snapshot(tmp_path):
    application = app()
    DeferredThread.pending = []
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    state.next_refresh = make_snapshot(tmp_path, "committed", "HRRR")
    controller = AnalysisController(state=state, thread_factory=DeferredThread)
    DeferredThread.pending.pop(0).run()
    application.processEvents()

    controller.refresh()
    worker = DeferredThread.pending.pop(0)
    worker.run()  # Backend commit is complete; _finish is still Qt-queued.
    assert controller.busy and controller.active.tranche.tranche_id == "tranche-a"
    configuration = ModelConfiguration("GFS", ("wind",), (0, -1, 1, 1), "native", 0)
    controller.configure((configuration,), "GFS")
    assert controller.busy
    application.processEvents()
    assert not controller.busy
    assert controller.active.tranche.tranche_id == "committed"
    controller.close()


def test_action_queued_before_close_cannot_reach_backend_after_close(tmp_path):
    application = app()
    DeferredThread.pending = []
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path))
    controller = AnalysisController(state=state, thread_factory=DeferredThread)
    DeferredThread.pending.pop(0).run()
    application.processEvents()
    controller.select_model("HRRR")
    stale = DeferredThread.pending.pop(0)
    generation = state.action_generation
    controller.close()
    stale.run()
    application.processEvents()
    assert state.action_generation == generation + 1
    assert state.active.entry.model_id == "GFS"


@pytest.mark.parametrize(
    "first_action,failed_action",
    [("select", "select"), ("refresh", "refresh"), ("clear", "select")],
)
def test_failed_new_action_reconciles_backend_commit_with_queued_result(
    tmp_path, first_action, failed_action
):
    application = app()
    DeferredThread.pending = []
    state = FakeAnalysisState(tmp_path, make_snapshot(tmp_path, "old", "GFS"))
    controller = AnalysisController(state=state, thread_factory=DeferredThread)
    DeferredThread.pending.pop(0).run()
    application.processEvents()
    original = controller.active

    if first_action == "select":
        controller.select_model("HRRR")
    elif first_action == "refresh":
        state.next_refresh = make_snapshot(tmp_path, "commit-a", "HRRR")
        controller.refresh()
    else:
        controller.clear_selection()
    action_a = DeferredThread.pending.pop(0)
    action_a.run()  # Backend commit; its Qt result remains queued.

    if failed_action == "refresh":
        state.next_refresh = None
        controller.refresh()
    else:
        state.select_model = lambda _model, **kwargs: (_ for _ in ()).throw(
            ValueError("synthetic precommit selection failure")
        )
        controller.select_model("GFS")
    action_b = DeferredThread.pending.pop(0)
    action_b.run()
    application.processEvents()

    expected = None if first_action == "clear" else state.active
    assert expected is not original or first_action == "clear"
    assert controller.active is expected
    assert state.active is expected
    assert controller.error
    controller.close()
