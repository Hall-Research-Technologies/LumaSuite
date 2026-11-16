"""
Hall Research published-firmware status tests (CHECK + COMPARE + DISPLAY
optional internet enrichment -- see PROJECT.md "Hall Research published-
firmware status" and lumaserver._fetch_hall_firmware_catalog /
api_firmware_status).

HARDWARE FENCE: same pattern as test_password_fallback.py -- every real
network transport (socket, websocket-client, requests.post/.get) is replaced
with a spy that raises if actually invoked, before lumaserver is imported.
This feature's own network call (requests.get against
https://hallresearch.com/api/products/<slug>) is exercised here exclusively
through a per-test mock of lumaserver.requests.get -- never the real
transport -- so a bug that let this reach past the mock would trip the same
fence as any device-I/O test.

Run directly: python tests/test_hall_firmware_status.py
Or via discovery: python -m unittest discover -s tests -p "test_*.py"
"""
import json
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

_TMP_DATA_DIR = tempfile.mkdtemp(prefix="lumasuite_test_data_")
os.environ["LUMA_DATA_DIR"] = _TMP_DATA_DIR

_transport_attempts = []


def _hardware_fence_spy(name):
    def _fn(*args, **kwargs):
        _transport_attempts.append((name, args, kwargs))
        raise AssertionError(
            "HARDWARE FENCE TRIPPED: a test attempted a real '%s' call "
            "(args=%r kwargs=%r). Mock lumaserver.requests.get instead of "
            "letting a real transport be reached." % (name, args, kwargs)
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


def _hall_version_payload(version_entries):
    """Build a minimal Hall product-API-shaped payload. version_entries is a
    list of (version_str, release_date) tuples, newest-first or in any order
    -- the code under test must pick the numerically highest, not the first."""
    firmware = []
    for version_str, release_date in version_entries:
        firmware.append({
            "version": [{"type": "paragraph", "text": version_str, "spans": []}],
            "release_date": release_date,
            "firmware_link": {"link_type": "Web", "url": "https://example.invalid/should-never-be-read.zip"},
        })
    return {"data": {"firmware": firmware}}


def _fake_response(status_code=200, json_body=None, content=b"{}"):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.content = content
    resp.json = mock.Mock(return_value=json_body if json_body is not None else {})
    return resp


def _wait_for_firmware_threads(timeout=15.0):
    """A test owns every thread it set in motion: stop the schedule loop and wait
    for any fetch still running, so nothing lands inside a later test."""
    import threading as _threading
    lumaserver._hall_firmware_stop.set()
    for thread in list(_threading.enumerate()):
        if thread.name == "hall-firmware":
            thread.join(timeout)


class _ResetMixin:
    def setUp(self):
        _wait_for_firmware_threads()
        lumaserver._hall_firmware_stop.clear()
        with lumaserver._hall_firmware_lock:
            lumaserver._hall_firmware_cache.clear()
            lumaserver._hall_firmware_fetch_state = "pending"
            lumaserver._hall_firmware_fetched_at = None
            lumaserver._hall_firmware_fetch_started = False
            lumaserver._hall_firmware_last_attempt = None
            lumaserver._hall_firmware_last_error = ""
        # The remembered answer is on disk; one test's must not be the next one's.
        try:
            os.remove(lumaserver.HALL_FIRMWARE_STATE_FILE)
        except OSError:
            pass
        with lumaserver.cache_lock:
            lumaserver.cache_units.clear()
        self.addCleanup(_wait_for_firmware_threads)

    def _seed_unit(self, ip, model, version):
        with lumaserver.cache_lock:
            lumaserver.cache_units[ip] = {"ip": ip, "model": model, "version": version}


class ModelMappingTests(_ResetMixin, unittest.TestCase):
    def test_e4211_maps_to_slug(self):
        self.assertEqual(lumaserver._hall_slug_for_model("HW-LUMA-E4211"), "hw-luma-e4211")

    def test_d4111_maps_to_slug(self):
        self.assertEqual(lumaserver._hall_slug_for_model("HW-LUMA-D4111"), "hw-luma-d4111")

    def test_cs31_real_device_model_variant_maps_by_prefix(self):
        # Real hardware reports "AT-OME-CS31-SA-HDBT", not the bare product slug.
        self.assertEqual(lumaserver._hall_slug_for_model("AT-OME-CS31-SA-HDBT"), "at-ome-cs31-sa")

    def test_unsupported_model_has_no_mapping(self):
        self.assertIsNone(lumaserver._hall_slug_for_model("SOME-OTHER-VENDOR-BOX"))

    def test_empty_model_has_no_mapping(self):
        self.assertIsNone(lumaserver._hall_slug_for_model(""))
        self.assertIsNone(lumaserver._hall_slug_for_model(None))


class CatalogFetchTests(_ResetMixin, unittest.TestCase):
    def test_picks_highest_version_not_first_entry(self):
        payload = _hall_version_payload([("1.0.1", None), ("1.0.2", "2026-04-16")])
        with mock.patch.object(lumaserver.requests, "get", return_value=_fake_response(json_body=payload)) as m:
            lumaserver._fetch_hall_firmware_catalog()
        self.assertEqual(m.call_count, 3)  # one per mapped product, every call
        self.assertEqual(lumaserver._hall_firmware_cache["hw-luma-e4211"]["version"], "1.0.2")
        self.assertEqual(lumaserver._hall_firmware_fetch_state, "ready")
        self.assertIsNotNone(lumaserver._hall_firmware_fetched_at)

    def test_never_reads_firmware_link(self):
        # The payload deliberately includes a firmware_link; assert the cached
        # entry never carries it forward (this feature never treats a
        # Hall-supplied URL as an install source).
        payload = _hall_version_payload([("1.0.2", "2026-04-16")])
        with mock.patch.object(lumaserver.requests, "get", return_value=_fake_response(json_body=payload)):
            lumaserver._fetch_hall_firmware_catalog()
        entry = lumaserver._hall_firmware_cache["hw-luma-e4211"]
        self.assertNotIn("firmware_link", entry)
        self.assertNotIn("url", entry)

    def test_connection_error_leaves_state_unavailable_no_raise(self):
        with mock.patch.object(lumaserver.requests, "get", side_effect=ConnectionError("no route")):
            lumaserver._fetch_hall_firmware_catalog()  # must not raise
        self.assertEqual(lumaserver._hall_firmware_cache, {})
        self.assertEqual(lumaserver._hall_firmware_fetch_state, "unavailable")

    def test_dns_failure_leaves_state_unavailable_no_raise(self):
        import socket as socket_module
        with mock.patch.object(lumaserver.requests, "get", side_effect=socket_module.gaierror("name resolution failed")):
            lumaserver._fetch_hall_firmware_catalog()
        self.assertEqual(lumaserver._hall_firmware_fetch_state, "unavailable")

    def test_timeout_leaves_state_unavailable_no_raise(self):
        import requests as requests_module
        with mock.patch.object(lumaserver.requests, "get", side_effect=requests_module.exceptions.Timeout("timed out")):
            lumaserver._fetch_hall_firmware_catalog()
        self.assertEqual(lumaserver._hall_firmware_fetch_state, "unavailable")

    def test_non_200_status_is_treated_as_unavailable_for_that_product(self):
        with mock.patch.object(lumaserver.requests, "get", return_value=_fake_response(status_code=404, json_body={})):
            lumaserver._fetch_hall_firmware_catalog()
        self.assertEqual(lumaserver._hall_firmware_cache, {})
        self.assertEqual(lumaserver._hall_firmware_fetch_state, "unavailable")

    def test_malformed_json_response_does_not_raise(self):
        resp = _fake_response()
        resp.json = mock.Mock(side_effect=ValueError("not json"))
        with mock.patch.object(lumaserver.requests, "get", return_value=resp):
            lumaserver._fetch_hall_firmware_catalog()  # must not raise
        self.assertEqual(lumaserver._hall_firmware_fetch_state, "unavailable")

    def test_missing_firmware_section_is_handled(self):
        with mock.patch.object(lumaserver.requests, "get", return_value=_fake_response(json_body={"data": {}})):
            lumaserver._fetch_hall_firmware_catalog()
        self.assertEqual(lumaserver._hall_firmware_cache, {})
        self.assertEqual(lumaserver._hall_firmware_fetch_state, "unavailable")

    def test_oversized_response_is_rejected(self):
        huge = b"x" * (lumaserver.HALL_FIRMWARE_MAX_RESPONSE_BYTES + 1)
        resp = _fake_response(content=huge)
        with mock.patch.object(lumaserver.requests, "get", return_value=resp):
            lumaserver._fetch_hall_firmware_catalog()
        self.assertEqual(lumaserver._hall_firmware_cache, {})

    def test_one_product_failing_does_not_block_the_others(self):
        good_payload = _hall_version_payload([("1.0.2", "2026-04-16")])

        def _side_effect(url, timeout=None):
            if "hw-luma-e4211" in url:
                raise ConnectionError("simulated failure for this product only")
            return _fake_response(json_body=good_payload)

        with mock.patch.object(lumaserver.requests, "get", side_effect=_side_effect):
            lumaserver._fetch_hall_firmware_catalog()
        self.assertNotIn("hw-luma-e4211", lumaserver._hall_firmware_cache)
        self.assertIn("hw-luma-d4111", lumaserver._hall_firmware_cache)
        self.assertIn("at-ome-cs31-sa", lumaserver._hall_firmware_cache)
        self.assertEqual(lumaserver._hall_firmware_fetch_state, "ready")


class VersionComparisonTests(_ResetMixin, unittest.TestCase):
    def _run_with_published(self, published_version):
        # Mirror what the real lazy-start path guarantees: the fetch and the
        # "already started" flag always move together, so a later route call
        # in the same test never spawns a second, unmocked background fetch
        # that would race the assertions below.
        with lumaserver._hall_firmware_lock:
            lumaserver._hall_firmware_fetch_started = True
        payload = _hall_version_payload([(published_version, "2026-04-16")])
        with mock.patch.object(lumaserver.requests, "get", return_value=_fake_response(json_body=payload)):
            lumaserver._fetch_hall_firmware_catalog()

    def test_installed_older_is_update_available(self):
        self._seed_unit("10.0.0.1", "HW-LUMA-E4211", "V1.0.1.0")
        self._run_with_published("1.0.2")
        with lumaserver.APP.test_client() as c:
            data = json.loads(c.get("/api/firmware_status").data)
        self.assertEqual(data["results"]["10.0.0.1"]["state"], "update_available")

    def test_installed_equal_is_up_to_date(self):
        self._seed_unit("10.0.0.2", "HW-LUMA-E4211", "1.0.2")
        self._run_with_published("1.0.2")
        with lumaserver.APP.test_client() as c:
            data = json.loads(c.get("/api/firmware_status").data)
        self.assertEqual(data["results"]["10.0.0.2"]["state"], "up_to_date")

    def test_installed_newer_is_up_to_date_not_an_error(self):
        """Dev/engineering units legitimately run ahead of Hall's current
        public release -- this must never read as needing an update."""
        self._seed_unit("10.0.0.3", "HW-LUMA-D4111", "V1.0.2.6")
        self._run_with_published("1.0.2")
        with lumaserver.APP.test_client() as c:
            data = json.loads(c.get("/api/firmware_status").data)
        self.assertEqual(data["results"]["10.0.0.3"]["state"], "up_to_date")

    def test_leading_v_is_normalized_on_both_sides(self):
        self._seed_unit("10.0.0.4", "HW-LUMA-E4211", "v1.0.2")
        self._run_with_published("V1.0.2")
        with lumaserver.APP.test_client() as c:
            data = json.loads(c.get("/api/firmware_status").data)
        self.assertEqual(data["results"]["10.0.0.4"]["state"], "up_to_date")

    def test_multi_component_numeric_not_lexicographic(self):
        """'1.0.10' must compare as greater than '1.0.2' numerically -- a
        lexicographic/string compare would wrongly rank '1.0.10' < '1.0.2'."""
        self._seed_unit("10.0.0.5", "HW-LUMA-E4211", "1.0.10")
        self._run_with_published("1.0.2")
        with lumaserver.APP.test_client() as c:
            data = json.loads(c.get("/api/firmware_status").data)
        self.assertEqual(data["results"]["10.0.0.5"]["state"], "up_to_date")

    def test_malformed_installed_version_is_unknown_not_crashed(self):
        self._seed_unit("10.0.0.6", "HW-LUMA-E4211", "not-a-version")
        self._run_with_published("1.0.2")
        with lumaserver.APP.test_client() as c:
            resp = c.get("/api/firmware_status")
            data = json.loads(resp.data)
        self.assertEqual(resp.status_code, 200)
        # A known model whose installed version cannot be read is Unknown, with the
        # reason; it is neither compared nor hidden.
        self.assertEqual(data["results"]["10.0.0.6"]["state"], "unknown")
        self.assertEqual(data["results"]["10.0.0.6"]["reason"], "installed_version_unreadable")

    def test_malformed_published_version_is_unknown_not_crashed(self):
        self._seed_unit("10.0.0.7", "HW-LUMA-E4211", "1.0.2.3")
        self._run_with_published("not-a-real-version-string")
        with lumaserver.APP.test_client() as c:
            resp = c.get("/api/firmware_status")
            data = json.loads(resp.data)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["results"]["10.0.0.7"]["state"], "unknown")
        self.assertEqual(data["results"]["10.0.0.7"]["reason"], "published_version_unavailable")

    def test_unsupported_model_produces_no_status(self):
        self._seed_unit("10.0.0.8", "SOME-OTHER-VENDOR-BOX", "1.0.0")
        self._run_with_published("1.0.2")
        with lumaserver.APP.test_client() as c:
            data = json.loads(c.get("/api/firmware_status").data)
        self.assertNotIn("10.0.0.8", data["results"])


