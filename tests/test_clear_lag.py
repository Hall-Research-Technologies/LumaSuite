"""Clear Units ends the work that was started for the inventory it cleared, and the log is bounded.

The lag a real browser showed (60 units, 12 encoders that never answered): Clear Units took 171 s to finish,
of which the server spent milliseconds. The rest was the page's own background reads queued ahead of it (see
tests/clear_lag_test.js). On the server side the same defect had a second face: a sweep that began before a
Clear Units went on asking every cleared unit about itself -- the matrix sweep for its whole pool, the
Device Manager's verification sweep one unit at a time, several reads each, for as long as the list was long.

These tests hold both halves at the transport that opens the connection. Fixtures are synthetic and
deep-copied: background work mutates cached device dicts in place.
"""
import copy
import io
import logging
import logging.handlers
import os
import tempfile
import threading
import time
import unittest

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, SOURCE_A, RealApp, RealRemote, identity)

import luma_users

import luma_logging


def quiet_unit(i, role="decoder", **extra):
    unit = {"mac": "02:77:%02x:%02x:00:01" % (i >> 8, i & 255), "ip": "203.0.113.%d" % (i % 250 + 1) if i < 250
            else "198.51.%d.%d" % (100 + i // 250, i % 250 + 1),
            "type": role, "hostname": "quiet-%03d" % i, "model": "TEST-D1", "status": "idle",
            "net": {"links": []}}
    unit.update(extra)
    return unit


class Opened:
    """The transport boundary: every connection the sweep tries to open, counted by address."""

    def __init__(self, delay=0.05):
        self.ips = []
        self.lock = threading.Lock()
        self.delay = delay
        self.on_open = None

    def __call__(self, url, **kwargs):
        ip = url.split("//", 1)[1].split(":", 1)[0]
        with self.lock:
            self.ips.append(ip)
            count = len(self.ips)
        if self.on_open:
            self.on_open(count)
        time.sleep(self.delay)
        raise ConnectionRefusedError()


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.transport = Opened()
        from lumaserver import _ws_call_auth as _unused  # noqa: F401  (the real chain is restored below)
        self.app.patch("_ws_call_auth", REAL_WS_CALL_AUTH)
        self.app.patch("create_connection", self.transport)

    def load(self, units):
        with self.m.cache_lock:
            self.m.cache_units.clear()
            for unit in copy.deepcopy(units):
                self.m.cache_units[unit["ip"]] = unit

    def clear_like_the_endpoint(self):
        """What Clear Units does to the inventory: a new generation and an empty cache."""
        with self.m.cache_lock:
            self.m.cache_generation += 1
            self.m.cache_units.clear()


import lumaserver as _server  # noqa: E402
REAL_WS_CALL_AUTH = _server._ws_call_auth


class MatrixSweepAfterClearTests(Base):
    def test_a_sweep_stops_reading_units_once_the_inventory_is_cleared(self):
        units = [quiet_unit(i) for i in range(160)]
        self.load(units)
        cleared = threading.Event()

        def after_some(count):
            if count == 5 and not cleared.is_set():
                cleared.set()
                self.clear_like_the_endpoint()
        self.transport.on_open = after_some
        sweep = threading.Thread(target=lambda: self.m._refresh_matrix_state_core(force=True))
        sweep.start()
        sweep.join(60)
        self.assertFalse(sweep.is_alive(), "the sweep did not finish")
        self.assertTrue(cleared.is_set(), "the clear never happened: the test did not measure what it claims")
        opened = len(self.transport.ips)
        # The units already being read when the clear landed finish; none queued behind them starts.
        self.assertLessEqual(opened, 5 + 32 + 2, "a sweep went on reading %d of 160 cleared units" % opened)

    def test_without_a_clear_the_same_sweep_reads_every_unit(self):
        """The control for the test above: the same load, no clear, and every unit is read."""
        self.load([quiet_unit(i) for i in range(60)])
        self.m._refresh_matrix_state_core(force=True)
        self.assertEqual(len(set(self.transport.ips)), 60)

    def test_a_cleared_inventory_is_not_repopulated_by_a_late_sweep(self):
        self.load([quiet_unit(i) for i in range(40)])
        self.transport.on_open = lambda count: self.clear_like_the_endpoint() if count == 2 else None
        self.m._refresh_matrix_state_core(force=True)
        with self.m.cache_lock:
            self.assertEqual(dict(self.m.cache_units), {})


class VerifySweepAfterClearTests(Base):
    def test_the_device_manager_sweep_stops_when_the_inventory_is_cleared(self):
        self.load([quiet_unit(i) for i in range(40)])
        asked = []
        gate = threading.Event()

        def hostname(ip, timeout):
            asked.append(ip)
            if len(asked) == 2 and not gate.is_set():
                gate.set()
                self.clear_like_the_endpoint()
            time.sleep(0.02)
            return ""
        self.app.patch("_ws_get_hostname", hostname)
        self.app.patch("_ws_get_hdcp_decoder", lambda ip, timeout: "")
        with self.m.cache_lock:
            self.m._cache_verify_state.update(running=False, finished=0.0)
        self.app.patch("_merge_disk_into_memory", lambda: 0)
        response = self.m.APP.test_client().get("/api/cache")
        self.assertEqual(response.status_code, 200)
        deadline = time.time() + 20
        while time.time() < deadline:
            with self.m._cache_verify_lock:
                if not self.m._cache_verify_state["running"]:
                    break
            time.sleep(0.05)
        else:
            self.fail("the verification sweep never finished (the test must not leave it running)")
        self.assertTrue(gate.is_set(), "the clear never happened: the test did not measure what it claims")
        self.assertLessEqual(len(asked), 3, "the sweep went on asking %d cleared units about themselves" % len(asked))


class DerivedStateTests(unittest.TestCase):
    """What the application remembers about cleared units, other than the inventory itself, goes with them."""

    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.store = self.remote.store
        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.admin = self.remote.sign_in("root", ADMIN_PASSWORD)
        self.addCleanup(lambda: self.m._save_video_wall_state({"walls": [], "ignored_candidates": []}))
        self.addCleanup(self.m._vw_invalidate_detect_cache)
        self.addCleanup(self.m._net_learn_attempts.clear)

    def remember_something(self):
        self.m._net_learn_attempts["198.51.100.99"] = 1.0
        self.m._VW_DETECT_CACHE["payload"] = {"candidates": ["describes the cleared units"]}
        self.m._VW_DETECT_CACHE["ts"] = time.time()

    def assert_forgotten(self):
        self.assertEqual(self.m._net_learn_attempts, {}, "the neighbour-learning times of cleared units were kept")
        self.assertIsNone(self.m._VW_DETECT_CACHE["payload"], "a Video Wall detection of cleared units was kept")

    def assert_kept(self):
        self.assertTrue(self.m._net_learn_attempts)
        self.assertIsNotNone(self.m._VW_DETECT_CACHE["payload"])

    def listener_off(self):
        listener = self.remote.remote.listener
        saved = (listener._server, listener._bound)
        listener._server, listener._bound = None, None
        self.addCleanup(lambda: setattr(listener, "_server", saved[0]) or setattr(listener, "_bound", saved[1]))

    def test_a_full_clear_forgets_them(self):
        self.listener_off()
        self.remember_something()
        response = self.m.APP.test_client().post("/api/clear_units", json={})
        self.assertEqual(response.get_json()["scope"], "all")
        self.assert_forgotten()

    def test_a_clear_while_remote_access_is_on_forgets_them_when_units_went(self):
        self.remember_something()
        response = self.remote.post(self.admin, "/api/clear_units", {})
        body = response.get_json()
        self.assertEqual(body["scope"], "unassigned")
        self.assertGreater(body["cleared"], 0)
        self.assert_forgotten()

    def test_a_clear_that_removes_nothing_leaves_them_alone(self):
        with self.m.cache_lock:
            held = [copy.deepcopy(u) for u in self.m.cache_units.values()]
        result = self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions(
            [identity(u) for u in held if u["type"] == "encoder"], [identity(u) for u in held if u["type"] != "encoder"], [], []))
        self.assertTrue(result["ok"], result)
        self.remember_something()
        body = self.remote.post(self.admin, "/api/clear_units", {}).get_json()
        self.assertEqual(body["cleared"], 0, body)
        self.assert_kept()


