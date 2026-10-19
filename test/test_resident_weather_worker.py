"""Resource open/use/close stays on one worker, without weakening calculations."""

from pathlib import Path
import sys
from threading import get_ident
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from resident_weather_worker import ResidentWeatherWorker


class TestResidentWorker(unittest.TestCase):
    def exercise(self, retain):
        events = []

        class Resource:
            def __init__(self):
                self.owner = get_ident()
                events.append(("open", self.owner))

            def value(self):
                self.assert_owner()
                events.append(("use", get_ident()))
                return 42

            def assert_owner(self):
                if self.owner != get_ident():
                    raise AssertionError("Resource crossed threads")

            def close(self):
                self.assert_owner()
                events.append(("close", get_ident()))

        worker = ResidentWeatherWorker(Resource, retain=retain)
        try:
            self.assertEqual(worker.call(lambda r: r.value()), 42)
            self.assertEqual(worker.call(lambda r: r.value()), 42)
            self.assertNotEqual(worker.owner, get_ident())
        finally:
            worker.close()
        self.assertEqual(len({thread for _, thread in events}), 1)
        self.assertEqual(
            sum(event == "open" for event, _ in events), 1 if retain else 2
        )
        self.assertEqual(
            sum(event == "close" for event, _ in events), 1 if retain else 2
        )
        self.assertFalse(worker.thread.is_alive())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            worker.call(lambda r: r.value())

    def test_resident_owner_opens_once_and_closes_on_owner(self):
        self.exercise(True)

    def test_reopen_control_has_identical_values_and_same_thread_rules(self):
        self.exercise(False)

    def test_job_failure_propagates_and_owner_can_close(self):
        class Resource:
            def close(self):
                pass

        worker = ResidentWeatherWorker(Resource)
        try:

            def fail(r):
                raise ValueError("resource rejected")

            with self.assertRaisesRegex(ValueError, "resource rejected"):
                worker.call(fail)
            self.assertEqual(worker.call(lambda r: 1), 1)
        finally:
            worker.close()
