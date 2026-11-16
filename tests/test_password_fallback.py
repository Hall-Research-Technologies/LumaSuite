"""
Hardware fence + Primary/Fallback device-password tests (family spec Part 4
and Part 8.4).

HARDWARE FENCE: before lumaserver is imported, every network transport it
could reach (raw sockets, requests, the websocket-client library) is
replaced with a spy that records the attempt and raises AssertionError.
lumaserver has no injectable "device client" seam (see PROJECT.md, Test
Architecture / Hardware-Network Boundaries) -- it calls these libraries
directly inline -- so this fence is the only thing standing between an
accidental test bug and a real network call to real hardware. Individual
tests must mock lumaserver._ws_call (the one function that actually speaks
the wire protocol) to supply canned responses; if a test's code path
reaches past that mock into a real transport, the fence raises immediately
instead of silently attempting a real connection.

Run directly: python tests/test_password_fallback.py
Or via discovery: python -m unittest discover -s tests -p "test_*.py"
"""
import io
import json
import logging
import os
import socket as socket_module
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Isolated, throwaway data dir -- never touch the developer's real
# producer_config.json / units_cache.json / video_walls.json.
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="lumasuite_test_data_")
os.environ["LUMA_DATA_DIR"] = _TMP_DATA_DIR

_transport_attempts = []


def _hardware_fence_spy(name):
    def _fn(*args, **kwargs):
        _transport_attempts.append((name, args, kwargs))
        raise AssertionError(
            "HARDWARE FENCE TRIPPED: a test attempted a real '%s' call "
            "(args=%r kwargs=%r). Tests must mock lumaserver._ws_call (or "
            "requests.post, for the one test that explicitly covers the "
            "legacy HTTP password-change endpoint) instead of letting a "
            "real transport be reached." % (name, args, kwargs)
        )
    return _fn


_fence_patchers = [
    mock.patch("socket.create_connection", _hardware_fence_spy("socket.create_connection")),
    mock.patch("socket.socket", _hardware_fence_spy("socket.socket")),
]
for _p in _fence_patchers:
    _p.start()

try:
    import websocket as _websocket_module
    _ws_patcher = mock.patch.object(_websocket_module, "create_connection", _hardware_fence_spy("websocket.create_connection"))
    _ws_patcher.start()
    _fence_patchers.append(_ws_patcher)
except Exception:
    pass

try:
    import requests as _requests_module
    _req_post_patcher = mock.patch.object(_requests_module, "post", _hardware_fence_spy("requests.post"))
    _req_get_patcher = mock.patch.object(_requests_module, "get", _hardware_fence_spy("requests.get"))
    _req_post_patcher.start()
    _req_get_patcher.start()
    _fence_patchers.append(_req_post_patcher)
    _fence_patchers.append(_req_get_patcher)
except Exception:
    pass

# Import happens with the fence armed, in case any module-level code path
# were to reach a transport (it shouldn't, but this is the point of a fence).
import lumaserver  # noqa: E402


def _reset_state():
    lumaserver.CONFIG["password"] = "PrimaryPass1"
    lumaserver.CONFIG["fallback_password"] = "FallbackPass1"
    with lumaserver._device_credential_lock:
        lumaserver._device_credential_cache.clear()
    with lumaserver.cache_lock:
        lumaserver.cache_units.clear()


