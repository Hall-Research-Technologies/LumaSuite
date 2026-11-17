"""Remote Access: enabling, HTTPS and certificates, restore at start-up, and what is never returned.

The listener is replaced by a recorder, so no socket is opened and nothing listens; what
is asserted is the decisions made before one would be: what is refused, what is started
with which TLS context, and what is persisted.
"""
import json
import os
import ssl
import stat
import unittest
from unittest import mock

from _support import USER_PASSWORD, System

import luma_adapters
import luma_remote
import luma_tls

ADAPTER = {"id": "adapter-one", "name": "Test Network", "address": "192.0.2.50", "mac": "02:00:5e:00:00:50"}
MOVED = dict(ADAPTER, address="192.0.2.60")


def started_recorder(system):
    """Replace RemoteListener.start with something that records and pretends."""
    calls = []

    def start(self, host, port, ssl_context=None, allowed_names=()):
        calls.append({"host": host, "port": port, "ssl_context": ssl_context})
        self._server = object()
        self._bound = (host, port)
        self._scheme = "https" if ssl_context is not None else "http"
        return {"ok": True, "enabled": True, "host": host, "port": port, "scheme": self._scheme,
                "secure": ssl_context is not None, "url": "%s://%s:%s/" % (self._scheme, host, port)}

    patcher = mock.patch.object(luma_remote.RemoteListener, "start", start)
    patcher.start()
    return calls, patcher


