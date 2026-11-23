"""A crowd of units that do not answer must not cost the sweep, the host or the log what it used to.

Measured on the real server with 250 units that never answered (a mix of refused connections, TCP
that accepts and says nothing, a WebSocket upgrade that is never followed by a reply, and addresses
that drop every packet), before this change:

    sweep duration          14-16 s against a 10 s page interval (so a sweep was always running)
    threads / sockets       ~500 threads, ~2,300 sockets at peak
    log                     3.6 MB in 59 s, 6,700 warnings, 12,600 INFO lines -- all idle polling
    connects to quiet units 2,900 refused connects in 59 s, each costing about 2 s of a worker

Quiet units are now looked at on an exponential back-off with a cap, derived from the record that
already existed, so no persisted field changed. These tests assert the shape, at the transport that
opens the connection, and each one is proved by mutation (see the report).
"""
import logging
import threading
import time
import unittest

import _support
from _support import RealApp, SOURCE_A, SOURCE_B, DISPLAY_1, DISPLAY_2

import lumaserver as _server
import luma_liveness
from websocket._core import create_connection as REAL_CREATE_CONNECTION  # noqa: F401  (kept for parity)

REAL_WS_CALL_AUTH = _server._ws_call_auth
REAL_WS_CALL_SWITCHER = _server._ws_call_switcher_no_password


def quiet_unit(i, misses, age, role="decoder"):
    """A synthetic unit nothing like the reported bench: documentation addresses, invented hardware ids."""
    return {"mac": "02:77:%02x:%02x:00:01" % (i >> 8, i & 255), "ip": "203.0.113.%d" % (i + 1),
            "type": role, "hostname": "quiet-%03d" % i, "model": "TEST-D1" if role == "decoder" else "TEST-E1",
            "status": "idle",
            "reach": {"misses": misses, "ts": int(time.time() - age)}}


class Counting:
    """The transport boundary: every connection the polling code tries to open, counted by address."""

    def __init__(self, answer=None, delay=0.0):
        self.opened = []
        self.live = 0
        self.max_live = 0
        self.lock = threading.Lock()
        self.answer = answer or {}
        self.delay = delay

    def __call__(self, url, **kwargs):
        ip = url.split("//", 1)[1].split(":", 1)[0]
        with self.lock:
            self.opened.append(ip)
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        try:
            if self.delay:
                time.sleep(self.delay)
            raise ConnectionRefusedError()
        finally:
            with self.lock:
                self.live -= 1


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.transport = Counting()
        # The real call chain down to the connection opener; only the opener is replaced.
        self.app.patch("_ws_call_auth", REAL_WS_CALL_AUTH)
        self.app.patch("_ws_call_switcher_no_password", REAL_WS_CALL_SWITCHER)
        self.app.patch("create_connection", self.transport)
        self.app.patch("_persist_units_cache", lambda: None)

    def load(self, units):
        with self.m.cache_lock:
            self.m.cache_units.clear()
            for unit in units:
                self.m.cache_units[unit["ip"]] = dict(unit)

    def sweep(self, force=False):
        self.transport.opened.clear()
        return self.m._refresh_matrix_state_core(force=force)


