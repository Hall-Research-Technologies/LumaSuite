"""A page read must not wait on a unit that does not answer.

Measured on the packaged build: a Matrix or State read took 3.5 s whenever any unit was
unreachable, a device refresh took 7.0 s, and a scan call to one silent unit took 15 s. The
cause was one rule applied in two places: `_ws_call` tried all five WebSocket paths even
when the first one never answered (a unit that is silent on one path is silent on all), and
the page read waited for the whole device sweep, which it did not even use.

These tests use real sockets on loopback -- which the fence allows -- so a unit that accepts a
connection and never replies is a genuine hang, not a stub that raises quickly. Nothing leaves
the machine. The upper bounds are generous multiples of the timeouts they check, so a slow
machine does not fail them; what they pin is the *shape* (one attempt, not five).
"""
import socket
import threading
import time
import unittest

import _support
from _support import RealApp, System, SOURCE_A, DISPLAY_1, USER_PASSWORD, identity

import luma_statecache
from websocket._core import create_connection as REAL_CREATE_CONNECTION
from websocket import WebSocketBadStatusException


class HangingUnit:
    """A TCP listener that accepts every connection and never answers."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted.append(conn)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for conn in self.accepted:
            try:
                conn.close()
            except OSError:
                pass


class WsCallSilenceTests(unittest.TestCase):
    TIMEOUT = 0.3

    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        # The fence replaces the WebSocket opener with one that always refuses, loopback included.
        # These tests need the real opener; the sockets beneath it are still fenced to loopback.
        self.app.patch("create_connection", REAL_CREATE_CONNECTION)

    def test_a_unit_that_never_answers_is_asked_once_not_once_per_path(self):
        unit = HangingUnit()
        self.addCleanup(unit.close)
        self.app.patch("_ws_url", lambda ip, path: "ws://127.0.0.1:%d%s" % (unit.port, path))
        paths = len(self.m.CONFIG["ws_paths"])
        self.assertGreaterEqual(paths, 4, "the premise: there are several paths to try")
        start = time.perf_counter()
        result = self.m._ws_call("198.51.100.5", {"id": "1", "method": "X.Get"}, self.TIMEOUT)
        elapsed = time.perf_counter() - start
        self.assertIsNone(result)
        self.assertEqual(len(unit.accepted), 1, "one attempt: a silent unit is silent on every path")
        self.assertLess(elapsed, self.TIMEOUT * 3, "bounded by the timeout, not by timeout x paths (%.2fs)" % elapsed)

    def test_a_refused_connection_ends_the_call(self):
        calls = []

        def refuse(url, **kwargs):
            calls.append(url)
            raise ConnectionRefusedError()
        self.app.patch("create_connection", refuse)
        self.assertIsNone(self.m._ws_call("198.51.100.6", {"id": "1", "method": "X.Get"}, self.TIMEOUT))
        self.assertEqual(len(calls), 1)

    def test_a_connect_timeout_ends_the_call(self):
        calls = []

        def stall(url, **kwargs):
            calls.append(url)
            raise socket.timeout()
        self.app.patch("create_connection", stall)
        self.assertIsNone(self.m._ws_call("198.51.100.6", {"id": "1", "method": "X.Get"}, self.TIMEOUT))
        self.assertEqual(len(calls), 1)

    def test_a_path_the_unit_rejects_still_moves_on_to_the_next(self):
        """Only silence ends the call. A reply that says 'not here' is how the right path is found."""
        calls = []

        class Socket:
            def settimeout(self, _t):
                pass

            def send(self, _payload):
                pass

            def recv(self):
                return '{"id": "1", "result": {"ok": true}}'

            def close(self):
                pass

        def reject_twice(url, **kwargs):
            calls.append(url)
            if len(calls) <= 2:
                raise WebSocketBadStatusException("Handshake status %d", 404, "Not Found")
            return Socket()
        self.app.patch("create_connection", reject_twice)
        answer = self.m._ws_call("198.51.100.7", {"id": "1", "method": "X.Get"}, self.TIMEOUT)
        self.assertEqual(answer, {"id": "1", "result": {"ok": True}})
        self.assertEqual(len(calls), 3)

    def test_the_whole_call_has_a_deadline_even_when_every_path_rejects_slowly(self):
        calls = []

        def slow_rejection(url, **kwargs):
            calls.append(url)
            time.sleep(self.TIMEOUT)
            raise WebSocketBadStatusException("Handshake status %d", 404, "Not Found")
        self.app.patch("create_connection", slow_rejection)
        start = time.perf_counter()
        self.assertIsNone(self.m._ws_call("198.51.100.8", {"id": "1", "method": "X.Get"}, self.TIMEOUT))
        elapsed = time.perf_counter() - start
        self.assertLess(len(calls), len(self.m.CONFIG["ws_paths"]))
        # Four sleeps cost 4 x TIMEOUT before any scheduling overhead; five paths would cost 5 x. The
        # deadline is proved by the call count above; this bound only has to stay below the full
        # five-path cost, so it allows 0.5 x TIMEOUT of runner jitter instead of none.
        self.assertLess(elapsed, self.TIMEOUT * 4.5)


class RefreshCostTests(unittest.TestCase):
    """What an unreachable unit costs the device refresh."""

    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.calls = []

    def _poll(self, replies):
        def fake(ip, method, params=None, timeout=None):
            self.calls.append((ip, method))
            return replies.get(ip)
        self.app.patch("_ws_call_auth", fake)

    def test_an_encoder_that_gave_no_reply_is_not_asked_a_second_question(self):
        self.app.seed([dict(SOURCE_A, ip="198.51.100.30", mac="02:00:5e:77:00:01")])
        self._poll({})
        self.m._refresh_matrix_state_core()
        self.assertEqual(self.calls, [("198.51.100.30", "VideoMultiIpStream.Get")])

    def test_an_encoder_that_replied_with_an_error_is_still_asked_for_its_audio(self):
        """A reply that is an error is a unit that answered; only silence skips the second read."""
        self.app.seed([dict(SOURCE_A, ip="198.51.100.31", mac="02:00:5e:77:00:02")])
        self._poll({"198.51.100.31": {"error": {"message": "nope"}}})
        self.m._refresh_matrix_state_core()
        self.assertEqual([c[1] for c in self.calls], ["VideoMultiIpStream.Get", "AudioMultiIpStream.Get"])


class BoundedObservationTests(unittest.TestCase):
    def test_a_slow_refresh_does_not_hold_the_caller(self):
        release = threading.Event()
        cache = luma_statecache.StateCache(ttl=5)
        self.addCleanup(cache.wait_idle, 5)
        self.addCleanup(release.set)
        start = time.perf_counter()
        info = cache.get_bounded("k", lambda: release.wait(10), wait=0.2)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.6)
        self.assertFalse(info["fresh"])
        self.assertTrue(info["refreshing"])

    def test_the_answer_is_kept_for_the_next_caller_when_the_refresh_finishes(self):
        release = threading.Event()
        cache = luma_statecache.StateCache(ttl=5)
        cache.get_bounded("k", lambda: release.wait(10), wait=0.05)
        release.set()
        self.assertTrue(cache.wait_idle(5))
        start = time.perf_counter()
        info = cache.get_bounded("k", lambda: self.fail("a fresh answer must not be recomputed"), wait=0.2)
        self.assertLess(time.perf_counter() - start, 0.15)
        self.assertTrue(info["fresh"])
        self.assertFalse(info["refreshing"])
        self.assertIsNotNone(info["age_s"])

    def test_twelve_concurrent_readers_cost_one_refresh(self):
        runs = []
        release = threading.Event()

        def producer():
            runs.append(1)
            release.wait(5)
        cache = luma_statecache.StateCache(ttl=5)
        self.addCleanup(cache.wait_idle, 5)
        self.addCleanup(release.set)
        results = []
        readers = [threading.Thread(target=lambda: results.append(cache.get_bounded("k", producer, wait=0.15)))
                   for _ in range(12)]
        for reader in readers:
            reader.start()
        for reader in readers:
            reader.join(5)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(results), 12)

    def test_a_failed_refresh_never_reaches_the_caller_and_is_tried_again(self):
        attempts = []

        def broken():
            attempts.append(1)
            raise RuntimeError("device sweep exploded")
        cache = luma_statecache.StateCache(ttl=5)
        info = cache.get_bounded("k", broken, wait=0.5)
        self.assertTrue(cache.wait_idle(5))
        self.assertFalse(info["fresh"])
        cache.get_bounded("k", broken, wait=0.5)
        cache.wait_idle(5)
        self.assertEqual(len(attempts), 2, "a failure is not cached as an answer")

    def test_a_refresh_that_started_before_an_invalidation_is_not_stored(self):
        """A read in flight when a write lands describes a world that no longer exists."""
        release = threading.Event()
        cache = luma_statecache.StateCache(ttl=5)
        cache.get_bounded("k", lambda: release.wait(5), wait=0.05)
        cache.invalidate("k")
        release.set()
        self.assertTrue(cache.wait_idle(5))
        self.assertEqual(cache.stats()["discarded"], 1)
        again = []
        info = cache.get_bounded("k", lambda: again.append(1), wait=1.0)
        self.assertTrue(cache.wait_idle(5))
        self.assertEqual(again, [1])
        self.assertTrue(info["fresh"])


class ServerObservationTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.release = threading.Event()
        cache = luma_statecache.StateCache(ttl=5)
        self.app.patch("_observe_cache", cache)
        self.addCleanup(cache.wait_idle, 5)
        self.addCleanup(self.release.set)

    def test_the_page_read_never_waits_for_a_hung_sweep_longer_than_the_bound(self):
        self.app.patch("_refresh_matrix_state_core", lambda: self.release.wait(10))
        self.app.patch("OBSERVE_WAIT_SECONDS", 0.2)
        start = time.perf_counter()
        info = self.m._observe_matrix_state()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, "served from what was last read (%.2fs)" % elapsed)
        self.assertEqual((info["fresh"], info["refreshing"]), (False, True))

    def test_the_wait_is_a_fixed_small_bound_not_a_setting_that_can_be_left_open(self):
        self.assertLessEqual(self.m.OBSERVE_WAIT_SECONDS, 1.0)


class ObservedBodyTests(unittest.TestCase):
    """What the page is told about how old what it was shown is."""

    class AgedSystem(System):
        def _observe(self):
            self.observed += 1
            return {"fresh": False, "age_s": 7.0, "refreshing": True}

    def test_both_page_reads_carry_how_old_the_state_is(self):
        s = self.AgedSystem(units=[SOURCE_A, DISPLAY_1])
        self.addCleanup(s.close)
        s.turn_remote_on()
        s.admin("root")
        s.user("alex", sources=[identity(SOURCE_A)], destinations=[identity(DISPLAY_1)],
               standard=True, matrix=True)
        client = s.sign_in("alex", USER_PASSWORD)
        for path in ("/api/user/state", "/api/user/matrix"):
            body = client.get(path).get_json()
            self.assertEqual(body["observed"], {"fresh": False, "age_s": 7.0, "refreshing": True}, path)

    def test_a_reader_that_reports_nothing_adds_no_field(self):
        s = System(units=[SOURCE_A, DISPLAY_1])
        self.addCleanup(s.close)
        s.turn_remote_on()
        s.admin("root")
        s.user("alex", sources=[identity(SOURCE_A)], destinations=[identity(DISPLAY_1)])
        client = s.sign_in("alex", USER_PASSWORD)
        self.assertNotIn("observed", client.get("/api/user/state").get_json())


if __name__ == "__main__":
    unittest.main()
