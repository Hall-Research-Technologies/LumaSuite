"""The credential that authenticated a unit is that unit's working credential.

Found on hardware (Omni, then confirmed to be the same class of defect here): a unit that
only answers to the fallback password was discovered and controlled, but a path that built
its own request from the primary alone failed until the unit's password was changed to
match. Mixed-password installations have to work for every authenticated operation.

What is held here:

  * the credential slot is remembered per unit IDENTITY (MAC), not per address, so a moved
    unit keeps its credential and a different unit at a reused address does not inherit it;
  * a restart does not forget it (the slot name is persisted on the unit's own record);
  * the HTTP paths (slate upload, logo/image upload, firmware login) resolve the unit's
    credential and rotate only on an HTTP 401/403;
  * the Production Studio proxy treats the page's idea of "the current password" as
    unresolved, not as an override, when it is one of the configured credentials;
  * a refused credential is re-resolved on the next call (never a latch);
  * no password reaches a response or a log record.

Nothing reaches a network: the WebSocket device and `requests.post` are in-memory fakes.
"""
import copy
import json
import logging
import threading
import unittest
from unittest import mock

import _support  # noqa: F401  (installs the fence and the scratch data directory first)

import lumaserver as srv

PRIMARY = "primary-secret-A"
FALLBACK = "fallback-secret-B"
ADDR_A = "192.0.2.51"      # accepts the primary
ADDR_B = "192.0.2.52"      # accepts only the fallback
MAC_A = "02:00:5e:51:00:0a"
MAC_B = "02:00:5e:51:00:0b"
MAC_C = "02:00:5e:51:00:0c"


class Lab:
    """One accepted password per address. Records offers by NAME, never by value."""

    def __init__(self):
        self.accepts = {ADDR_A: PRIMARY, ADDR_B: FALLBACK}
        self.ws_offers = []              # (address, method, "ok"/"refused", name)
        self.http_offers = []            # (address, path, status, name)
        self.http_status_override = {}   # address -> status that is not about the credential

    @staticmethod
    def name(pwd):
        return {PRIMARY: "primary", FALLBACK: "fallback"}.get(pwd, "other")

    def ws(self, ip, payload, timeout):
        offered = (payload.get("params") or {}).get("password")
        good = self.accepts.get(ip) == offered
        self.ws_offers.append((ip, payload.get("method"), "ok" if good else "refused", self.name(offered)))
        if not good:
            return {"jsonrpc": "2.0", "id": payload.get("id"),
                    "error": {"code": -32001, "message": "Authentication failed"}}
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"ok": True}}

    def http_post(self, url, *args, **kwargs):
        address = url.split("//", 1)[1].split("/", 1)[0]
        path = "/" + url.split("//", 1)[1].split("/", 1)[1]
        offered = None
        if "json" in kwargs and isinstance(kwargs["json"], dict):
            offered = kwargs["json"].get("password")
        bearer = (kwargs.get("headers") or {}).get("Authorization", "")
        if bearer.startswith("Bearer ") and "." in bearer:
            import base64
            body = bearer.split(".", 1)[1]
            body += "=" * (-len(body) % 4)
            try:
                offered = json.loads(base64.b64decode(body)).get("password")
            except Exception:
                offered = None
        if address in self.http_status_override:
            status = self.http_status_override[address]
        else:
            status = 200 if self.accepts.get(address) == offered else 401
        self.http_offers.append((address, path, status, self.name(offered)))
        response = mock.Mock()
        response.status_code = status
        response.text = ""
        return response


