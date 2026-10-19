"""Experiment-only latest-request preparation: one active and one pending slot."""

from dataclasses import dataclass
from threading import Condition, Event, Thread


@dataclass(frozen=True)
class Request:
    generation: int
    identity: tuple


class CoverageScheduler:
    def __init__(self, prepare):
        self.prepare = prepare
        self.condition = Condition()
        self.generation = 0
        self.pending = self.active = self.result = None
        self.cancel = None
        self.closed = False
        self.events = []
        self.thread = Thread(target=self._run, name="coverage-preparation")
        self.thread.start()

    def request(self, identity):
        with self.condition:
            if self.closed:
                raise RuntimeError("Scheduler closed")
            self.generation += 1
            request = Request(self.generation, tuple(identity))
            if self.cancel:
                self.cancel.set()
            if self.result is not None:
                self.events.append(("discarded-ready", self.result[0].generation))
            self.pending, self.result = request, None
            self.events.append(("requested", request.generation))
            self.condition.notify_all()
            return request

    def take(self):
        with self.condition:
            result, self.result = self.result, None
            return result

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                request, self.pending = self.pending, None
                self.active, self.cancel = request, Event()
                cancel = self.cancel
                self.events.append(("started", request.generation))
            value = error = None
            try:
                value = self.prepare(request, cancel)
            except Exception as exc:
                error = str(exc)
            with self.condition:
                if not self.closed and request.generation == self.generation:
                    self.result = (request, value, error)
                    self.events.append(("completed", request.generation))
                else:
                    self.events.append(("discarded", request.generation))
                self.active = self.cancel = None
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed, self.pending, self.result = True, None, None
            if self.cancel:
                self.cancel.set()
            self.condition.notify_all()
        self.thread.join(10)
        if self.thread.is_alive():
            raise RuntimeError("Coverage preparation failed to stop")
