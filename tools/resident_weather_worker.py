"""Single-owner experiment worker; source handles never leave its thread."""

from concurrent.futures import Future
from queue import Queue
from threading import Thread, get_ident
import time


class ResidentWeatherWorker:
    def __init__(self, factory, retain=True):
        self.factory, self.retain = factory, retain
        self.queue = Queue(maxsize=1)
        self.ready = Future()
        self.closed = False
        self.events = []
        self.thread = Thread(target=self._run, name="resident-numerical-resources")
        self.thread.start()
        self.owner = self.ready.result(timeout=30)

    def _run(self):
        resources = None
        owner = get_ident()

        def open_resources():
            began = time.perf_counter()
            result = self.factory()
            self.events.append(
                dict(event="open", thread=owner, seconds=time.perf_counter() - began)
            )
            return result

        def close_resources(value):
            value.close()
            self.events.append(dict(event="close", thread=owner))

        try:
            if self.retain:
                resources = open_resources()
            self.ready.set_result(owner)
            while True:
                job = self.queue.get()
                if job is None:
                    return
                function, future = job
                try:
                    if not self.retain:
                        resources = open_resources()
                    self.events.append(dict(event="task", thread=owner))
                    future.set_result(function(resources))
                except Exception as exc:
                    future.set_exception(exc)
                finally:
                    if not self.retain and resources is not None:
                        close_resources(resources)
                        resources = None
        except Exception as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            else:
                self.events.append(dict(event="fatal", thread=owner, error=str(exc)))
        finally:
            if resources is not None:
                close_resources(resources)

    def call(self, function):
        if self.closed or not self.thread.is_alive():
            raise RuntimeError("Numerical worker closed")
        if get_ident() == self.owner:
            raise RuntimeError("Recursive numerical-worker call")
        future = Future()
        self.queue.put((function, future), timeout=5)
        return future.result(timeout=30)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.queue.put(None, timeout=5)
        self.thread.join(10)
        if self.thread.is_alive():
            raise RuntimeError("Numerical resource owner failed to stop")
