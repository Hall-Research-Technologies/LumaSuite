"""The local listener answers only to its own address.

The local listener trusts whoever reaches it, because only this computer can. A
web page the operator visits can still reach it: after the page has loaded, its
own name is made to resolve to 127.0.0.1 (DNS rebinding) and the page then reads
and writes as the local operator -- device passwords, a backup export, a restore --
from a request whose Host header is that page's name. Nothing in the product
looked at Host.

These run a real listener on loopback with a trivial application behind it, and
send it real requests with the Host header the attack would carry.
"""
from __future__ import annotations

import http.client
import os
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _support                                           # noqa: E402,F401  (the fence)
import luma_remote                                       # noqa: E402


def app(environ, start_response):
    body = b"APP-RAN-OK"
    start_response("200 OK", [("Content-Type", "text/plain"),
                              ("Content-Length", str(len(body)))])
    return [body]


class HostHeaderTable(unittest.TestCase):

    def test_names_for_this_computer_are_accepted(self):
        for value in ("127.0.0.1", "127.0.0.1:8080", "localhost", "LOCALHOST:8080",
                      "localhost.", "[::1]", "[::1]:8080", "127.5.4.3:80"):
            with self.subTest(value=value):
                self.assertTrue(luma_remote.is_loopback_host_header(value))

    def test_anything_else_is_refused(self):
        for value in ("attacker.invalid", "attacker.invalid:8080", "198.51.100.7",
                      "198.51.100.7:8080", "0.0.0.0", "[2001:db8::1]:80",
                      "localhost.attacker.invalid", "127.0.0.1.attacker.invalid",
                      "evil-localhost", "localhost:8080:80", "", "   ", None,
                      "[::1", "[]", "127.0.0.1@attacker.invalid"):
            with self.subTest(value=value):
                self.assertFalse(luma_remote.is_loopback_host_header(value))


class RealListenerTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.servers = {}
        for tag in (luma_remote.LOCAL, luma_remote.REMOTE):
            from werkzeug.serving import make_server
            server = make_server("127.0.0.1", 0, luma_remote._ListenerTag(app, tag))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.servers[tag] = server

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers.values():
            server.shutdown()
            server.server_close()

    def get(self, tag, host):
        conn = http.client.HTTPConnection("127.0.0.1", self.servers[tag].server_port,
                                          timeout=10)
        try:
            conn.putrequest("GET", "/", skip_host=True)
            if host is not None:
                conn.putheader("Host", host)
            conn.endheaders()
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_the_local_listener_serves_its_own_address(self):
        port = self.servers[luma_remote.LOCAL].server_port
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", "localhost"):
            with self.subTest(host=host):
                self.assertEqual(self.get(luma_remote.LOCAL, host)[0], 200)

    def test_a_rebound_name_is_refused_on_the_local_listener(self):
        port = self.servers[luma_remote.LOCAL].server_port
        for host in (f"attacker.invalid:{port}", "attacker.invalid",
                     f"198.51.100.7:{port}"):
            with self.subTest(host=host):
                status, body = self.get(luma_remote.LOCAL, host)
                self.assertEqual(status, 421)
                self.assertNotIn(b"APP-RAN-OK", body, "the application must not run")

    def test_the_remote_listener_is_not_subject_to_it(self):
        """It is reached by name and address from other computers, and it asks
        for a sign-in instead of trusting where the request came from."""
        status, _ = self.get(luma_remote.REMOTE, "omnisuite.example.test")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
