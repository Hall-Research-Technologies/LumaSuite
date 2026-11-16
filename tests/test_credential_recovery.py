"""
Credential-resolver recovery + Producer-proxy override tests (2026-09-19
auth/device-I/O correctness pass).

Covers the transition matrix that tests/test_password_fallback.py's
original suite predates:
  - cached-Fallback recovering back to Primary on a genuine rejection
    (the one-way-fallback defect this pass fixes)
  - non-auth failures never rotating the cached source, from either
    starting slot
  - both-credentials-rejected is not a permanent lockout -- the very next
    call can still recover
  - duplicate Primary/Fallback values never sent twice
  - cached source invalidation on a Settings change (Primary changed,
    Fallback changed, both changed, Fallback removed, swapped)
  - the Producer JSON-RPC proxy's caller-supplied password override:
    survives the proxy, is operation-scoped (never mutates Settings or
    the credential cache), is never echoed or logged, and stays distinct
    from any other params field (e.g. a LoginPassword.Set new-value
    field) it is sent alongside.

HARDWARE FENCE: identical pattern to test_password_fallback.py -- every
real network transport is replaced with a spy that raises before
lumaserver is imported. See that file's module docstring for the full
rationale; this file intentionally copies the fence verbatim rather than
re-deriving it.

Run directly: python tests/test_credential_recovery.py
Or via discovery: python -m unittest discover -s tests -p "test_*.py"
"""
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

_TMP_DATA_DIR = tempfile.mkdtemp(prefix="lumasuite_test_data_")
os.environ["LUMA_DATA_DIR"] = _TMP_DATA_DIR
# Pre-seed an empty units_cache.json (AGENTS.md Testing & hardware safety):
# an unseeded-but-fresh LUMA_DATA_DIR still migrates whatever
# units_cache.json sits beside lumaserver.py in the repo root -- i.e. real
# local device data -- into the new directory, and this test file exercises
# /api/cache and /api/config, which can trigger background verification of
# whatever is in cache_units. Seeding an empty array keeps this run fully
# isolated from any real local device data.
with open(os.path.join(_TMP_DATA_DIR, "units_cache.json"), "w", encoding="utf-8") as _f:
    _f.write("[]")

_transport_attempts = []


def _hardware_fence_spy(name):
    def _fn(*args, **kwargs):
        _transport_attempts.append((name, args, kwargs))
        raise AssertionError(
            "HARDWARE FENCE TRIPPED: a test attempted a real '%s' call "
            "(args=%r kwargs=%r). Tests must mock lumaserver._ws_call "
            "instead of letting a real transport be reached." % (name, args, kwargs)
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

import lumaserver  # noqa: E402


def _reset_state():
    lumaserver.CONFIG["password"] = "PrimaryPass1"
    lumaserver.CONFIG["fallback_password"] = "FallbackPass1"
    with lumaserver._device_credential_lock:
        lumaserver._device_credential_cache.clear()
    with lumaserver.cache_lock:
        lumaserver.cache_units.clear()


def _rejection(payload):
    return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32001, "message": "Invalid password"}}


def _ok(payload, result=None):
    return {"jsonrpc": "2.0", "id": payload["id"], "result": result if result is not None else {"ok": True}}