class Fixture(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.calls, patcher = started_recorder(self.s)
        self.addCleanup(patcher.stop)
        adapters = mock.patch.object(luma_adapters, "enumerate_adapters", lambda *a, **k: [dict(ADAPTER)])
        adapters.start()
        self.addCleanup(adapters.stop)
        self.s.admin("root")
        self.local = self.s.client(remote=False)

    def enable(self, **body):
        payload = {"enabled": True, "adapter_id": ADAPTER["id"]}
        payload.update(body)
        return self.local.post("/api/remote_access", json=payload)

    def generate(self, address=ADAPTER["address"]):
        return self.local.post("/api/admin/certificate", json={"action": "generate", "address": address})

    def preference(self):
        path = self.s.remote.preference_file
        return json.load(open(path, encoding="utf-8")) if path.exists() else None


class EnablingTests(Fixture):
    def test_it_is_off_until_an_administrator_turns_it_on(self):
        self.assertFalse(self.s.remote.enabled())
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.preference())

    def test_it_needs_an_administrator_account_first(self):
        other = System()
        self.addCleanup(other.close)
        self.assertEqual(other.client(remote=False).post("/api/remote_access", json={
            "enabled": True, "adapter_id": ADAPTER["id"]}).status_code, 409)

    def test_https_with_no_certificate_refuses_and_never_starts_in_the_clear(self):
        response = self.enable()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.s.remote.enabled())
        self.assertIn("certificate", response.get_json()["error"].lower())

    def test_the_refusal_says_a_certificate_is_the_missing_piece_so_the_page_can_offer_the_choice(self):
        body = self.enable().get_json()
        self.assertIs(body["needs_certificate"], True)
        self.assertEqual(body["reason"], "no_usable_certificate")
        self.assertFalse(body["certificate"].get("present"))

    def test_refusing_for_want_of_a_certificate_creates_nothing_and_changes_nothing(self):
        before = self.preference()
        self.enable()
        self.assertEqual(self.calls, [], "no listener of any kind")
        self.assertFalse(os.path.exists(str(self.s.remote.cert_file)), "no certificate was made silently")
        self.assertEqual(self.preference(), before, "no preference was saved, and none downgraded to HTTP")

    def test_an_unreadable_certificate_is_refused_the_same_way_and_never_replaced(self):
        with open(str(self.s.remote.cert_file), "w") as handle:
            handle.write("not a certificate")
        marker = open(str(self.s.remote.cert_file)).read()
        response = self.enable()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason"], "no_usable_certificate")
        self.assertEqual(self.calls, [])
        self.assertEqual(open(str(self.s.remote.cert_file)).read(), marker, "the file was left exactly as found")

    def test_the_choice_to_change_to_http_still_needs_the_explicit_acknowledgement(self):
        # "Change to HTTP" in the page is a selection, not consent: the request must still carry it.
        response = self.enable(transport="http")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.get_json()["needs_confirmation"])
        self.assertEqual(self.calls, [])

    def test_https_with_a_certificate_starts_with_a_tls_context(self):
        self.generate()
        response = self.enable()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.calls), 1)
        self.assertIsInstance(self.calls[0]["ssl_context"], ssl.SSLContext)
        self.assertEqual(self.calls[0]["host"], ADAPTER["address"])
        self.assertTrue(self.s.remote.enabled())

    def test_plain_http_cannot_be_reached_by_omitting_a_field(self):
        response = self.enable(transport="http")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.get_json()["needs_confirmation"])
        self.assertEqual(self.calls, [])

    def test_a_truthy_field_of_some_other_name_does_not_acknowledge_plain_http(self):
        for field in ("accept", "confirm", "insecure", "acknowledge", "accept_insecurely"):
            self.assertEqual(self.enable(transport="http", **{field: True}).status_code, 400, field)
        self.assertEqual(self.calls, [])

    def test_only_the_boolean_true_acknowledges_plain_http(self):
        # A string is not an acknowledgement, whatever it says: "false" and "no" are non-empty and
        # therefore truthy, and the old check let every one of them turn plaintext access on.
        for junk in ("yes", "false", "true", "no", "on", "1", 1, 2, 0, None, [], {}, [True], {"a": 1}, 1.0):
            response = self.enable(transport="http", accept_insecure=junk)
            self.assertEqual(response.status_code, 400, repr(junk))
            self.assertTrue(response.get_json()["needs_confirmation"], repr(junk))
        self.assertEqual(self.calls, [], "something other than true started a plain HTTP listener")
        self.assertFalse(self.s.remote.enabled())

    def test_the_field_absent_is_refused_too(self):
        response = self.enable(transport="http")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_the_boolean_true_is_accepted_and_nothing_else_is_needed(self):
        response = self.enable(transport="http", accept_insecure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0]["ssl_context"])

    def test_the_settings_page_sends_a_boolean(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "remote_settings.js"),
                      encoding="utf-8").read()
        self.assertIn("accept_insecure: transport === 'http'", source)

    def test_plain_http_with_the_explicit_acknowledgement_is_allowed_and_says_so(self):
        response = self.enable(transport="http", accept_insecure=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.calls[0]["ssl_context"])
        self.assertFalse(response.get_json()["secure"])

    def test_an_unknown_transport_reads_as_https(self):
        self.assertEqual(self.enable(transport="gopher").status_code, 400)       # https, no certificate
        self.assertEqual(self.calls, [])

    def test_the_network_is_chosen_by_identity_and_a_stale_address_in_the_body_is_ignored(self):
        self.generate()
        self.enable(address="203.0.113.99")
        self.assertEqual(self.calls[0]["host"], ADAPTER["address"])

    def test_a_missing_adapter_is_refused_with_a_sentence(self):
        response = self.enable(adapter_id="no-such-adapter")
        self.assertEqual(response.status_code, 400)
        self.assertIn("no longer available", response.get_json()["error"])

    def test_the_choice_is_remembered_by_adapter_identity(self):
        self.generate()
        self.enable()
        saved = self.preference()
        self.assertEqual((saved["enabled"], saved["adapter_id"], saved["transport"]),
                         (True, ADAPTER["id"], "https"))

    def test_turning_it_off_stops_the_listener_and_signs_everyone_out(self):
        self.generate()
        self.enable()
        self.s.user("alex")
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.assertTrue(self.s.remote.enabled())
        self.local.post("/api/remote_access", json={"enabled": False})
        self.assertFalse(self.s.remote.enabled())
        self.assertEqual(self.s.store.active_session_count(), 0)
        self.assertFalse(self.preference()["enabled"])

    def test_a_user_cannot_change_remote_access(self):
        self.generate()
        self.enable()
        self.s.user("alex")
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.assertEqual(self.s.post(client, "/api/remote_access", {"enabled": False}).status_code, 403)
        self.assertEqual(client.get("/api/remote_access").status_code, 403)
        self.assertTrue(self.s.remote.enabled())

    def test_enabling_is_audited(self):
        self.generate()
        self.enable()
        self.assertIn("remote_access.enabled", [e["event"] for e in self.s.store.audit_entries(limit=20)])