class VideoWallDetectionTests(Base):
    """The Device Manager badge reads every decoder, so it must not cost a silent crowd anything or hold the page.

    Measured before: 25 units that did not answer made /api/video_wall/candidates_summary take 24 s, because the
    rows it read had no liveness record (a quiet unit was never skipped), each silent unit was asked four times,
    and the request waited for all of it.
    """

    def setUp(self):
        super().setUp()
        self.transport.delay = 0.0
        # The Base transport refuses every connection; here the unit-level call is what is counted.
        self.calls = []
        self.lock = threading.Lock()
        self.reply = lambda ip, method: None

        def device(ip, method, params=None, timeout=None):
            with self.lock:
                self.calls.append((ip, method, timeout))
            return self.reply(ip, method)
        self.app.patch("_ws_call_auth", device)
        self.m._vw_forget_detect()
        self.addCleanup(self.m._vw_forget_detect)
        self.addCleanup(self.finish_detection)
        self.app.patch("_load_video_wall_state", lambda: {"walls": [], "ignored_candidates": []})

    def finish_detection(self):
        thread = self.m._vw_detect_thread
        if thread is not None:
            thread.join(30)
            self.assertFalse(thread.is_alive(), "a detection outlived its test")

    def quiet(self, count, **reach):
        record = {"reach": {"misses": 6, "ts": int(time.time() - 5)}} if reach is not None and reach.get("quiet") else {}
        return [quiet_unit(i, **record) for i in range(count)]

    def test_a_unit_known_to_be_quiet_is_not_read_at_all(self):
        self.load(self.quiet(40, quiet=True))
        payload = self.m._vw_run_detect()
        self.assertEqual(self.calls, [], "silent units already known to be quiet were read again")
        self.assertEqual(len(payload["decoders"]), 40)

    def test_without_the_record_the_same_units_are_read(self):
        """The control: the same crowd, never probed, is read (once each)."""
        self.load(self.quiet(10))
        self.m._vw_run_detect()
        self.assertEqual(len({c[0] for c in self.calls}), 10)

    def test_a_silent_unit_is_asked_once_not_four_times(self):
        self.load(self.quiet(12))
        self.m._vw_run_detect()
        self.assertEqual(len(self.calls), 12, "each silent unit should cost one read: %d reads" % len(self.calls))

    def test_a_unit_that_answers_with_an_error_is_still_read_in_full(self):
        self.reply = lambda ip, method: {"error": {"code": -32601, "message": "method not found"}}
        self.load(self.quiet(3))
        self.m._vw_run_detect()
        self.assertEqual(len(self.calls), 12, "a unit that replied is not silent; its other reads are still made")

    def test_every_read_asks_for_a_short_timeout(self):
        self.load(self.quiet(3))
        self.m._vw_run_detect()
        for _ip, _method, timeout in self.calls:
            self.assertTrue(timeout is not None and 0 < timeout <= 2.0, timeout)

    def test_the_refresh_path_keeps_its_own_timeout(self):
        self.load(self.quiet(1))
        self.m._vw_read_decoder(self.m.cache_units["203.0.113.1"])
        self.assertTrue(all(c[2] == self.m.CONFIG.get("TIMEOUT", 3.0) for c in self.calls), "only the detection asks for the short timeout")

    def test_a_crowd_of_silent_units_costs_one_wave_not_one_per_unit(self):
        self.load(self.quiet(100))
        started = time.time()
        self.reply = lambda ip, method: (time.sleep(0.05), None)[1]
        self.m._vw_run_detect()
        self.assertLess(time.time() - started, 3.0)
        self.assertEqual(len(self.calls), 100)

    # ---- the badge never holds the page
    def slow_detect(self):
        release = threading.Event()
        runs = []
        original = self.m._vw_run_detect

        def slow():
            runs.append(1)
            release.wait(30)
            return {"ok": True, "candidates": [{"needs_review": True, "candidate_status": "NEW"}], "review_count": 1,
                    "encoders": [], "decoders": []}
        self.app.patch("_vw_run_detect", slow)
        self.addCleanup(release.set)
        return release, runs

    def get_badge(self):
        started = time.time()
        body = self.m.APP.test_client().get("/api/video_wall/candidates_summary").get_json()
        return body, time.time() - started

    def test_the_badge_answers_within_its_wait_while_the_detection_is_still_running(self):
        self.app.patch("VW_BADGE_WAIT_SECONDS", 0.2)
        release, _runs = self.slow_detect()
        body, took = self.get_badge()
        self.assertLess(took, 2.0, "the badge waited for the detection")
        self.assertEqual((body["ok"], body["review_count"], body["by_status"]), (True, 0, {}))
        self.assertIs(body["stale"], True)
        self.assertIs(body["pending"], True, "nothing has been detected yet, and the count is not an answer")
        release.set()
        self.finish_detection()
        again, _ = self.get_badge()
        self.assertEqual(again["review_count"], 1)
        self.assertNotIn("stale", again)

    def test_with_a_last_good_answer_the_badge_serves_it_marked_stale(self):
        self.app.patch("VW_BADGE_WAIT_SECONDS", 0.2)
        release, _runs = self.slow_detect()
        good = {"ok": True, "candidates": [{"needs_review": True, "candidate_status": "CONFLICT"}], "review_count": 3,
                "encoders": [], "decoders": []}
        with self.m._vw_detect_cache_lock:
            self.m._VW_DETECT_LAST.update(ts=time.time() - 60, payload=good)
        body, took = self.get_badge()
        self.assertLess(took, 2.0)
        self.assertEqual((body["review_count"], body["by_status"], body["stale"], body["pending"]), (3, {"CONFLICT": 1}, True, False))
        release.set()

    def test_pages_arriving_together_share_one_detection(self):
        self.app.patch("VW_BADGE_WAIT_SECONDS", 0.3)
        release, runs = self.slow_detect()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.get_badge())) for _ in range(5)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        self.assertEqual(len(runs), 1, "five callers started %d detections" % len(runs))
        release.set()
        self.finish_detection()
        full = self.m._vw_cached_detect()
        self.assertEqual(full["review_count"], 1)
        self.assertEqual(len(runs), 1, "the finished detection is cached for the page that wants all of it")

    def test_a_clear_while_a_detection_runs_discards_its_result(self):
        self.app.patch("VW_BADGE_WAIT_SECONDS", 0.1)
        release, _runs = self.slow_detect()
        self.get_badge()
        self.m._vw_forget_detect()                      # what Clear Units does
        release.set()
        self.finish_detection()
        self.assertIsNone(self.m._VW_DETECT_CACHE["payload"], "a detection of the cleared inventory was cached")
        self.assertIsNone(self.m._VW_DETECT_LAST["payload"], "and kept as the last good one")

    def test_clear_units_forgets_the_last_good_detection_too(self):
        with self.m._vw_detect_cache_lock:
            self.m._VW_DETECT_LAST.update(ts=time.time(), payload={"candidates": [], "review_count": 9})
        self.m._drop_inventory_derived_state()
        self.assertIsNone(self.m._VW_DETECT_LAST["payload"])

    def test_the_page_that_shows_the_walls_still_gets_the_whole_answer(self):
        self.load(self.quiet(3))
        payload = self.m._vw_cached_detect()
        self.assertEqual(len(payload["decoders"]), 3)
        self.assertIn("candidates", payload)

    def test_a_failing_detection_is_reported_to_the_page_that_waited_for_it(self):
        def broken():
            raise RuntimeError("boom")
        self.app.patch("_vw_run_detect", broken)
        with self.assertRaises(RuntimeError):
            self.m._vw_cached_detect()
        self.finish_detection()
        self.assertIsNone(self.m._vw_detect_run, "a failed detection left the single-flight claimed")


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(self.cleanup)
        self.root = logging.getLogger()
        self.saved = (self.root.level, list(self.root.handlers), list(self.root.filters))
        self.addCleanup(self.restore_root)

    def restore_root(self):
        for handler in list(self.root.handlers):
            if handler not in self.saved[1]:
                self.root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        self.root.handlers[:] = self.saved[1]
        self.root.setLevel(self.saved[0])

    def cleanup(self):
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_the_default_is_info_not_debug(self):
        info = luma_logging.configure(self.folder, env={}, stream=io.StringIO())
        self.assertEqual(info["level"], "INFO")
        self.assertEqual(self.root.level, logging.INFO)
        logging.getLogger("t").debug("detail nobody asked for")
        logging.getLogger("t").info("what happened")
        for h in self.root.handlers:
            h.flush()
        text = open(info["file"], encoding="utf-8").read()
        self.assertIn("what happened", text)
        self.assertNotIn("detail nobody asked for", text)

    def test_debug_is_available_when_asked_for(self):
        info = luma_logging.configure(self.folder, env={"LUMA_LOG_LEVEL": "debug"}, stream=io.StringIO())
        self.assertEqual(info["level"], "DEBUG")
        logging.getLogger("t").debug("the detail")
        for h in self.root.handlers:
            h.flush()
        self.assertIn("the detail", open(info["file"], encoding="utf-8").read())

    def test_an_unrecognised_level_is_the_default_never_an_error(self):
        for junk in ("loud", "", None, "10", "  ", "INFO; rm"):
            self.assertEqual(luma_logging.level_from(junk), logging.INFO, repr(junk))
        self.assertEqual(luma_logging.level_from("warning"), logging.WARNING)

    def test_warnings_and_errors_are_never_filtered_out(self):
        info = luma_logging.configure(self.folder, env={"LUMA_LOG_LEVEL": "ERROR"}, stream=io.StringIO())
        logging.getLogger("t").warning("a warning")
        logging.getLogger("t").error("an error")
        for h in self.root.handlers:
            h.flush()
        text = open(info["file"], encoding="utf-8").read()
        self.assertIn("an error", text)
        default = luma_logging.configure(self.folder, env={}, stream=io.StringIO())
        logging.getLogger("t").warning("kept warning")
        for h in self.root.handlers:
            h.flush()
        self.assertIn("kept warning", open(default["file"], encoding="utf-8").read())

    def test_the_file_is_bounded_however_much_is_logged(self):
        luma_logging.configure(self.folder, env={}, stream=io.StringIO())
        file_handlers = [h for h in self.root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(file_handlers), 1)
        handler = file_handlers[0]
        self.assertEqual(handler.maxBytes, luma_logging.MAX_BYTES)
        self.assertEqual(handler.backupCount, luma_logging.BACKUPS)
        # Rotation, measured: shrink the limit so the test is quick, and write far more than all files can hold.
        handler.maxBytes = 20000
        line = "x" * 200
        for _ in range(2000):                          # 400 KB against a 20 KB file and 3 backups
            logging.getLogger("t").info(line)
        handler.flush()
        folder = os.path.dirname(handler.baseFilename)
        files = sorted(os.listdir(folder))
        self.assertLessEqual(len(files), luma_logging.BACKUPS + 1, files)
        total = sum(os.path.getsize(os.path.join(folder, f)) for f in files)
        self.assertLessEqual(total, (luma_logging.BACKUPS + 1) * (20000 + 400), total)

    def test_the_log_file_can_be_switched_off_and_its_failure_does_not_stop_the_application(self):
        info = luma_logging.configure(self.folder, env={"LUMA_LOG_FILE": "0"}, stream=io.StringIO())
        self.assertIsNone(info["file"])
        self.assertFalse(os.path.exists(os.path.join(self.folder, "logs")))
        blocker = os.path.join(self.folder, "blocked")
        open(blocker, "w").close()
        info = luma_logging.configure(blocker, env={}, stream=io.StringIO())     # "logs" cannot be made under a file
        self.assertIsNone(info["file"])
        logging.getLogger("t").error("still logged to the stream")

    def test_no_stderr_and_no_file_is_still_a_working_logger(self):
        import sys
        saved = sys.stderr
        sys.stderr = None                     # a windowed program
        try:
            info = luma_logging.configure(self.folder, env={"LUMA_LOG_FILE": "0"})
            logging.getLogger("t").error("nowhere to go")
            self.assertEqual(info["level"], "INFO")
        finally:
            sys.stderr = saved

    def test_the_application_configures_it_and_nothing_sets_debug_any_more(self):
        import re
        source = open(os.path.join(_support.ROOT, "lumaserver.py"), encoding="utf-8").read()
        code = re.sub(r"(?m)^\s*#.*$", "", source)
        self.assertIn("luma_logging.configure(DATA_DIR)", code)
        self.assertNotRegex(code, r"logging\.basicConfig\([^)]*DEBUG")
        self.assertNotRegex(code, r"setLevel\(\s*logging\.DEBUG")

    def test_the_request_log_redaction_still_covers_the_new_handlers(self):
        import luma_request
        luma_logging.configure(self.folder, env={}, stream=io.StringIO())
        luma_request.install_token_redaction()
        for handler in self.root.handlers:
            self.assertTrue(any(isinstance(f, luma_request.RedactDirectAccessToken) for f in handler.filters),
                            "a handler without the redaction filter would write a bearer link to disk")


if __name__ == "__main__":
    unittest.main()