class ScheduleTests(unittest.TestCase):
    def test_nothing_waits_until_the_miss_limit(self):
        for misses in range(luma_liveness.MISS_LIMIT):
            self.assertEqual(luma_liveness.backoff_seconds(misses), 0.0)

    def test_the_schedule_doubles_and_is_capped(self):
        steps = [luma_liveness.backoff_seconds(luma_liveness.MISS_LIMIT + k) for k in range(8)]
        self.assertEqual(steps[:4], [10.0, 20.0, 40.0, 80.0])
        self.assertEqual(max(steps), luma_liveness.BACKOFF_CAP)
        self.assertEqual(steps, sorted(steps))

    def test_junk_misses_never_raise(self):
        for junk in (None, "x", [], {}):
            self.assertEqual(luma_liveness.backoff_seconds(junk), 0.0)

    def test_due_follows_the_record(self):
        now = 10_000.0
        # misses=4 waits 40 s, stretched by a per-unit factor of 0.75-1.25: 30-50 s.
        quiet = {"mac": "02:00:00:00:00:01", "reach": {"misses": 4, "ts": now - 5}}
        self.assertFalse(luma_liveness.is_due(quiet, now=now))
        quiet["reach"]["ts"] = now - 60
        self.assertTrue(luma_liveness.is_due(quiet, now=now))
        quiet["reach"]["ts"] = now - 5
        self.assertTrue(luma_liveness.is_due(quiet, now=now, force=True))
        self.assertTrue(luma_liveness.is_due({"reach": {"misses": 1, "ts": now}}, now=now), "one miss is not gone")
        self.assertTrue(luma_liveness.is_due({"reach": {"misses": 0, "ts": now}}, now=now))
        self.assertTrue(luma_liveness.is_due({}, now=now), "never probed")
        self.assertTrue(luma_liveness.is_due({"reach": "junk"}, now=now))

    def test_the_jitter_is_bounded_deterministic_and_spreads_a_crowd(self):
        factors = [luma_liveness.jitter("02:77:00:%02x:00:01" % i) for i in range(200)]
        self.assertTrue(all(0.75 <= f < 1.25 for f in factors))
        self.assertEqual(luma_liveness.jitter("x"), luma_liveness.jitter("x"))
        self.assertGreater(len({round(f, 2) for f in factors}), 20, "units that went quiet together fall due together")

    def test_a_crowd_that_went_quiet_together_does_not_fall_due_in_one_sweep(self):
        now = 10_000.0
        crowd = [{"mac": "02:77:00:%02x:00:01" % i, "reach": {"misses": 7, "ts": now - 120}} for i in range(200)]
        due = sum(1 for u in crowd if luma_liveness.is_due(u, now=now))
        self.assertTrue(0 < due < 200, "all or none of the crowd fell due at the cap: %d of 200" % due)

    def test_a_clock_that_moved_back_does_not_silence_a_unit(self):
        now = 10_000.0
        self.assertTrue(luma_liveness.is_due({"reach": {"misses": 9, "ts": now + 3600}}, now=now))


class SweepTests(Base):
    def test_quiet_units_inside_their_back_off_are_not_connected_to(self):
        self.load([quiet_unit(i, misses=6, age=5) for i in range(60)])
        self.sweep()
        self.assertEqual(self.transport.opened, [], "a unit silent for a long time was asked on every sweep")

    def test_a_quiet_unit_is_looked_at_again_when_its_back_off_has_elapsed(self):
        self.load([quiet_unit(i, misses=3, age=40) for i in range(5)])      # waits 15-25 s, last look 40 s ago
        self.sweep()
        self.assertEqual(len(set(self.transport.opened)), 5)

    def test_the_wait_is_capped(self):
        unit = quiet_unit(1, misses=500, age=luma_liveness.BACKOFF_CAP * 1.25 + 5)
        self.load([unit])
        self.sweep()
        self.assertIn(unit["ip"], self.transport.opened)

    def test_a_unit_that_has_missed_one_read_is_still_asked_every_sweep(self):
        self.load([quiet_unit(1, misses=1, age=1), quiet_unit(2, misses=0, age=1)])
        self.sweep()
        self.assertEqual(len(set(self.transport.opened)), 2)

    def test_a_manual_refresh_probes_every_unit(self):
        self.load([quiet_unit(i, misses=9, age=1) for i in range(12)])
        self.sweep(force=True)
        self.assertEqual(len(set(self.transport.opened)), 12)

    def test_the_refresh_endpoint_forces_the_sweep_it_starts(self):
        self.load([quiet_unit(i, misses=9, age=1) for i in range(6)])
        self.app.patch("_observe_cache", __import__("luma_statecache").StateCache(ttl=0.0))
        client = self.m.APP.test_client()
        self.transport.opened.clear()
        client.post("/api/refresh_matrix_state", json={})
        self.assertEqual(self.transport.opened, [])
        client.post("/api/refresh_matrix_state", json={"force": True})
        self.assertEqual(len(set(self.transport.opened)), 6)
        # And the force is spent: the next ordinary sweep is back on the schedule.
        self.transport.opened.clear()
        client.post("/api/refresh_matrix_state", json={})
        self.assertEqual(self.transport.opened, [])

    def test_repeated_sweeps_do_not_grow_the_cost_of_a_silent_crowd(self):
        self.load([quiet_unit(i, misses=0, age=0) for i in range(40)])
        costs = [len(self.opened_by_sweep()) for _ in range(5)]
        self.assertGreater(costs[0], 0)
        self.assertEqual(costs[3:], [0, 0], "after the miss limit the crowd costs nothing: %r" % costs)

    def opened_by_sweep(self):
        self.sweep()
        # The record's timestamp is whole seconds, so the next look is not yet due: exactly what is asserted.
        return list(self.transport.opened)

    def test_an_answering_unit_among_the_quiet_is_still_polled_every_sweep(self):
        good = dict(SOURCE_A)
        good["reach"] = {"misses": 0, "ts": int(time.time())}
        self.load([good] + [quiet_unit(i, misses=8, age=1) for i in range(30)])
        self.sweep()
        self.assertIn(good["ip"], self.transport.opened)
        self.assertEqual(set(self.transport.opened), {good["ip"]})

    def test_concurrency_stays_bounded(self):
        self.transport.delay = 0.02
        self.load([quiet_unit(i, misses=0, age=0) for i in range(120)])
        self.sweep()
        self.assertLessEqual(self.transport.max_live, 32)
        self.assertGreater(len(self.transport.opened), 100)

    def test_a_unit_that_answers_again_is_marked_answering_at_once(self):
        unit = quiet_unit(1, misses=7, age=500)
        self.load([unit])

        def reply(ip, method, params=None, timeout=None):
            return {"result": {"streamname": "s"}}
        self.app.patch("_ws_call_auth", reply)
        self.app.patch("_ws_call_switcher_no_password", reply)
        self.sweep()
        with self.m.cache_lock:
            self.assertFalse(luma_liveness.is_offline(self.m.cache_units[unit["ip"]]))
            self.assertEqual(self.m.cache_units[unit["ip"]]["reach"]["misses"], 0)

    def test_the_persisted_record_keeps_its_shape(self):
        unit = quiet_unit(1, misses=0, age=0)
        self.load([unit])
        self.sweep()
        with self.m.cache_lock:
            record = self.m.cache_units[unit["ip"]]["reach"]
        self.assertEqual(set(record), {"misses", "ts"}, "the back-off must not add a persisted field")


