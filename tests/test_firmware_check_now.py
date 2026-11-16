"""Settings > Firmware > Check Now: one explicit check through the lookup the schedule already uses.

Everything runs against a stubbed Hall Research transport. A check is asked of the server in the
background, never waited on by the request, never run twice at once, and a failed one leaves the last
good answer in place.
"""
import copy
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import _support
from _support import RealApp

import luma_access


class CheckNowTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.client = self.m.APP.test_client()
        self.addCleanup(self._wait_for_workers)
        # Put every module-level name this feature touches back afterwards.
        names = ("_hall_firmware_cache", "_hall_firmware_fetch_state", "_hall_firmware_fetched_at",
                 "_hall_firmware_last_attempt", "_hall_firmware_last_error", "_hall_firmware_fetch_started",
                 "_hall_check_thread")
        saved = {n: copy.deepcopy(getattr(self.m, n)) for n in names}

        def restore():
            for n, v in saved.items():
                if n == "_hall_firmware_cache":
                    self.m._hall_firmware_cache.clear()
                    self.m._hall_firmware_cache.update(v)
                else:
                    setattr(self.m, n, v)
        self.addCleanup(restore)
        # The scheduled lookup is not started here: only the explicit one is under test.
        self.m._hall_firmware_fetch_started = True
        self.state_dir = tempfile.mkdtemp(prefix="luma_fw_")
        patcher = mock.patch.object(self.m, "HALL_FIRMWARE_STATE_FILE", os.path.join(self.state_dir, "firmware_status.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wait_for_workers(self):
        worker = self.m._hall_check_thread
        if worker is not None:
            worker.join(10)
            self.assertFalse(worker.is_alive(), "a test started a check and left it running")

    def post(self):
        return self.client.post("/api/firmware_status/check", json={})

    def status(self):
        return self.client.get("/api/firmware_status").get_json()

    # ------------------------------------------------------------------------------ the route
    def test_it_is_an_administrator_route(self):
        self.assertEqual(luma_access.ROUTES["/api/firmware_status/check"], luma_access.Access.ADMIN)

    def test_one_press_is_one_check_and_the_request_does_not_wait_for_it(self):
        gate, fetches = threading.Event(), []

        def slow():
            fetches.append(1)
            gate.wait(5)
        with mock.patch.object(self.m, "_fetch_hall_firmware_catalog", slow):
            started = time.monotonic()
            r = self.post()
            self.assertEqual(r.status_code, 202)
            self.assertLess(time.monotonic() - started, 2, "the request answered while the check was still running")
            self.assertEqual(r.get_json()["started"], True)
            for _ in range(200):
                if fetches:
                    break
                time.sleep(0.01)
            self.assertEqual(fetches, [1])
            self.assertTrue(self.status()["hall_lookup"]["running"], "status says a check is running")
            # A second press while it runs is not a second check, and is still accepted.
            again = self.post()
            self.assertEqual(again.status_code, 202)
            self.assertEqual(again.get_json()["started"], False)
            self.assertEqual(fetches, [1], "never two at once")
            gate.set()
            self.m._hall_check_thread.join(5)
        self.assertFalse(self.status()["hall_lookup"]["running"])

    def test_the_schedule_does_not_ask_again_while_an_explicit_check_runs(self):
        fetches = []
        with mock.patch.object(self.m, "_fetch_hall_firmware_catalog", lambda: fetches.append(1)):
            self.m._hall_check_gate.acquire()
            try:
                self.assertFalse(self.m._run_hall_check_if_idle())
            finally:
                self.m._hall_check_gate.release()
            self.assertEqual(fetches, [], "found the gate held, so nothing was fetched")
            self.assertTrue(self.m._run_hall_check_if_idle())
            self.assertEqual(fetches, [1])
            self.assertFalse(self.m._hall_check_gate.locked(), "the gate is released on every path")

    def test_the_gate_is_released_when_the_fetch_raises(self):
        def boom():
            raise RuntimeError("down")
        with mock.patch.object(self.m, "_fetch_hall_firmware_catalog", boom):
            with self.assertRaises(RuntimeError):
                self.m._run_hall_check_if_idle()
        self.assertFalse(self.m._hall_check_gate.locked(), "a failed check does not disable the button for the life of the process")

    # --------------------------------------------------------------------- opening Settings asks nothing
    def test_reading_the_status_never_starts_a_check(self):
        fetches = []
        with mock.patch.object(self.m, "_fetch_hall_firmware_catalog", lambda: fetches.append(1)):
            for _ in range(3):
                self.assertEqual(self.client.get("/api/firmware_status").status_code, 200)
        self.assertEqual(fetches, [], "Settings reads local state only")
        worker = self.m._hall_check_thread
        self.assertTrue(worker is None or not worker.is_alive(), "no worker was started by reading")

    # ------------------------------------------------------------------------------------ a real result
    def test_a_failed_check_keeps_the_last_good_answer_and_says_it_is_stale(self):
        with self.m._hall_firmware_lock:
            self.m._hall_firmware_cache.clear()
            self.m._hall_firmware_cache["hw-luma-d4111"] = {"version": "1.0.2", "release_date": None, "checked_at": 1}
            self.m._hall_firmware_fetch_state = "ready"
            self.m._hall_firmware_fetched_at = time.time() - 60
        before = self.status()["hall_lookup"]
        with mock.patch.object(self.m.requests, "get", side_effect=OSError("no route")):
            self.assertEqual(self.post().status_code, 202)
            self.m._hall_check_thread.join(10)
        after = self.status()
        self.assertEqual(after["hall_lookup"]["state"], "unavailable")
        self.assertTrue(after["hall_lookup"]["stale"], "the answer on screen is marked as the last known one")
        self.assertIn("hw-luma-d4111", after["models"], "the remembered catalogue was not erased")
        self.assertGreater(after["hall_lookup"]["last_attempt"], before.get("last_attempt") or 0, "the attempt is recorded")
        self.assertEqual(after["hall_lookup"]["fetched_at"], before["fetched_at"], "Last checked still names the last success")

    def test_a_successful_check_updates_last_checked_and_the_catalogue(self):
        self.m._hall_firmware_fetched_at = None
        with mock.patch.object(self.m, "_fetch_hall_firmware_catalog") as fetch:
            def fake():
                with self.m._hall_firmware_lock:
                    self.m._hall_firmware_cache["hw-luma-d4111"] = {"version": "1.0.3", "release_date": None, "checked_at": 5}
                    self.m._hall_firmware_fetch_state = "ready"
                    self.m._hall_firmware_fetched_at = 1234.0
                    self.m._hall_firmware_last_attempt = 1234.0
            fetch.side_effect = fake
            self.assertEqual(self.post().status_code, 202)
            self.m._hall_check_thread.join(10)
        look = self.status()["hall_lookup"]
        self.assertEqual((look["state"], look["fetched_at"], look["running"]), ("ready", 1234.0, False))
        self.assertEqual(self.status()["models"]["hw-luma-d4111"]["version"], "1.0.3")

    def test_the_status_reply_names_no_internal_state_beyond_the_documented_fields(self):
        look = self.status()["hall_lookup"]
        self.assertEqual(sorted(look), ["error", "fetched_at", "interval_days", "last_attempt", "running", "stale", "state"])


if __name__ == "__main__":
    unittest.main()
