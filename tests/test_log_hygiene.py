"""A credential sent to an endpoint never appears in the log.

Audit logging names the fields it records and never dumps a request body: a verbatim dump logs whatever a
caller sends, including a credential added to that request later. `/api/scan` logged the whole body and
`/api/upgrade` the whole dict, which carries the device password. Driven by the route table: every JSON POST
route is sent a body in which every credential-like field, and a nested copy, holds a canary, with logging at
DEBUG, and the canary may not appear in any record from any logger.
"""
import logging
import re
import unittest

import _support
from _support import ADMIN_PASSWORD, RealApp, RealRemote

import luma_users

CANARY = "CANARY-pw-7731-xyzzy"
CREDENTIAL_FIELDS = ("password", "newpassword", "oldpassword", "fallback_password", "passphrase", "token",
                     "secret", "authorization", "csrf_token")


class Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
            if record.exc_info and record.exc_info[1] is not None:
                self.lines.append(repr(record.exc_info[1]))
        except Exception as exc:          # a formatting fault is itself a finding
            self.lines.append("LOGGING-FAULT %r" % (exc,))


class LogHygieneTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.remote.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.handler = Collect()
        root = logging.getLogger()
        self.previous = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.handler)
        self.addCleanup(lambda: (root.removeHandler(self.handler), root.setLevel(self.previous)))

    def routes(self):
        out = set()
        for rule in self.m.APP.url_map.iter_rules():
            if "POST" in rule.methods and rule.endpoint != "static":
                out.add(re.sub(r"<[^>]+>", "nope", rule.rule))
        return sorted(out)

    def body(self):
        fields = {name: CANARY for name in CREDENTIAL_FIELDS}
        fields.update({"ip": "192.0.2.10", "ips": ["192.0.2.10"], "targets": ["192.0.2.0/30"],
                       "firmware": "x.tar.gz", "file": "x.tar.gz", "username": "admin", "method": "System.Get",
                       "params": {"password": CANARY}, "data": {"password": CANARY}})
        return fields

    def test_no_route_logs_a_credential_it_was_sent(self):
        local = self.m.APP.test_client()
        count = 0
        for path in self.routes():
            if path in ("/api/auth/login", "/api/auth/logout", "/api/bootstrap/admin"):
                continue        # these take the credential on purpose; checked separately below
            local.post(path, json=self.body())
            count += 1
        self.assertGreater(count, 55)
        leaked = [line for line in self.handler.lines if CANARY in line]
        self.assertEqual(leaked, [], "a credential reached the log")

    def test_a_failed_sign_in_does_not_log_the_password_tried(self):
        client = self.m.APP.test_client()
        client.environ_base["luma.listener"] = "remote"
        for name in ("root", "nobody"):
            client.post("/api/auth/login", json={"username": name, "password": CANARY})
        self.assertEqual([l for l in self.handler.lines if CANARY in l], [])

    def test_scan_and_upgrade_log_field_names_not_the_body(self):
        local = self.m.APP.test_client()
        local.post("/api/scan", json={"targets": "192.0.2.0/30", "password": CANARY})
        local.post("/api/upgrade", json={"targets": [], "firmware": "x", "password": CANARY})
        text = "\n".join(self.handler.lines)
        self.assertNotIn(CANARY, text)
        self.assertRegex(text, r"\[api_scan\] fields received")
        self.assertRegex(text, r"\[api_upgrade\] fields received")

    def test_no_handler_logs_a_whole_request_body_by_construction(self):
        import os
        pattern = re.compile(r"log(?:ging)?\.\w+\(\s*f?[\"'][^\"']*\{(?:data|body|payload)\}")
        offenders = []
        for module in ("lumaserver.py", "luma_remote_api.py", "luma_gate.py", "producer2_routes.py"):
            with open(os.path.join(_support.ROOT, module), encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if pattern.search(line) or re.search(r"log(?:ging)?\.\w+\([^)]*request\.(?:get_json|data|form)", line):
                        offenders.append("%s:%d" % (module, number))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