class OrderingAndVerifyTests(Base):
    def test_units_that_answer_are_submitted_before_the_quiet_ones(self):
        submitted = []

        class Sequential:
            """Runs each job at once, in submission order, and records the order."""

            def __init__(self, max_workers=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def submit(self, fn, unit):
                submitted.append(unit["ip"])
                import concurrent.futures as cf
                future = cf.Future()
                future.set_result(fn(unit))
                return future
        self.app.patch("ThreadPoolExecutor", Sequential)
        quiet = [quiet_unit(i, misses=2, age=500) for i in range(5)]          # offline, but due again
        good = [quiet_unit(100 + i, misses=0, age=0) for i in range(5)]
        self.load(quiet + good)                                               # quiet ones first in the cache
        self.sweep()
        self.assertEqual(len(submitted), 10)
        answering = {u["ip"] for u in good}
        self.assertEqual(set(submitted[:5]), answering, "a crowd of quiet units was submitted ahead of the answering ones")

    def test_a_unit_that_stays_quiet_is_not_logged_again_on_every_sweep(self):
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record)
        handler = Handler(logging.INFO)
        logging.getLogger().addHandler(handler)
        previous = logging.getLogger().level
        logging.getLogger().setLevel(logging.INFO)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.addCleanup(logging.getLogger().setLevel, previous)
        self.load([quiet_unit(1, misses=6, age=500)])
        for _ in range(3):
            self.sweep()
        self.assertEqual([r for r in records if "[liveness]" in r.getMessage()], [])

    def test_a_unit_that_comes_back_is_logged_once(self):
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record)
        handler = Handler(logging.INFO)
        logging.getLogger().addHandler(handler)
        previous = logging.getLogger().level
        logging.getLogger().setLevel(logging.INFO)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.addCleanup(logging.getLogger().setLevel, previous)
        self.load([quiet_unit(1, misses=6, age=500)])
        reply = lambda ip, method, params=None, timeout=None: {"result": {"streamname": "s"}}
        self.app.patch("_ws_call_auth", reply)
        self.app.patch("_ws_call_switcher_no_password", reply)
        self.sweep()
        self.sweep()
        changes = [r.getMessage() for r in records if "[liveness]" in r.getMessage()]
        self.assertEqual(len(changes), 1, changes)
        self.assertIn("answering again", changes[0])

    def test_the_hostname_sweep_skips_quiet_units(self):
        asked = []
        self.app.patch("_ws_get_hostname", lambda ip, timeout: asked.append(ip) or "")
        self.app.patch("_ws_get_dante_info", lambda ip, timeout: {})
        self.app.patch("_ws_get_dante_power_status", lambda ip, timeout: None)
        self.app.patch("_ws_get_hdcp_encoder", lambda ip, timeout: "")
        self.app.patch("_ws_get_hdcp_decoder", lambda ip, timeout: "")
        quiet = [quiet_unit(i, misses=6, age=5) for i in range(5)]
        good = [quiet_unit(100 + i, misses=0, age=0) for i in range(3)]
        self.load(quiet + good)
        with self.m._cache_verify_lock:
            self.m._cache_verify_state.update(running=False, finished=-1e9)
        self.m.APP.test_client().get("/api/cache")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.m._cache_verify_lock:
                if not self.m._cache_verify_state["running"] and self.m._cache_verify_state["finished"] > 0:
                    break
            time.sleep(0.02)
        self.assertEqual(set(asked), {u["ip"] for u in good}, "the sweep asked a unit inside its back-off")


