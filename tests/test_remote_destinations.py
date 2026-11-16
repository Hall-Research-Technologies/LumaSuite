"""A remote browser must not be able to aim a stored device credential at a host.

The server attaches the device password to nearly every connection it makes, and a
couple of dozen handlers take the address from the request. A stolen administrator
session could therefore make LumaSuite forward the configured password to a
machine of the attacker's choosing.

Control needs an address the server already holds; discovery may probe an unseen
address only if it is not on the public Internet. Real application, isolated state,
network fenced, and the transport replaced by a recorder that is also the
assertion: a refused request must make no connection at all.
"""
import os
import re
import socket
import threading
import unittest

import _support
from _support import ADMIN_PASSWORD, RealApp, RealRemote, SOURCE_A, DISPLAY_1

import luma_destinations
import luma_users

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWN = SOURCE_A["ip"]
DISPLAY = DISPLAY_1["ip"]
OTHER = "192.0.2.77"


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.remote.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.local = self.m.APP.test_client()
        self.admin = self.remote.sign_in("root", ADMIN_PASSWORD)
        self.app.patch("_save_persisted_config", lambda: None)

        self.reached = []

        def tcp(ip, *args, **kwargs):
            self.reached.append("tcp:" + str(ip))
            return False

        def ws(url, *args, **kwargs):
            self.reached.append(str(url))
            raise OSError("fenced")

        def http(*args, **kwargs):
            self.reached.append("http:" + str(args[:1]))
            raise OSError("fenced")

        self.app.patch("_tcp_probe", tcp)
        self.app.patch("create_connection", ws)
        self.app.patch("_persist_units_cache", lambda: None)
        for name in ("post", "get"):
            patcher = self.app.mock.patch.object(self.m.requests, name, http)
            patcher.start()
            self.addCleanup(patcher.stop)

    def contacted(self):
        return list(self.reached) + [str(c) for c in self.app.devices.calls]

    def post(self, path, body):
        return self.remote.post(self.admin, path, body)


class ControlNeedsAKnownDeviceTests(Base):

    def test_a_known_device_is_reached(self):
        response = self.post("/api/version", {"ip": KNOWN})
        self.assertNotIn(response.status_code, (400, 404), response.get_data(as_text=True))
        self.assertTrue(any(KNOWN in call for call in self.contacted()), self.contacted())

    def test_an_address_the_server_has_never_seen_is_not(self):
        for path, body in (
                ("/api/version", {"ip": OTHER}),
                ("/api/hostname", {"ip": OTHER}),
                ("/api/set_hostname", {"ip": OTHER, "hostname": "a"}),
                ("/api/get_streamname", {"ip": OTHER}),
                ("/api/set_streamname", {"ip": OTHER, "name": "a"}),
                ("/api/reset", {"ip": OTHER}),
                ("/api/set_hdcp", {"ip": OTHER}),
                ("/api/set_dante_power", {"ip": OTHER}),
                ("/api/switcher_tune", {"ip": OTHER}),
                ("/api/producer/jsonrpc", {"ip": OTHER, "method": "Rtmp.Set"}),
                ("/api/producer/change_password",
                 {"ip": OTHER, "oldpassword": "x", "newpassword": "abcdef1"}),
                ("/api/ntp_profile", {"ips": [OTHER]}),
                ("/api/set_ntp", {"ips": [OTHER], "timezone": "UTC"}),
                ("/api/firmware_check", {"ips": [OTHER]}),
                ("/api/route_matrix", {"decoder_ip": OTHER, "encoder_ip": KNOWN}),
                ("/api/route_matrix", {"decoder_ip": DISPLAY, "encoder_ip": OTHER}),
                ("/api/upgrade", {"targets": [OTHER], "file": "f.bin"}),
        ):
            with self.subTest(path=path, body=body):
                del self.reached[:]
                del self.app.devices.calls[:]
                response = self.post(path, body)
                self.assertEqual(response.status_code, 404, response.get_data(as_text=True))
                self.assertEqual(response.get_json()["reason"], "unknown_device")
                self.assertEqual(self.contacted(), [], "a refusal must make no connection")

    def test_a_list_with_one_stranger_is_refused_whole(self):
        response = self.post("/api/ntp_profile", {"ips": [KNOWN, OTHER]})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.contacted(), [])

    def test_malformed_and_hostile_addresses_are_400_not_404(self):
        for bad in ("evil.example", "%s@evil.example" % KNOWN, "%s:80/x#" % KNOWN,
                    "[::1]", "::1", "ws://evil.example/", "0x7f000001",
                    "192.0.2.10/24", "192.0.2.256", "192.0.2"):
            with self.subTest(bad=bad):
                response = self.post("/api/version", {"ip": bad})
                self.assertEqual(response.status_code, 400, bad)
                self.assertEqual(response.get_json()["reason"], "invalid_address")
                self.assertEqual(self.contacted(), [])

    def test_loopback_and_friends_are_refused_unless_actually_recorded(self):
        for odd in ("127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0",
                    "255.255.255.255", "8.8.8.8"):
            with self.subTest(odd=odd):
                self.assertEqual(self.post("/api/version", {"ip": odd}).status_code, 404)
                self.assertEqual(self.contacted(), [])

    def test_a_device_that_moved_follows_the_inventory_not_the_old_address(self):
        unit = self.app.unit(KNOWN)
        with self.m.cache_lock:
            self.m.cache_units.pop(KNOWN)
            unit["ip"] = OTHER
            self.m.cache_units[OTHER] = unit
        self.assertNotEqual(self.post("/api/version", {"ip": OTHER}).status_code, 404)
        del self.reached[:]
        del self.app.devices.calls[:]
        self.assertEqual(self.post("/api/version", {"ip": KNOWN}).status_code, 404)
        self.assertEqual(self.contacted(), [])

    def test_an_emptied_inventory_reaches_nothing(self):
        with self.m.cache_lock:
            self.m.cache_units.clear()
        self.assertEqual(self.post("/api/version", {"ip": KNOWN}).status_code, 404)
        self.assertEqual(self.contacted(), [])

    def test_the_local_operator_is_unchanged(self):
        response = self.local.post("/api/version", json={"ip": OTHER})
        self.assertNotEqual(response.status_code, 404)
        self.assertTrue(any(OTHER in call for call in self.contacted()), self.contacted())

    def test_a_refusal_is_audited(self):
        self.post("/api/version", {"ip": OTHER})
        events = [e for e in self.remote.store._audit
                  if (e.get("event") or e.get("action")) == "access.denied"]
        self.assertTrue(any((e.get("detail") or {}).get("reason") == "unknown_device"
                            for e in events), list(self.remote.store._audit)[-3:])