class CertificateTests(Fixture):
    def test_a_generated_certificate_names_the_address_it_was_made_for(self):
        body = self.generate().get_json()
        self.assertTrue(body["ok"])
        described = luma_tls.describe(str(self.s.remote.cert_file))
        self.assertTrue(luma_tls.covers(described, ADAPTER["address"]))
        self.assertFalse(luma_tls.covers(described, MOVED["address"]))

    def test_validity_is_decided_from_the_alternative_names_and_never_the_common_name(self):
        # A certificate whose common name matches and whose SAN does not is not valid for it.
        self.assertFalse(luma_tls.covers({"subject": "192.0.2.50", "san": ["192.0.2.99"]}, "192.0.2.50"))
        self.assertTrue(luma_tls.covers({"subject": "other", "san": ["192.0.2.50"]}, "192.0.2.50"))

    def test_a_certificate_whose_names_cannot_be_read_covers_nothing(self):
        for described in ({}, {"san": []}, {"readable": False}, {"subject": "192.0.2.50"}):
            self.assertFalse(luma_tls.covers(described, "192.0.2.50"), described)

    def test_the_private_key_is_never_returned_or_described(self):
        self.generate()
        for path in ("/api/remote_access", "/api/admin/certificate"):
            response = self.local.get(path) if path == "/api/remote_access" else \
                self.local.post(path, json={"action": "generate", "address": ADAPTER["address"]})
            text = response.get_data(as_text=True)
            self.assertNotIn("PRIVATE KEY", text)
            self.assertNotIn("BEGIN", text)
        described = luma_tls.describe(str(self.s.remote.cert_file))
        self.assertTrue({"fingerprint", "subject", "san"} <= set(described))
        self.assertFalse([k for k in described if "key" in k.lower() and k != "key_usage"])

    def test_the_private_key_file_is_narrowed_to_its_owner(self):
        self.generate()
        mode = os.stat(self.s.remote.key_file).st_mode
        if os.name != "nt":
            self.assertEqual(mode & (stat.S_IRWXG | stat.S_IRWXO), 0)

    def test_nothing_that_holds_a_key_is_written_to_the_preference_file(self):
        self.generate()
        self.enable()
        text = open(self.s.remote.preference_file, encoding="utf-8").read()
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("BEGIN", text)

    def test_the_tls_context_requires_at_least_tls_one_point_two(self):
        self.generate()
        context = luma_tls.load_context(str(self.s.remote.cert_file), str(self.s.remote.key_file))
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_a_missing_or_mismatched_or_encrypted_key_is_refused_with_its_own_reason(self):
        self.generate()
        cert, key = str(self.s.remote.cert_file), str(self.s.remote.key_file)
        with self.assertRaises(luma_tls.TlsError):
            luma_tls.load_context(cert, key + ".missing")
        other = System()
        self.addCleanup(other.close)
        other.client(remote=False).post("/api/admin/certificate", json={"action": "generate", "address": "192.0.2.77"})
        with self.assertRaises(luma_tls.TlsError):
            luma_tls.load_context(cert, str(other.remote.key_file))              # a different key
        notpem = os.path.join(self.s.dir, "bad.pem")
        open(notpem, "w").write("not a certificate")
        with self.assertRaises(luma_tls.TlsError):
            luma_tls.load_context(notpem, key)

    def test_a_certificate_made_for_another_address_is_refused_not_quietly_reissued(self):
        self.generate(address=MOVED["address"])
        before = luma_tls.describe(str(self.s.remote.cert_file))["fingerprint"]
        response = self.enable()
        body = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertTrue(body["needs_certificate"])
        self.assertIn(MOVED["address"], body["error"])
        self.assertIn(ADAPTER["address"], body["error"])
        self.assertEqual(luma_tls.describe(str(self.s.remote.cert_file))["fingerprint"], before)
        self.assertEqual(self.calls, [])

    def test_a_supplied_certificate_is_never_blocked_for_carrying_a_name_we_cannot_know(self):
        cert, key = os.path.join(self.s.dir, "site.pem"), os.path.join(self.s.dir, "site.key")
        luma_tls.generate_self_signed("203.0.113.99", cert, key)
        self.assertEqual(self.local.post("/api/admin/certificate", json={
            "action": "use", "cert_path": cert, "key_path": key}).status_code, 200)
        self.assertEqual(self.enable().status_code, 200)             # the site's own certificate

    def test_a_certificate_cannot_be_removed_while_it_is_serving(self):
        self.generate()
        self.enable()
        response = self.local.post("/api/admin/certificate", json={"action": "remove"})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(os.path.exists(self.s.remote.cert_file))

    def test_removing_deletes_only_our_own_pair_and_never_a_suppliers_files(self):
        self.generate()
        self.local.post("/api/admin/certificate", json={"action": "remove"})
        self.assertFalse(os.path.exists(self.s.remote.cert_file))
        self.assertFalse(os.path.exists(self.s.remote.key_file))
        cert, key = os.path.join(self.s.dir, "site.pem"), os.path.join(self.s.dir, "site.key")
        luma_tls.generate_self_signed("203.0.113.99", cert, key)
        self.local.post("/api/admin/certificate", json={"action": "use", "cert_path": cert, "key_path": key})
        self.local.post("/api/admin/certificate", json={"action": "remove"})
        self.assertTrue(os.path.exists(cert) and os.path.exists(key))