class CachedFallbackRecoveryTests(unittest.TestCase):
    """The core defect this pass fixes: a cached Fallback must be able to
    recover back to Primary on a genuine rejection, not fail forever."""

    def setUp(self):
        _reset_state()

    def test_cached_fallback_rejected_recovers_to_primary(self):
        lumaserver._remember_credential_source("10.1.0.1", "fallback")
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            pw = payload["params"]["password"]
            attempts.append(pw)
            if pw == "FallbackPass1":
                return _rejection(payload)
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = lumaserver._ws_call_auth("10.1.0.1", "System.Get", {})

        self.assertEqual(attempts, ["FallbackPass1", "PrimaryPass1"])
        self.assertNotIn("error", resp)
        self.assertEqual(lumaserver._known_credential_source("10.1.0.1"), "primary")

    def test_cached_primary_rejected_fallback_attempted_and_succeeds(self):
        """Explicit cached-Primary case (distinct from the no-cache default,
        even though both resolve first_source == "primary") -- proves an
        EXPLICITLY remembered Primary still rotates forward to Fallback on
        a genuine rejection, symmetric with the cached-Fallback case."""
        lumaserver._remember_credential_source("10.1.0.8", "primary")
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            pw = payload["params"]["password"]
            attempts.append(pw)
            if pw == "PrimaryPass1":
                return _rejection(payload)
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = lumaserver._ws_call_auth("10.1.0.8", "System.Get", {})

        self.assertEqual(attempts, ["PrimaryPass1", "FallbackPass1"])
        self.assertNotIn("error", resp)
        self.assertEqual(lumaserver._known_credential_source("10.1.0.8"), "fallback")

    def test_cached_fallback_success_stays_on_fallback_no_extra_attempt(self):
        lumaserver._remember_credential_source("10.1.0.2", "fallback")
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.1.0.2", "System.Get", {})

        self.assertEqual(attempts, ["FallbackPass1"])
        self.assertEqual(lumaserver._known_credential_source("10.1.0.2"), "fallback")

    def test_cached_fallback_non_auth_failure_does_not_try_primary(self):
        """Fallback non-auth failure -> STOP, never try Primary merely
        because transport failed."""
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return None  # timeout/refused/malformed -- never a rejection

        lumaserver._remember_credential_source("10.1.0.3", "fallback")
        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = lumaserver._ws_call_auth("10.1.0.3", "System.Get", {})

        self.assertEqual(attempts, ["FallbackPass1"])
        self.assertIsNone(resp)
        # Cache must be untouched -- still "fallback", not rotated/cleared.
        self.assertEqual(lumaserver._known_credential_source("10.1.0.3"), "fallback")

    def test_cached_primary_non_auth_failure_does_not_try_fallback(self):
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return None

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = lumaserver._ws_call_auth("10.1.0.4", "System.Get", {})

        self.assertEqual(attempts, ["PrimaryPass1"])
        self.assertIsNone(resp)
        self.assertIsNone(lumaserver._known_credential_source("10.1.0.4"))

    def test_unrelated_jsonrpc_error_from_cached_fallback_does_not_rotate(self):
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "Method not found"}}

        lumaserver._remember_credential_source("10.1.0.5", "fallback")
        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.1.0.5", "Some.UnknownMethod", {})

        self.assertEqual(attempts, ["FallbackPass1"])
        self.assertEqual(lumaserver._known_credential_source("10.1.0.5"), "fallback")

    def test_both_rejected_starting_from_cached_fallback_is_not_a_lockout(self):
        """Both auth-reject (fallback-first order) -> Auth Failed this
        call, but the cache must not latch a dead state."""
        lumaserver.cache_units["10.1.0.6"] = {"ip": "10.1.0.6"}
        lumaserver._remember_credential_source("10.1.0.6", "fallback")

        def fake_ws_call(ip, payload, timeout):
            return _rejection(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = lumaserver._ws_call_auth("10.1.0.6", "System.Get", {})

        self.assertTrue(lumaserver._is_credential_rejection(resp))
        self.assertIsNone(lumaserver._known_credential_source("10.1.0.6"))
        self.assertEqual(lumaserver.cache_units["10.1.0.6"].get("password_source"), "failed")

    def test_recovery_after_prior_auth_failed(self):
        """After a both-rejected call, a LATER call (e.g. credentials on
        the device changed back) must still be able to succeed -- no
        permanent lockout."""
        lumaserver._remember_credential_source("10.1.0.7", "fallback")

        def fake_ws_call_both_reject(ip, payload, timeout):
            return _rejection(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call_both_reject):
            first = lumaserver._ws_call_auth("10.1.0.7", "System.Get", {})
        self.assertTrue(lumaserver._is_credential_rejection(first))

        attempts = []

        def fake_ws_call_primary_now_good(ip, payload, timeout):
            pw = payload["params"]["password"]
            attempts.append(pw)
            if pw == "PrimaryPass1":
                return _ok(payload)
            return _rejection(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call_primary_now_good):
            second = lumaserver._ws_call_auth("10.1.0.7", "System.Get", {})

        self.assertNotIn("error", second)
        self.assertEqual(attempts, ["PrimaryPass1"])
        self.assertEqual(lumaserver._known_credential_source("10.1.0.7"), "primary")


class DuplicateAndMissingCredentialTests(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def test_identical_primary_and_fallback_never_sent_twice(self):
        lumaserver.CONFIG["fallback_password"] = "PrimaryPass1"  # identical to Primary
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return _rejection(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.2.0.1", "System.Get", {})

        self.assertEqual(attempts, ["PrimaryPass1"])

    def test_identical_credentials_with_cached_fallback_uses_primary_slot(self):
        lumaserver.CONFIG["fallback_password"] = "PrimaryPass1"
        lumaserver._remember_credential_source("10.2.0.2", "fallback")
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.2.0.2", "System.Get", {})

        self.assertEqual(attempts, ["PrimaryPass1"])
        self.assertEqual(lumaserver._known_credential_source("10.2.0.2"), "primary")

    def test_fallback_removed_while_cached_falls_back_to_primary(self):
        lumaserver._remember_credential_source("10.2.0.3", "fallback")
        lumaserver.CONFIG["fallback_password"] = ""
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"]["password"])
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.2.0.3", "System.Get", {})

        self.assertEqual(attempts, ["PrimaryPass1"])


class SettingsChangeInvalidationTests(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.client = lumaserver.APP.test_client()

    def test_primary_change_invalidates_cache(self):
        lumaserver._remember_credential_source("10.3.0.1", "fallback")
        lumaserver.cache_units["10.3.0.1"] = {"ip": "10.3.0.1", "password_source": "fallback"}
        resp = self.client.post("/api/config", json={"password": "NewPrimaryAa1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(lumaserver._known_credential_source("10.3.0.1"))
        self.assertNotIn("password_source", lumaserver.cache_units["10.3.0.1"])

    def test_fallback_change_invalidates_cache(self):
        lumaserver._remember_credential_source("10.3.0.2", "primary")
        resp = self.client.post("/api/config", json={"fallback_password": "NewFallbackBb2"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(lumaserver._known_credential_source("10.3.0.2"))

    def test_both_changed_invalidates_cache(self):
        lumaserver._remember_credential_source("10.3.0.3", "fallback")
        resp = self.client.post("/api/config", json={"password": "NewPrimaryCc3", "fallback_password": "NewFallbackDd4"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(lumaserver._known_credential_source("10.3.0.3"))

    def test_credentials_swapped_invalidates_cache(self):
        lumaserver._remember_credential_source("10.3.0.4", "primary")
        resp = self.client.post("/api/config", json={"password": "FallbackPass1", "fallback_password": "PrimaryPass1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(lumaserver._known_credential_source("10.3.0.4"))

    def test_unrelated_config_change_does_not_invalidate_cache(self):
        """Changing an unrelated field (username) must not spuriously
        invalidate the credential cache."""
        lumaserver._remember_credential_source("10.3.0.5", "fallback")
        resp = self.client.post("/api/config", json={"username": "admin2"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(lumaserver._known_credential_source("10.3.0.5"), "fallback")

    def test_setting_password_to_same_value_does_not_invalidate_cache(self):
        lumaserver._remember_credential_source("10.3.0.6", "fallback")
        resp = self.client.post("/api/config", json={"password": "PrimaryPass1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(lumaserver._known_credential_source("10.3.0.6"), "fallback")


class ProducerOverrideTests(unittest.TestCase):
    """Producer JSON-RPC proxy: caller-supplied password override."""

    def setUp(self):
        _reset_state()
        self.client = lumaserver.APP.test_client()

    def test_override_reaches_outbound_call_verbatim(self):
        captured = {}

        def fake_ws_call(ip, payload, timeout):
            captured["password"] = payload["params"].get("password")
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = self.client.post("/api/producer/jsonrpc", json={
                "ip": "10.4.0.1", "method": "Platform.Reboot",
                "params": {"password": "OverrideValue1"},
            })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["password"], "OverrideValue1")

    def test_override_is_single_attempt_even_on_rejection(self):
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload["params"].get("password"))
            return _rejection(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            self.client.post("/api/producer/jsonrpc", json={
                "ip": "10.4.0.2", "method": "Platform.Reboot",
                "params": {"password": "OverrideValue2"},
            })

        self.assertEqual(attempts, ["OverrideValue2"])

    def test_override_never_mutates_settings_or_cache(self):
        def fake_ws_call(ip, payload, timeout):
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            self.client.post("/api/producer/jsonrpc", json={
                "ip": "10.4.0.3", "method": "Platform.Reboot",
                "params": {"password": "OverrideValue3"},
            })

        self.assertEqual(lumaserver.CONFIG["password"], "PrimaryPass1")
        self.assertEqual(lumaserver.CONFIG["fallback_password"], "FallbackPass1")
        self.assertIsNone(lumaserver._known_credential_source("10.4.0.3"))

    def test_no_override_preserves_normal_resolver_behavior(self):
        """Same proxy, no caller password in params -> ordinary
        Primary/Fallback resolution, unaffected by the override feature."""
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            pw = payload["params"]["password"] if "params" in payload and isinstance(payload["params"], dict) else None
            attempts.append(pw)
            if pw == "PrimaryPass1":
                return _rejection(payload)
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            self.client.post("/api/producer/jsonrpc", json={
                "ip": "10.4.0.4", "method": "Rtmp.Get", "params": {},
            })

        self.assertEqual(attempts, ["PrimaryPass1", "FallbackPass1"])
        self.assertEqual(lumaserver._known_credential_source("10.4.0.4"), "fallback")

    def test_override_never_echoed_in_response(self):
        def fake_ws_call(ip, payload, timeout):
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = self.client.post("/api/producer/jsonrpc", json={
                "ip": "10.4.0.5", "method": "Platform.Reboot",
                "params": {"password": "SecretCanaryValue5"},
            })

        self.assertNotIn("SecretCanaryValue5", resp.get_data(as_text=True))

    def test_override_never_logged(self):
        log_records = []
        handler = logging.Handler()
        handler.emit = lambda record: log_records.append(record.getMessage())
        logging.getLogger().addHandler(handler)
        try:
            def fake_ws_call(ip, payload, timeout):
                return _ok(payload)

            with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
                self.client.post("/api/producer/jsonrpc", json={
                    "ip": "10.4.0.6", "method": "Platform.Reboot",
                    "params": {"password": "SecretCanaryValue6"},
                })
        finally:
            logging.getLogger().removeHandler(handler)

        for message in log_records:
            self.assertNotIn("SecretCanaryValue6", message)

    def test_loginpassword_set_keeps_auth_and_new_value_distinct(self):
        """LoginPassword.Set's auth credential (params.password, resolved
        or overridden) must never be confused with a differently-keyed
        new-password value the caller sends alongside it -- the resolver
        only ever touches the "password" key."""
        captured = {}

        def fake_ws_call(ip, payload, timeout):
            captured["payload"] = payload
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = self.client.post("/api/producer/jsonrpc", json={
                "ip": "10.4.0.7", "method": "LoginPassword.Set",
                "params": {"password": "AuthOverrideValue7", "newpassword": "BrandNewValue7"},
            })

        self.assertEqual(resp.status_code, 200)
        sent_params = captured["payload"]["params"]
        self.assertEqual(sent_params.get("password"), "AuthOverrideValue7")
        self.assertEqual(sent_params.get("newpassword"), "BrandNewValue7")
        self.assertNotEqual(sent_params.get("password"), sent_params.get("newpassword"))
        body = resp.get_data(as_text=True)
        self.assertNotIn("AuthOverrideValue7", body)
        self.assertNotIn("BrandNewValue7", body)

    def test_malformed_producer_request_handled_safely(self):
        resp = self.client.post("/api/producer/jsonrpc", json={"method": "Rtmp.Get"})  # missing ip
        self.assertEqual(resp.status_code, 400)

        resp2 = self.client.post("/api/producer/jsonrpc", json={"ip": "10.4.0.8", "method": "Not.Allowed"})
        self.assertEqual(resp2.status_code, 400)

    def test_unrelated_method_unaffected_by_override_feature(self):
        """A method with no password concept in its params (bare bool)
        must still work through the shared resolver unaffected."""
        captured = {}

        def fake_ws_call(ip, payload, timeout):
            captured["payload"] = payload
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = self.client.post("/api/producer/jsonrpc", json={
                "ip": "10.4.0.9", "method": "SystemBlinkLed.Set", "params": True,
            })

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("password", captured["payload"])
        self.assertEqual(captured["payload"].get("params"), True)

    def test_cs31_style_producer_call_unaffected(self):
        """Existing CS31 Producer behavior (System.Get / SystemBlinkLed.Set
        with empty/bare params, as used by the CS31 blink-toggle UI) must
        still work unchanged by the override feature."""
        attempts = []

        def fake_ws_call(ip, payload, timeout):
            attempts.append(payload)
            return _ok(payload, result={"blinkled": False})

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            resp = self.client.post("/api/producer/jsonrpc", json={
                "ip": "203.0.113.173", "method": "System.Get", "params": {},
            })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(lumaserver._known_credential_source("203.0.113.173"), "primary")


class ResolverAuditSecretSafetyTests(unittest.TestCase):
    """Extends the existing secret-leak coverage to the new code paths
    (cache invalidation, recovery, override) added by this pass."""

    CANARY = "recovery-pass-canary"

    def setUp(self):
        _reset_state()
        self.log_records = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.log_records.append(record.getMessage())
        logging.getLogger().addHandler(self.handler)

    def tearDown(self):
        logging.getLogger().removeHandler(self.handler)

    def test_cached_fallback_recovery_never_logs_either_password(self):
        lumaserver.CONFIG["password"] = self.CANARY + "-Primary1"
        lumaserver.CONFIG["fallback_password"] = self.CANARY + "-Fallback1"
        lumaserver._remember_credential_source("10.5.0.1", "fallback")

        def fake_ws_call(ip, payload, timeout):
            pw = payload["params"]["password"]
            if pw == lumaserver.CONFIG["fallback_password"]:
                return _rejection(payload)
            return _ok(payload)

        with mock.patch.object(lumaserver, "_ws_call", side_effect=fake_ws_call):
            lumaserver._ws_call_auth("10.5.0.1", "System.Get", {})

        for message in self.log_records:
            self.assertNotIn(lumaserver.CONFIG["password"], message)
            self.assertNotIn(lumaserver.CONFIG["fallback_password"], message)

    def test_status_apis_never_include_password_source_secret_material(self):
        """The /api/cache and /api/config surfaces expose password_source
        ("primary"/"fallback"/"failed") but must never expose the actual
        secret value anywhere in that response."""
        lumaserver.CONFIG["password"] = self.CANARY + "-Primary2"
        lumaserver._remember_credential_source("10.5.0.2", "primary")
        lumaserver.cache_units["10.5.0.2"] = {"ip": "10.5.0.2", "password_source": "primary"}
        client = lumaserver.APP.test_client()
        resp = client.get("/api/cache")
        self.assertNotIn(self.CANARY + "-Primary2", resp.get_data(as_text=True))


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
