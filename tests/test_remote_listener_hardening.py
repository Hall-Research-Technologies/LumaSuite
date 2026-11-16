"""The network-facing listener answers only to its own names, says less, and sends safer headers.

Proven over real sockets on loopback with the real wrapper, request handler and (for HTTPS) a real generated
certificate: a Host header that is not this listener's is 421 whatever the path; the advertised address,
the machine name, `localhost` and the names a supplied certificate carries work, over HTTP and HTTPS; the
`Server` header names neither the framework nor the interpreter on either listener; and remote responses carry
nosniff, frame denial and no-referrer without a Content-Security-Policy that would break the pages.
"""
import http.client
import os
import socket
import ssl
import tempfile
import threading
import unittest

import _support
from _support import RealApp, RealRemote

import luma_remote
import luma_tls


def serve(testcase, app, bound_host="192.0.2.50", names=(), context=None):
    """A remote-tagged server on loopback that believes it is bound to `bound_host`:port."""
    from werkzeug.serving import make_server
    holder = {}

    def check(header):
        return luma_remote.host_allowed(header, (bound_host, holder["port"]), names)

    server = make_server("127.0.0.1", 0, luma_remote._ListenerTag(app, luma_remote.REMOTE, check),
                         threaded=True, ssl_context=context, request_handler=luma_remote.quiet_handler())
    holder["port"] = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    testcase.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(5)))
    return server.server_port


def request(port, host_header, path="/api/auth/session", tls=False, method="GET", body=None):
    if tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=5, context=context)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    if host_header is not None:
        conn.putheader("Host", host_header)
    if body is not None:
        conn.putheader("Content-Length", str(len(body)))
        conn.putheader("Content-Type", "application/json")
    conn.endheaders(body)
    response = conn.getresponse()
    data = response.read()
    out = (response.status, dict((k.lower(), v) for k, v in response.getheaders()), data)
    conn.close()
    return out


class HostTableTests(unittest.TestCase):
    BOUND = ("192.0.2.50", 29901)

    def allowed(self, header, names=()):
        return luma_remote.host_allowed(header, self.BOUND, names)

    def test_the_advertised_address_and_the_computers_own_names_are_served(self):
        me = socket.gethostname().lower()
        # A hosted runner's own name may already carry dots (a macOS runner's ends in .local), and the
        # application derives the .local form from the first label, so the test does the same.
        short = me.split(".")[0]
        for header in ("192.0.2.50:29901", "192.0.2.50", "localhost:29901", "LOCALHOST", "127.0.0.1:29901",
                       "[::1]:29901", me, me + ":29901", short + ".local:29901", me.upper() + ":29901"):
            self.assertTrue(self.allowed(header), header)

    def test_anything_else_is_not(self):
        for header in (None, "", "evil.example", "evil.example:29901", "192.0.2.51:29901", "192.0.2.50:29902",
                       "192.0.2.50.evil.example", "localhost.evil.example", "127.0.0.1.evil.example",
                       "192.0.2.50:abc", "[192.0.2.50", " ", "192.0.2.50:29901:1", "0.0.0.0:29901"):
            self.assertFalse(self.allowed(header), repr(header))

    def test_the_names_a_supplied_certificate_carries_are_served(self):
        self.assertFalse(self.allowed("av.site.example:29901"))
        self.assertTrue(self.allowed("av.site.example:29901", ["av.site.example", "192.0.2.50"]))
        self.assertTrue(self.allowed("AV.SITE.EXAMPLE", ["av.site.example"]))
        self.assertFalse(self.allowed("other.site.example", ["av.site.example"]))


class OverSocketsTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.remote = RealRemote(self, self.app)
        self.m = self.app.m
        self.me = socket.gethostname().lower()

    def test_plain_http_serves_its_own_address_and_refuses_a_foreign_host_with_421(self):
        port = serve(self, self.m.APP)
        for host in ("192.0.2.50:%d" % port, "localhost:%d" % port, self.me + ":%d" % port):
            status, _headers, _body = request(port, host, "/login")
            self.assertEqual(status, 200, host)
        for host in ("evil.example", "evil.example:%d" % port, "192.0.2.51:%d" % port, None):
            for path in ("/login", "/api/auth/session", "/", "/ui/matrix.html"):
                status, headers, _body = request(port, host, path)
                self.assertEqual(status, 421, (host, path))
                self.assertEqual(headers.get("cache-control"), "no-store")

    def test_https_listener_with_a_real_generated_certificate_serves_the_same_way(self):
        folder = tempfile.mkdtemp(prefix="luma-hardening-")
        cert, key = os.path.join(folder, "c.pem"), os.path.join(folder, "k.pem")
        luma_tls.generate_self_signed("192.0.2.50", cert, key)
        context = luma_tls.load_context(cert, key)
        names = luma_tls.describe(cert).get("san") or []
        self.assertIn("192.0.2.50", names)
        port = serve(self, self.m.APP, names=names, context=context)
        status, headers, _ = request(port, "192.0.2.50:%d" % port, "/login", tls=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        status, _h, _b = request(port, "evil.example", "/login", tls=True)
        self.assertEqual(status, 421)

    def test_server_header_names_neither_the_framework_nor_the_interpreter_on_either_listener(self):
        port = serve(self, self.m.APP)
        _s, headers, _b = request(port, "192.0.2.50:%d" % port, "/login")
        self.assertEqual(headers.get("server"), "LumaSuite")
        server = luma_remote.serve_tagged("127.0.0.1", 0, self.m.APP, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(5)))
        status, headers, _b = request(server.server_port, "127.0.0.1:%d" % server.server_port, "/login")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("server"), "LumaSuite")
        for value in headers.values():
            self.assertNotRegex(value, r"(?i)werkzeug|python/")

    def test_remote_responses_carry_the_hardening_headers_and_no_csp(self):
        port = serve(self, self.m.APP)
        for path in ("/login", "/api/auth/session", "/ui/matrix.html"):
            status, headers, _b = request(port, "192.0.2.50:%d" % port, path)
            self.assertIn(status, (200, 302, 401, 403, 404), path)
            self.assertEqual(headers.get("x-content-type-options"), "nosniff", path)
            self.assertEqual(headers.get("x-frame-options"), "DENY", path)
            self.assertEqual(headers.get("referrer-policy"), "no-referrer", path)
            self.assertNotIn("content-security-policy", headers, path)

    def test_local_responses_are_not_given_frame_denial_they_did_not_have(self):
        response = self.m.APP.test_client().get("/api/app_version")
        self.assertNotIn("X-Frame-Options", response.headers)

    def test_a_malformed_body_over_the_real_listener_is_400_with_a_plain_sentence(self):
        port = serve(self, self.m.APP)
        status, _h, body = request(port, "192.0.2.50:%d" % port, "/api/auth/login", method="POST", body=b"5")
        self.assertEqual(status, 400)
        self.assertIn(b"JSON object", body)


if __name__ == "__main__":
    unittest.main()