class LogTests(Base):
    def capture(self, level=logging.INFO):
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record)
        handler = Handler(level)
        logging.getLogger().addHandler(handler)
        previous = logging.getLogger().level
        logging.getLogger().setLevel(level)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.addCleanup(logging.getLogger().setLevel, previous)
        return records

    def test_an_unanswered_read_is_not_an_info_line(self):
        records = self.capture(logging.INFO)
        self.load([quiet_unit(i, misses=0, age=0) for i in range(10)])
        self.sweep()
        noisy = [r for r in records if r.levelno >= logging.INFO and
                 ("[audit]" in r.getMessage() or "Failed on all paths" in r.getMessage())]
        self.assertEqual(noisy, [], "idle polling wrote %d line(s) at INFO or above" % len(noisy))

    def test_a_mutation_is_still_audited_at_info(self):
        records = self.capture(logging.INFO)
        self.m._audit_device_request("203.0.113.9", "SelectVideoStream.Set", {"password": "x"}, "ok_via_primary")
        self.m._audit_device_request("203.0.113.9", "SelectVideoStream.Get", {}, "rejected_primary_and_fallback")
        self.assertEqual(len([r for r in records if "[audit]" in r.getMessage()]), 2)
        self.assertNotIn("x\"", " ".join(r.getMessage() for r in records), "the credential is still redacted")

    def test_a_failed_write_is_still_a_warning(self):
        records = self.capture(logging.WARNING)
        self.m._ws_call("203.0.113.9", {"id": "1", "method": "SelectVideoStream.Set"}, 0.2)
        self.assertTrue([r for r in records if "Failed on all paths" in r.getMessage()])

    def test_the_change_of_state_is_logged_once(self):
        records = self.capture(logging.INFO)
        self.load([quiet_unit(1, misses=1, age=0)])      # the next miss makes it offline
        self.sweep()
        changes = [r for r in records if "[liveness]" in r.getMessage()]
        self.assertEqual(len(changes), 1)
        self.assertIn("not answering", changes[0].getMessage())


class VideoWallReadTests(Base):
    def test_a_quiet_decoder_is_not_read_four_times_by_the_wall_page(self):
        unit = quiet_unit(1, misses=6, age=5)
        self.load([unit])
        reported = self.m._vw_read_decoder(self.m.cache_units[unit["ip"]])
        self.assertEqual(self.transport.opened, [])
        self.assertFalse(reported["online"])

    def test_a_decoder_that_has_not_missed_enough_is_still_read(self):
        unit = quiet_unit(1, misses=0, age=0)
        self.load([unit])
        self.m._vw_read_decoder(self.m.cache_units[unit["ip"]])
        self.assertTrue(self.transport.opened)


if __name__ == "__main__":
    unittest.main()