class WorkingCredentialTests(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        for target, replacement in (("_ws_call", self.lab.ws),):
            original = getattr(srv, target)
            self.addCleanup(lambda t=target, o=original: setattr(srv, t, o))
            setattr(srv, target, replacement)
        patcher = mock.patch.object(srv.requests, "post", self.lab.http_post)
        patcher.start()
        self.addCleanup(patcher.stop)
        with srv.cache_lock:
            saved_units = copy.deepcopy(dict(srv.cache_units))
            srv.cache_units.clear()
            srv.cache_units[ADDR_A] = {"ip": ADDR_A, "mac": MAC_A, "type": "encoder", "model": "TEST-E1"}
            srv.cache_units[ADDR_B] = {"ip": ADDR_B, "mac": MAC_B, "type": "encoder", "model": "TEST-E1"}
        saved_config = copy.deepcopy(dict(srv.CONFIG))
        srv.CONFIG["password"], srv.CONFIG["fallback_password"] = PRIMARY, FALLBACK
        srv.CONFIG["ws_include_password"] = True
        with srv._device_credential_lock:
            saved_slots = dict(srv._device_credential_cache)
            srv._device_credential_cache.clear()

        def restore():
            with srv.cache_lock:
                srv.cache_units.clear()
                srv.cache_units.update(saved_units)
            srv.CONFIG.clear()
            srv.CONFIG.update(saved_config)
            with srv._device_credential_lock:
                srv._device_credential_cache.clear()
                srv._device_credential_cache.update(saved_slots)
        self.addCleanup(restore)

    def call(self, ip, method="Test.Get"):
        return srv._ws_call_auth(ip, method, {"password": PRIMARY}, 1.0)

    # -- the resolver, by identity -------------------------------------------

    def test_a_unit_on_the_fallback_is_found_once_and_then_asked_directly(self):
        self.assertTrue(self.call(ADDR_B).get("result"))
        self.lab.ws_offers.clear()
        self.assertTrue(self.call(ADDR_B).get("result"))
        self.assertEqual([o[2:] for o in self.lab.ws_offers], [("ok", "fallback")],
                         "the second call presented the primary first")

    def test_the_slot_follows_the_mac_across_an_address_change(self):
        self.call(ADDR_B)
        moved = "192.0.2.77"
        self.lab.accepts[moved] = FALLBACK
        with srv.cache_lock:
            srv.cache_units.pop(ADDR_B)
            srv.cache_units[moved] = {"ip": moved, "mac": MAC_B, "type": "encoder", "model": "TEST-E1"}
        self.lab.ws_offers.clear()
        self.call(moved)
        self.assertEqual([o[2:] for o in self.lab.ws_offers], [("ok", "fallback")])

    def test_a_different_unit_at_a_reused_address_does_not_inherit_the_slot(self):
        self.call(ADDR_B)
        self.lab.accepts[ADDR_B] = PRIMARY
        with srv.cache_lock:
            srv.cache_units[ADDR_B] = {"ip": ADDR_B, "mac": MAC_C, "type": "encoder", "model": "TEST-E1"}
        self.lab.ws_offers.clear()
        self.assertTrue(self.call(ADDR_B).get("result"))
        self.assertEqual([o[2:] for o in self.lab.ws_offers], [("ok", "primary")])

    def test_the_persisted_slot_survives_a_restart(self):
        self.call(ADDR_B)
        with srv.cache_lock:
            self.assertEqual(srv.cache_units[ADDR_B].get("password_source"), "fallback")
        with srv._device_credential_lock:
            srv._device_credential_cache.clear()             # a fresh process
        self.lab.ws_offers.clear()
        self.call(ADDR_B)
        self.assertEqual([o[2:] for o in self.lab.ws_offers], [("ok", "fallback")])

    def test_a_unit_whose_password_changed_is_re_resolved(self):
        self.call(ADDR_B)
        self.lab.accepts[ADDR_B] = PRIMARY          # changed on the unit itself
        self.assertTrue(self.call(ADDR_B).get("result"))
        self.lab.ws_offers.clear()
        self.call(ADDR_B)
        self.assertEqual([o[2:] for o in self.lab.ws_offers], [("ok", "primary")])

    def test_a_unit_that_rejects_both_credentials_is_not_latched(self):
        self.call(ADDR_B)
        self.lab.accepts[ADDR_B] = "neither-of-ours"
        self.assertTrue(self.call(ADDR_B).get("error"))
        with srv._device_credential_lock:
            self.assertNotIn(srv._credential_key(ADDR_B), srv._device_credential_cache,
                             "a disproven slot must not keep being preferred")
        self.lab.accepts[ADDR_B] = PRIMARY            # fixed on the unit afterwards
        self.lab.ws_offers.clear()
        self.assertTrue(self.call(ADDR_B).get("result"))
        self.assertEqual(self.lab.ws_offers[0][3], "primary", "the next call starts again from the primary")

    def test_a_silent_unit_does_not_rotate_the_slot(self):
        self.call(ADDR_B)
        srv._ws_call = lambda ip, payload, timeout: None
        self.assertIsNone(self.call(ADDR_B))
        srv._ws_call = self.lab.ws
        self.lab.ws_offers.clear()
        self.call(ADDR_B)
        self.assertEqual([o[2:] for o in self.lab.ws_offers], [("ok", "fallback")])

    # -- HTTP paths -----------------------------------------------------------

    def test_slate_upload_reaches_a_fallback_unit(self):
        result = srv._slate_upload_one(ADDR_B, "slate.png", b"x", "image/png")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.lab.http_offers[-1][2:], (200, "fallback"))

    def test_slate_upload_is_not_retried_for_a_reason_that_is_not_the_credential(self):
        self.lab.http_status_override[ADDR_B] = 500
        result = srv._slate_upload_one(ADDR_B, "slate.png", b"x", "image/png")
        self.assertFalse(result["ok"])
        self.assertEqual({o[3] for o in self.lab.http_offers}, {"primary"},
                         "a 500 is not a credential refusal; the other password must not be tried")

    def test_slate_upload_remembers_the_credential(self):
        srv._slate_upload_one(ADDR_B, "slate.png", b"x", "image/png")
        self.lab.http_offers.clear()
        srv._slate_upload_one(ADDR_B, "slate.png", b"x", "image/png")
        self.assertEqual([o[2:] for o in self.lab.http_offers], [(200, "fallback")])

    def test_firmware_login_resolves_the_unit_credential(self):
        class Session:
            def __init__(self, outer):
                self.outer = outer

            def post(self, url, **kwargs):
                return self.outer.http_post(url, **kwargs)
        self.assertTrue(srv._login_and_session(ADDR_B, Session(self.lab)))
        self.assertEqual(self.lab.http_offers[-1][2:], (200, "fallback"))

    def test_firmware_login_is_not_retried_for_a_reason_that_is_not_the_credential(self):
        self.lab.http_status_override[ADDR_B] = 500

        class Session:
            def __init__(self, outer):
                self.outer = outer

            def post(self, url, **kwargs):
                return self.outer.http_post(url, **kwargs)
        self.assertFalse(srv._login_and_session(ADDR_B, Session(self.lab)))
        self.assertEqual(len(self.lab.http_offers), 1, "a 500 is not a credential refusal; no second password")

    def test_logo_upload_route_resolves_the_unit_credential(self):
        import producer2_routes
        with srv.APP.test_request_context("/"):
            response = producer2_routes._forward_file(ADDR_B, "logoImage", ("a.png", b"x", "image/png"), "/upload/logo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.lab.http_offers[-1][2:], (200, "fallback"))

    def test_an_explicit_upload_password_is_sent_once_and_verbatim(self):
        import producer2_routes
        with srv.APP.test_request_context("/"):
            response = producer2_routes._forward_file(ADDR_B, "logoImage", ("a.png", b"x", "image/png"),
                                                      "/upload/logo", password=PRIMARY)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.lab.http_offers), 1)

    # -- the Production Studio proxy ----------------------------------------------

    def producer(self, ip, params):
        client = srv.APP.test_client()
        return client.post("/api/producer/jsonrpc",
                           json={"ip": ip, "method": "Rtmp.Get", "params": params})

    def test_a_page_sending_the_configured_password_is_not_an_override(self):
        # The Reboot button sends the page's idea of the current password.
        allowed = next(iter(srv.PRODUCER_ALLOWED_METHODS))
        client = srv.APP.test_client()
        r = client.post("/api/producer/jsonrpc",
                        json={"ip": ADDR_B, "method": allowed, "params": {"password": PRIMARY}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.lab.ws_offers[-1][2:], ("ok", "fallback"))

    def test_a_genuinely_different_override_is_still_sent_verbatim_and_once(self):
        allowed = next(iter(srv.PRODUCER_ALLOWED_METHODS))
        client = srv.APP.test_client()
        client.post("/api/producer/jsonrpc",
                    json={"ip": ADDR_B, "method": allowed, "params": {"password": "something-else-entirely"}})
        self.assertEqual([o[2:] for o in self.lab.ws_offers], [("refused", "other")])

    # -- nothing is exposed --------------------------------------------------------

    def test_no_password_reaches_a_log_record(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        logging.getLogger().addHandler(handler)
        self.addCleanup(lambda: logging.getLogger().removeHandler(handler))
        old = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(lambda: logging.getLogger().setLevel(old))
        self.call(ADDR_B)
        srv._slate_upload_one(ADDR_B, "slate.png", b"x", "image/png")
        srv._login_and_session(ADDR_B, mock.Mock(post=lambda url, **k: self.lab.http_post(url, **k)))
        text = "\n".join(records)
        for secret in (PRIMARY, FALLBACK):
            self.assertNotIn(secret, text)

    def test_concurrent_calls_to_different_units_keep_their_own_credentials(self):
        failures = []

        def worker(address):
            for _ in range(25):
                if not self.call(address).get("result"):
                    failures.append(address)
        threads = [threading.Thread(target=worker, args=(a,)) for a in (ADDR_A, ADDR_B)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