class DiscoveryMayProbeOnlyNonPublicAddressesTests(Base):

    def test_a_remote_scan_of_the_internet_is_refused_before_any_probe(self):
        for targets in ("8.8.8.8", "1.1.1.0/30", "203.0.113.1,8.8.8.8", "8.8.8.1-3",
                        "127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0"):
            with self.subTest(targets=targets):
                response = self.post("/api/scan", {"targets": targets})
                self.assertEqual(response.status_code, 400, targets)
                self.assertEqual(response.get_json()["reason"], "address_out_of_scope")
                self.assertEqual(self.contacted(), [])

    def test_a_site_scan_is_still_allowed(self):
        for targets in ("198.51.100.5", "198.51.100.64/30", "203.0.113.1-3", "192.0.2.50"):
            with self.subTest(targets=targets):
                response = self.post("/api/scan", {"targets": targets})
                self.assertNotIn((response.get_json() or {}).get("reason"),
                                 ("address_out_of_scope", "invalid_address"),
                                 response.get_data(as_text=True))

    def test_an_oversized_expression_is_refused_quickly_not_expanded(self):
        # The old expansion built every host of a /8 before keeping 4096 of them.
        import time
        began = time.monotonic()
        response = self.post("/api/scan", {"targets": "8.0.0.0/8"})
        self.assertEqual(response.status_code, 400)
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertEqual(self.contacted(), [])

    def test_the_probe_routes_follow_the_same_scope(self):
        for path in ("/api/tcp_probe", "/api/ping", "/api/producer/test_device"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {"ip": "8.8.8.8"}).status_code, 400)
                self.assertEqual(self.post(path, {"ip": "127.0.0.1"}).status_code, 400)
        self.assertEqual(self.contacted(), [])


class ExpansionIsBoundedTests(unittest.TestCase):

    def test_a_wide_prefix_is_truncated_without_building_it(self):
        import time
        import lumaserver
        began = time.monotonic()
        expanded = lumaserver._expand_targets("10.0.0.0/8")
        self.assertEqual(len(expanded), 4096)
        self.assertLess(time.monotonic() - began, 2.0)

    def test_an_octet_range_beyond_255_is_not_expanded(self):
        import time
        import lumaserver
        began = time.monotonic()
        self.assertEqual(lumaserver._expand_targets("10.0.0.1-99999999"), [])
        self.assertLess(time.monotonic() - began, 2.0)


