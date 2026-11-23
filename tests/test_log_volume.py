"""What the application logs at steady state, and what it must never stop logging.

The rotating log (luma_logging) held about eight minutes of history with a polling page open: ~58 MB/h,
85% of it werkzeug's per-request access lines, the rest per-read success lines and a liveness line. Steady
state is not an event. These tests hold the quiet default, the DEBUG override, that failures and
mutations stay at INFO or above, and that the liveness line is written on the transition only.
"""
import io
import json
import logging
import logging.handlers
import shutil
import tempfile
import unittest

import _support
from _support import RealApp

import luma_logging

import lumaserver as _server  # noqa: E402
REAL_WS_CALL_AUTH = _server._ws_call_auth
WERKZEUG = logging.getLogger("werkzeug")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, minimum=logging.INFO):
        return [r.getMessage() for r in self.records if r.levelno >= minimum]


class WerkzeugLevelTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.root = logging.getLogger()
        saved = (self.root.level, list(self.root.handlers), WERKZEUG.level)

        def restore():
            for h in list(self.root.handlers):
                if h not in saved[1]:
                    self.root.removeHandler(h)
                    h.close()
            self.root.handlers[:] = saved[1]
            self.root.setLevel(saved[0])
            WERKZEUG.setLevel(saved[2])
            shutil.rmtree(self.folder, ignore_errors=True)
        self.addCleanup(restore)

    def read(self, info):
        for h in self.root.handlers:
            h.flush()
        with open(info["file"], encoding="utf-8") as handle:
            return handle.read()

    def test_the_access_logger_is_quiet_by_default(self):
        info = luma_logging.configure(self.folder, env={}, stream=io.StringIO())
        self.assertEqual(WERKZEUG.getEffectiveLevel(), logging.WARNING)
        WERKZEUG.info('127.0.0.1 - - "POST /api/tcp_probe HTTP/1.1" 200 -')
        WERKZEUG.warning("a real warning from the server")
        text = self.read(info)
        self.assertNotIn("tcp_probe", text)
        self.assertIn("a real warning from the server", text)

    def test_debug_restores_the_access_lines(self):
        info = luma_logging.configure(self.folder, env={"LUMA_LOG_LEVEL": "DEBUG"}, stream=io.StringIO())
        WERKZEUG.info('"GET /api/heartbeat HTTP/1.1" 200 -')
        self.assertIn("/api/heartbeat", self.read(info))

    def test_asking_for_info_does_not_bring_the_access_lines_back(self):
        luma_logging.configure(self.folder, env={"LUMA_LOG_LEVEL": "INFO"}, stream=io.StringIO())
        self.assertEqual(WERKZEUG.getEffectiveLevel(), logging.WARNING)

    def test_a_stricter_level_is_honoured_for_the_access_logger_too(self):
        luma_logging.configure(self.folder, env={"LUMA_LOG_LEVEL": "ERROR"}, stream=io.StringIO())
        self.assertEqual(WERKZEUG.getEffectiveLevel(), logging.ERROR)

    def test_application_info_lines_are_untouched(self):
        info = luma_logging.configure(self.folder, env={}, stream=io.StringIO())
        logging.info("[audit] route changed")
        logging.warning("[ws_call] ID mismatch")
        logging.error("authentication failed")
        text = self.read(info)
        for needle in ("route changed", "ID mismatch", "authentication failed"):
            self.assertIn(needle, text)


class FakeSocket:
    def __init__(self, answer):
        self.answer = answer
        self.sent = None

    def settimeout(self, t):
        pass

    def send(self, text):
        self.sent = json.loads(text)

    def recv(self):
        if self.answer == "ok":
            return json.dumps({"id": self.sent["id"], "result": {"hostname": "x"}})
        return json.dumps({"id": self.sent["id"], "error": {"code": -1, "message": "refused by the unit"}})

    def close(self):
        pass


class CaptureCase(unittest.TestCase):
    level = logging.DEBUG

    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.capture = Capture()
        root = logging.getLogger()
        before = root.level
        root.addHandler(self.capture)
        root.setLevel(self.level)

        def restore():
            root.removeHandler(self.capture)
            root.setLevel(before)
        self.addCleanup(restore)


class ReadLineLevelTests(CaptureCase):
    def call(self, answer):
        self.app.patch("create_connection", lambda url, **kw: FakeSocket(answer))
        return self.m._ws_call("203.0.113.9", {"id": "r1", "method": "System.Get", "params": {}}, 0.5)

    def test_a_successful_read_is_debug_not_info(self):
        self.assertIn("result", self.call("ok"))
        self.assertEqual(self.capture.messages(logging.INFO), [])
        self.assertTrue([r for r in self.capture.records if "Got matching response" in r.getMessage()
                         and r.levelno == logging.DEBUG], "the success line is no longer written even at DEBUG")

    def test_a_refusal_by_the_unit_is_still_a_warning(self):
        self.call("error")
        self.assertTrue([r for r in self.capture.records if "refused by the unit" in r.getMessage()
                         and r.levelno >= logging.WARNING])

    def test_the_minimal_scan_read_logs_success_at_debug_and_failure_at_info(self):
        self.app.patch("_ws_call_auth", lambda *a, **k: {"result": {}})
        try:
            self.m._ws_get_minimal("203.0.113.9")
        except Exception:
            pass
        self.assertEqual([m for m in self.capture.messages(logging.INFO) if "WS_GET_MINIMAL" in m], [])
        self.capture.records.clear()
        self.app.patch("_ws_call_auth", lambda *a, **k: None)
        try:
            self.m._ws_get_minimal("203.0.113.9")
        except Exception:
            pass
        self.assertTrue([m for m in self.capture.messages(logging.INFO) if "WS_GET_MINIMAL" in m and "ERROR" in m],
                        "a failed scan read is no longer logged at INFO")


class LivenessLineTests(CaptureCase):
    """The unit that goes quiet is reported once, and again only when it answers or is lost afresh."""
    level = logging.INFO

    def setUp(self):
        super().setUp()
        self.state = {"answer": None}

        def connect(url, **kw):
            if self.state["answer"] is None:
                raise ConnectionRefusedError()
            return FakeSocket(self.state["answer"])
        self.app.patch("_ws_call_auth", REAL_WS_CALL_AUTH)
        self.app.patch("create_connection", connect)
        with self.m.cache_lock:
            self.m.cache_units.clear()
            self.m.cache_units["203.0.113.50"] = {
                "mac": "02:88:00:00:00:01", "ip": "203.0.113.50", "type": "decoder", "hostname": "synthetic-1",
                "model": "TEST-D1", "status": "idle", "net": {"links": []}}

    def sweep(self):
        self.capture.records.clear()
        self.m._refresh_matrix_state_core(force=True)
        return [m for m in self.capture.messages(logging.INFO) if "[liveness]" in m]

    def test_the_line_is_written_on_the_transition_only(self):
        seen = [self.sweep() for _ in range(6)]
        self.assertEqual([len(s) for s in seen], [0, 1, 0, 0, 0, 0], seen)
        self.assertIn("not answering", seen[1][0])

    def test_recovery_is_written_once_and_a_second_loss_again(self):
        for _ in range(4):
            self.sweep()
        self.state["answer"] = "ok"
        recovered = [self.sweep() for _ in range(3)]
        self.state["answer"] = None
        lost = [self.sweep() for _ in range(4)]
        self.assertEqual([len(s) for s in recovered], [1, 0, 0], recovered)
        self.assertIn("answering again", recovered[0][0])
        self.assertEqual([len(s) for s in lost], [0, 1, 0, 0], lost)


if __name__ == "__main__":
    unittest.main()