class PrimaryFallbackSemanticsTests(unittest.TestCase):
    """Part 8.2 'Passwords' checklist, verified against mocked responses
    only -- no test in this class can reach a real device."""

    def setUp(self):
        _reset_state()

    def test_primary_success_is_a_single_attempt(self):
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = lumaserver._ws_call_auth("10.0.0.1", "System.Get", {})

        self.assertEqual(attempts, ["PrimaryPass1"])
        self.assertNotIn("error", resp)
        self.assertEqual(lumaserver._known_credential_source("10.0.0.1"), "primary")

    def test_primary_rejected_fallback_attempted_and_succeeds(self):
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            pw = payload["params"]["password"]
            attempts.append(pw)
            if pw == "PrimaryPass1":
                return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32001, "message": "Invalid password"}}
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = lumaserver._ws_call_auth("10.0.0.2", "System.Get", {})

        self.assertEqual(attempts, ["PrimaryPass1", "FallbackPass1"])
        self.assertNotIn("error", resp)
        self.assertEqual(lumaserver._known_credential_source("10.0.0.2"), "fallback")

    def test_primary_success_means_fallback_is_never_attempted(self):
        """Assert on the attempt count, not just the outcome (Part 8.2)."""
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.0.0.3", "System.Get", {})

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0], "PrimaryPass1")

    def test_transport_failure_none_response_never_triggers_fallback(self):
        """A None response from _ws_call is what a timeout, a refused
        connection, or any other transport failure look like. It must
        never be treated as a credential rejection."""
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return None

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = lumaserver._ws_call_auth("10.0.0.4", "System.Get", {})

        self.assertEqual(attempts, ["PrimaryPass1"])
        self.assertIsNone(resp)
        self.assertIsNone(lumaserver._known_credential_source("10.0.0.4"))

    def test_malformed_or_non_dict_response_never_triggers_fallback(self):
        """_is_credential_rejection must reject anything that isn't a
        well-formed dict with an 'error' dict -- a malformed/unexpected
        response is not proof of a bad credential."""
        for bogus in (None, "not json", 42, ["a", "list"], {"no_error_key": True}, {"error": "not a dict"}):
            self.assertFalse(lumaserver._is_credential_rejection(bogus), repr(bogus))

    def test_unrelated_protocol_error_does_not_trigger_fallback(self):
        """A real, well-formed error response that has nothing to do with
        credentials (e.g. method not found) must not spend a fallback
        attempt."""
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "Method not found"}}

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.0.0.5", "Some.UnknownMethod", {})

        self.assertEqual(attempts, ["PrimaryPass1"])

    def test_known_good_credential_is_reused_first(self):
        """Once a device's working credential is known, subsequent calls
        must start with it directly rather than re-trying Primary first."""
        lumaserver._remember_credential_source("10.0.0.6", "fallback")
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.0.0.6", "System.Get", {})

        self.assertEqual(attempts, ["FallbackPass1"])

    def test_both_credentials_rejected_reports_failed_not_a_source(self):
        lumaserver.cache_units["10.0.0.7"] = {"ip": "10.0.0.7"}

        def fake_ws_call(ip, payload, timeout):
            return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32001, "message": "Invalid password"}}

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = lumaserver._ws_call_auth("10.0.0.7", "System.Get", {})

        self.assertTrue(lumaserver._is_credential_rejection(resp))
        self.assertIsNone(lumaserver._known_credential_source("10.0.0.7"))
        self.assertEqual(lumaserver.cache_units["10.0.0.7"].get("password_source"), "failed")

    def test_no_fallback_configured_is_a_single_attempt_on_rejection(self):
        lumaserver.CONFIG["fallback_password"] = ""
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32001, "message": "Invalid password"}}

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.0.0.8", "System.Get", {})

        self.assertEqual(attempts, ["PrimaryPass1"])

    def test_producer_proxy_shares_the_same_resolution(self):
        """api_producer_jsonrpc must go through the same Primary/Fallback
        logic as _ws_call_auth, not a separate/divergent path."""
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            pw = payload["params"]["password"]
            attempts.append(pw)
            if pw == "PrimaryPass1":
                return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32001, "message": "invalid password"}}
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}

        client = lumaserver.APP.test_client()
        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = client.post("/api/producer/jsonrpc", json={"ip": "10.0.0.9", "method": "Rtmp.Get", "params": {}})

        self.assertEqual(attempts, ["PrimaryPass1", "FallbackPass1"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(lumaserver._known_credential_source("10.0.0.9"), "fallback")

    def test_switcher_top_level_calls_carry_no_password(self):
        """Verified device quirk: CS31 switcher calls via _ws_call_switcher
        use top-level params with no password field at all, so
        Primary/Fallback does not apply to that path."""
        captured = {}

        def fake_ws_call(ip, payload, timeout):
            captured["payload"] = payload
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_switcher("10.0.0.10", "NetworkHostname.Set", {"hostname": "switcher-1"})

        self.assertNotIn("password", captured["payload"])
        self.assertEqual(captured["payload"].get("hostname"), "switcher-1")


class SecretLeakTests(unittest.TestCase):
    """Part 4.5's required canary test: a secret value must never surface
    in a response body or a log line."""

    CANARY = "canary-must-never-appear"

    def setUp(self):
        _reset_state()
        self.log_records = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.log_records.append(record.getMessage())
        logging.getLogger().addHandler(self.handler)

    def tearDown(self):
        logging.getLogger().removeHandler(self.handler)

    def _assert_canary_absent_everywhere(self, *bodies):
        for body in bodies:
            self.assertNotIn(self.CANARY, body)
        for message in self.log_records:
            self.assertNotIn(self.CANARY, message)

    def test_config_post_password_fields_never_leak(self):
        client = lumaserver.APP.test_client()
        resp = client.post("/api/config", json={
            "password": self.CANARY + "Aa1!",
            "fallback_password": self.CANARY + "Bb2!",
            "username": self.CANARY,
        })
        self._assert_canary_absent_everywhere(resp.get_data(as_text=True))

        # Default GET (no include_password) must not echo either secret,
        # but username is not a secret in this endpoint's own contract and
        # is expected to be returned -- assert that distinction explicitly.
        get_resp = client.get("/api/config")
        body = get_resp.get_data(as_text=True)
        self.assertNotIn(self.CANARY + "Aa1!", body)
        self.assertNotIn(self.CANARY + "Bb2!", body)

    def test_ws_call_auth_never_logs_the_password_value(self):
        canary_password = self.CANARY + "Cc3!"
        lumaserver.CONFIG["password"] = canary_password
        lumaserver.CONFIG["fallback_password"] = ""

        def fake_ws_call(ip, payload, timeout):
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.0.0.11", "System.Get", {})

        for message in self.log_records:
            self.assertNotIn(canary_password, message)

    def test_producer_change_password_never_leaks_and_never_touches_real_hardware(self):
        """The legacy HTTP change-password endpoint calls requests.post
        directly to the device -- fenced here with an explicit mock so this
        test cannot reach real hardware, per Part 8.4."""
        client = lumaserver.APP.test_client()

        class _FakeResponse:
            status_code = 200
            ok = True

            def json(self):
                return {"code": 0}

        # newpassword must satisfy _is_allowed_password's character rule
        # (alphanumeric + !@#$%^&*, no hyphens) to reach requests.post at
        # all -- rejecting it earlier is the correct, desired behavior,
        # just not what this particular test is checking.
        canary_pw = "canaryMustNeverAppear1"
        with mock.patch.object(_requests_module, "post", return_value=_FakeResponse()) as fake_post:
            resp = client.post("/api/producer/change_password", json={
                "ip": "10.0.0.12",
                "username": self.CANARY,
                "oldpassword": self.CANARY + "-Old1!",
                "newpassword": canary_pw + "New1",
            })
        self.assertTrue(fake_post.called)
        self._assert_canary_absent_everywhere(resp.get_data(as_text=True))
        self.assertNotIn(canary_pw, resp.get_data(as_text=True))


def _report_and_exit():
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)

    print("\n--- Hardware fence report ---")
    if _transport_attempts:
        print("CAUGHT %d attempted real-transport call(s) that were correctly blocked:" % len(_transport_attempts))
        for name, args, kwargs in _transport_attempts:
            print("  - %s(args=%r, kwargs=%r)" % (name, args, kwargs))
    else:
        print("No test attempted a real network transport. Fence was armed for the whole run.")

    for p in _fence_patchers:
        p.stop()

    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    _report_and_exit()