class PredicateTests(unittest.TestCase):

    @staticmethod
    def expand(text):
        import lumaserver
        return lumaserver._expand_targets(text)

    def run_check(self, rule, payload, known=(KNOWN,)):
        luma_destinations.check(rule, payload, lambda: set(known), self.expand)

    def refused(self, rule, payload, status, reason):
        with self.assertRaises(luma_destinations.Refusal) as caught:
            self.run_check(rule, payload)
        self.assertEqual((caught.exception.status, caught.exception.reason),
                         (status, reason))

    def test_control_known_unknown_malformed(self):
        self.run_check("/api/version", {"ip": KNOWN})
        self.refused("/api/version", {"ip": OTHER}, 404, "unknown_device")
        self.refused("/api/version", {"ip": "evil.example"}, 400, "invalid_address")

    def test_a_request_naming_no_address_does_not_read_the_inventory(self):
        def boom():
            raise AssertionError("inventory read for nothing")
        luma_destinations.check("/api/version", {"x": 1}, boom, lambda t: [])

    def test_a_known_address_may_be_rescanned_whatever_it_is(self):
        self.run_check("/api/scan", {"targets": "8.8.8.8"}, known=("8.8.8.8",))

    def test_the_scope_rule_by_address_without_any_probe(self):
        """Private and shared ranges are in scope; the public Internet and specials are not."""
        import ipaddress
        scope = luma_destinations.discovery_scope_ok
        for text in ("10.0.0.5", "172.16.4.9", "192.168.1.77", "100.64.0.9", "198.18.0.5",
                     "198.51.100.5", "203.0.113.9", "192.0.2.9"):
            self.assertTrue(scope(ipaddress.IPv4Address(text)), text)
        for text in ("8.8.8.8", "1.1.1.1", "127.0.0.1", "169.254.1.1", "224.0.0.1",
                     "0.0.0.0", "255.255.255.255", "240.0.0.1"):
            self.assertFalse(scope(ipaddress.IPv4Address(text)), text)

    def test_ipv6_is_refused(self):
        self.refused("/api/version", {"ip": "2001:db8::1"}, 400, "invalid_address")
        self.refused("/api/ping", {"ip": "fe80::1"}, 400, "invalid_address")


class AddressKeysCoverEveryHandlerTests(unittest.TestCase):
    """An endpoint added tomorrow that takes an address must be covered today."""

    NOT_A_DESTINATION = {
        "address": "the local adapter address Remote Access listens on; bound, never connected to",
        "hostname": "a name to set on a device already named by address",
        "html_url": "a GitHub release page, validated against its own host",
        "server": "an NTP server the DEVICE is told to use; this app never contacts it",
        "decoder_id": "the application's own device id (mac:..) of a saved-wall member, resolved to an address server-side; administrative route",
    }

    READ = re.compile(
        r"""(?:data|body|payload|request\.args|request\.form|args|form|req)"""
        r"""\.get\(\s*["']([A-Za-z_]+)["']""")

    def test_every_address_like_body_key_is_judged_or_explained(self):
        keys = set()
        for name in ("lumaserver.py", "producer2_routes.py", "luma_remote_api.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
                keys |= set(self.READ.findall(handle.read()))
        looks = {k for k in keys if re.search(
            r"ip|decoder|encoder|target|host|addr|device|server|url", k, re.I)}
        unaccounted = sorted(looks - luma_destinations.ADDRESS_KEYS
                             - set(self.NOT_A_DESTINATION))
        self.assertEqual(unaccounted, [],
                         "a handler reads a key that may name a device and "
                         "luma_destinations does not know it")


class NoRedirectCarriesACredentialTests(unittest.TestCase):

    def test_a_websocket_redirect_is_not_followed(self):
        import lumaserver
        target = socket.socket()
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        target.settimeout(2.0)
        reached = []

        def accept_target():
            try:
                conn, _ = target.accept()
                reached.append(True)
                conn.close()
            except OSError:
                pass

        front = socket.socket()
        front.bind(("127.0.0.1", 0))
        front.listen(1)
        front.settimeout(3)

        def redirect():
            try:
                conn, _ = front.accept()
                conn.recv(4096)
                conn.sendall(("HTTP/1.1 302 Found\r\nLocation: ws://127.0.0.1:%d/\r\n"
                              "Content-Length: 0\r\n\r\n"
                              % target.getsockname()[1]).encode())
                conn.close()
            except OSError:
                pass

        threads = [threading.Thread(target=accept_target, daemon=True),
                   threading.Thread(target=redirect, daemon=True)]
        for t in threads:
            t.start()
        try:
            try:
                ws = lumaserver.create_connection(
                    "ws://127.0.0.1:%d/" % front.getsockname()[1], timeout=2.0,
                    redirect_limit=0)
                ws.close()
            except Exception:
                pass
            for t in threads:
                t.join(3)
        finally:
            target.close()
            front.close()
        self.assertEqual(reached, [])

    def test_every_device_connection_in_the_source_refuses_redirects(self):
        ws = re.compile(r"(?<![\w.])create_connection\([^\n]*")
        http = re.compile(r"requests\.(?:post|put)\([^\n]*")
        for name in ("lumaserver.py", "producer2_routes.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
                text = handle.read()
            for call in ws.findall(text):
                if "socket." in call:
                    continue
                self.assertIn("redirect_limit=0", call, name + ": " + call)
            for call in http.findall(text):
                self.assertIn("allow_redirects=False", call, name + ": " + call)


if __name__ == "__main__":
    unittest.main()