class RestoreAtStartupTests(Fixture):
    def prepare(self, **overrides):
        self.generate()
        self.enable()
        self.s.remote.listener.stop()
        self.calls.clear()
        saved = self.preference()
        saved.update(overrides)
        json.dump(saved, open(self.s.remote.preference_file, "w", encoding="utf-8"))

    def test_nothing_is_restored_unless_it_was_left_on(self):
        self.assertEqual(self.s.remote.restore(), {"restored": False, "reason": "not enabled"})
        self.assertEqual(self.calls, [])

    def test_an_administrator_must_exist(self):
        other = System()
        self.addCleanup(other.close)
        other.remote.save_preference(True, ADAPTER["address"], 18089, adapter_id=ADAPTER["id"])
        self.assertEqual(other.remote.restore()["reason"], "no administrator")
        self.assertIn("administrator", other.remote.restore_error)
        self.assertEqual(self.calls, [])

    def test_a_left_on_configuration_comes_back_with_the_same_security(self):
        self.prepare()
        result = self.s.remote.restore()
        self.assertTrue(result["restored"])
        self.assertEqual(self.calls[0]["host"], ADAPTER["address"])
        self.assertIsInstance(self.calls[0]["ssl_context"], ssl.SSLContext)

    def test_a_missing_adapter_is_a_status_never_a_second_adapter_and_never_a_failure(self):
        self.prepare()
        with mock.patch.object(luma_adapters, "enumerate_adapters", lambda *a, **k: []):
            result = self.s.remote.restore()
        self.assertFalse(result["restored"])
        self.assertEqual(self.calls, [])
        self.assertIn("not available", self.s.remote.restore_error)
        self.assertEqual(self.s.remote.last_choice()["restore_error"], self.s.remote.restore_error)

    def test_https_that_cannot_get_a_context_never_falls_back_to_plain_http(self):
        self.prepare()
        os.remove(self.s.remote.key_file)
        result = self.s.remote.restore()
        self.assertEqual(result["reason"], "tls unavailable")
        self.assertEqual(self.calls, [])

    def test_the_adapter_moving_is_followed_and_remembered(self):
        self.prepare()
        self.generate(address=MOVED["address"])               # a certificate for the new address exists
        with mock.patch.object(luma_adapters, "enumerate_adapters", lambda *a, **k: [dict(MOVED)]):
            result = self.s.remote.restore()
        self.assertTrue(result["restored"])
        self.assertEqual(self.calls[0]["host"], MOVED["address"])
        self.assertEqual(self.preference()["address"], MOVED["address"])

    def test_a_generated_certificate_for_the_old_address_is_not_served_on_the_new_one(self):
        self.prepare()
        with mock.patch.object(luma_adapters, "enumerate_adapters", lambda *a, **k: [dict(MOVED)]):
            result = self.s.remote.restore()
        self.assertEqual(result["reason"], "certificate address mismatch")
        self.assertEqual(self.calls, [])
        self.assertIn("Create a certificate for this network", self.s.remote.restore_error)

    def test_a_configuration_saved_with_only_an_address_is_adopted_only_when_one_adapter_owns_it(self):
        self.prepare(adapter_id="", adapter_name="")
        self.assertTrue(self.s.remote.restore()["restored"])
        self.prepare(adapter_id="", adapter_name="", address="203.0.113.5")
        self.assertFalse(self.s.remote.restore()["restored"])

    def test_a_damaged_preference_file_reads_as_off(self):
        open(self.s.remote.preference_file, "w", encoding="utf-8").write("{ not json")
        self.assertFalse(self.s.remote.last_choice()["enabled"])
        self.assertEqual(self.s.remote.restore()["reason"], "not enabled")
        self.assertTrue(os.path.exists(str(self.s.remote.preference_file) + ".corrupt"))

    def test_an_unusable_saved_address_is_refused(self):
        self.prepare(adapter_id="", adapter_name="", address="0.0.0.0")
        self.assertFalse(self.s.remote.restore()["restored"])
        self.assertEqual(self.calls, [])

    def test_restoring_is_audited_and_never_raises_into_start_up(self):
        self.prepare()
        self.s.remote.restore()
        self.assertIn("remote_access.restored", [e["event"] for e in self.s.store.audit_entries(limit=20)])


class ListenerAddressTests(unittest.TestCase):
    def test_only_a_concrete_routable_interface_address_may_be_bound(self):
        for bad in ("0.0.0.0", "127.0.0.1", "::", "::1", "224.0.0.1", "not an address", "", None, "240.0.0.1"):
            self.assertFalse(luma_remote.is_usable_remote_address(bad), repr(bad))
        for good in ("192.0.2.50", "198.51.100.7", "203.0.113.5"):
            self.assertTrue(luma_remote.is_usable_remote_address(good), good)


if __name__ == "__main__":
    unittest.main()