class EndpointBehaviorTests(_ResetMixin, unittest.TestCase):
    def test_lazy_start_only_spawns_one_background_thread(self):
        payload = _hall_version_payload([("1.0.2", "2026-04-16")])
        with mock.patch.object(lumaserver.requests, "get", return_value=_fake_response(json_body=payload)) as m_get:
            with mock.patch.object(lumaserver.threading, "Thread", wraps=lumaserver.threading.Thread) as m_thread:
                with lumaserver.APP.test_client() as c:
                    c.get("/api/firmware_status")
                    c.get("/api/firmware_status")
                    c.get("/api/firmware_status")
                # Give the single background thread a moment to finish.
                import time as time_module
                for _ in range(50):
                    if lumaserver._hall_firmware_fetch_state != "pending":
                        break
                    time_module.sleep(0.05)
        self.assertEqual(m_thread.call_count, 1, "Only the first call should start the background fetch")
        # 3 GET calls total (one per product), regardless of how many times
        # the endpoint itself was hit -- never once per device, never once
        # per row, never once per repeated status request.
        self.assertEqual(m_get.call_count, 3)

    def test_first_call_returns_immediately_without_waiting_for_hall(self):
        """The endpoint must never block on the network -- simulate a Hall
        response that would hang, and assert the first call still returns
        promptly with a 'pending' state."""
        import time as time_module

        def _slow_get(url, timeout=None):
            time_module.sleep(1.5)
            return _fake_response(json_body=_hall_version_payload([("1.0.2", None)]))

        with mock.patch.object(lumaserver.requests, "get", side_effect=_slow_get):
            with lumaserver.APP.test_client() as c:
                started = time_module.time()
                resp = c.get("/api/firmware_status")
                elapsed = time_module.time() - started
                _wait_for_firmware_threads()      # still inside the mock
        data = json.loads(resp.data)
        self.assertLess(elapsed, 1.0, "The request must not block on the Hall lookup")
        self.assertEqual(data["hall_lookup"]["state"], "pending")
        self.assertEqual(data["results"], {})

    def test_zero_device_io_and_zero_set_calls(self):
        """This endpoint must never touch _ws_call/_ws_call_auth -- it only
        ever reads cache_units and (in the background) Hall's public API."""
        self._seed_unit("10.0.0.9", "HW-LUMA-E4211", "1.0.2")
        payload = _hall_version_payload([("1.0.2", None)])
        with mock.patch.object(lumaserver, "_ws_call", side_effect=AssertionError("must not be called")):
            with mock.patch.object(lumaserver, "_ws_call_auth", side_effect=AssertionError("must not be called")):
                with mock.patch.object(lumaserver.requests, "get", return_value=_fake_response(json_body=payload)):
                    with lumaserver.APP.test_client() as c:
                        resp = c.get("/api/firmware_status")
        self.assertEqual(resp.status_code, 200)

    def test_device_scan_style_cache_read_unaffected_when_hall_unavailable(self):
        """Normal local device management (reading cache_units) must keep
        working even when the Hall lookup fails outright."""
        self._seed_unit("10.0.0.10", "HW-LUMA-E4211", "1.0.2")
        with mock.patch.object(lumaserver.requests, "get", side_effect=ConnectionError("offline")):
            with lumaserver.APP.test_client() as c:
                resp = c.get("/api/firmware_status")
                import time as time_module
                for _ in range(50):
                    if lumaserver._hall_firmware_fetch_state != "pending":
                        break
                    time_module.sleep(0.05)
                # Asked again once the check has answered, a known model is
                # Unknown, not blank. (The "nothing yet" answer while a check is
                # still running is held by the slow-Hall test above.)
                data = json.loads(c.get("/api/firmware_status").data)
        # Installed version remains visible via the ordinary /api/cache path
        # (unit already seeded); the firmware-status endpoint itself must not
        # error just because Hall is unreachable. With nothing ever retrieved
        # the answer for a known model is a truthful Unknown, never N/A.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["results"]["10.0.0.10"]["state"], "unknown")
        self.assertEqual(data["results"]["10.0.0.10"]["reason"], "published_version_unavailable")
        with lumaserver.cache_lock:
            self.assertEqual(lumaserver.cache_units["10.0.0.10"]["version"], "1.0.2")


class HardwareFenceReportTest(unittest.TestCase):
    def test_no_real_transport_was_ever_reached_by_this_module(self):
        # Sanity check mirroring the other suites' fence report. A bare
        # 'socket.socket(AF_INET6)' probe is a known, pre-existing benign
        # artifact seen across every test file in this repo (Python/urllib3
        # internals checking IPv6 availability, not an actual connection
        # attempt) -- tolerate exactly that, fail on anything else, and
        # especially fail on any real requests.get/.post or
        # websocket.create_connection reaching past a test's own mock.
        unexpected = [
            (name, args, kwargs) for (name, args, kwargs) in _transport_attempts
            if not (name == "socket.socket" and args and args[0] == socket_module.AF_INET6)
        ]
        self.assertEqual(unexpected, [])


if __name__ == "__main__":
    print("\n--- Hardware fence report ---")
    unittest.main(exit=False, verbosity=2)
    if _transport_attempts:
        print(f"CAUGHT {len(_transport_attempts)} attempted real-transport call(s) that were correctly blocked:")
        for name, args, kwargs in _transport_attempts:
            print(f"  - {name}(args={args}, kwargs={kwargs})")
    else:
        print("No real-transport attempts were made (expected -- all tests mock lumaserver.requests.get).")
