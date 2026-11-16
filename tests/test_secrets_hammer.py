"""Credentials in, credentials nowhere else.

A long sequence of the operations that handle a secret -- sign-ins that fail and succeed, the
throttle, a password change, Direct Access (enable, open, regenerate, old link, new link), a
backup and a restore, an account deleted under an open session -- is run against the real
application, and then everything the application wrote or said is searched for the secrets that
went in:

  * its log output           * the activity record        * every file in its data folder
  * every response body      * every response header (apart from the cookie that is the point)

A password or passphrase may appear nowhere. A session token, a CSRF token and a Direct Access
token may appear only where handing them over is the whole purpose: the sign-in answer, the
cookie, and the link answer. Not in a log line, not in the activity record, not in a file.

Real application, isolated state, network fenced.
"""
import io
import json
import logging
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import _support
from _support import ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, SOURCE_A, RealApp, RealRemote, identity

import luma_backup
import luma_users

A, D1 = identity(SOURCE_A), identity(DISPLAY_1)
PASSPHRASE = "backup passphrase for the hammer"
NEW_PASSWORD = "a brand new passphrase!!"
WRONG = ["wrong-password-one", "wrong-password-two", "guess guess guess"]


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.lines.append(str(record.msg))


class SecretsHammerTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.store = self.remote.store
        self.local = self.m.APP.test_client()
        self.names = [section.filename for section in luma_backup.SECTIONS]
        self._originals = {}
        for name in self.names:
            path = os.path.join(self.m.DATA_DIR, name)
            if os.path.exists(path):
                self._originals[name] = open(path, "rb").read()
        self.addCleanup(self._put_back)
        snap = tempfile.mkdtemp(prefix="luma-hammer-")
        self.addCleanup(shutil.rmtree, snap, True)
        patcher = mock.patch.object(self.m, "BACKUP_SNAPSHOT_DIR", snap)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: (setattr(self.m, "_restore_in_progress", False),
                                 setattr(self.m, "_restart_required", False)))
        self.capture = Capture()
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, self._level)
        self.bodies = []        # (method, path, status, text, headers)
        real_open = __import__("werkzeug.test", fromlist=["Client"]).Client.open
        recorder = self

        def recording(client, *args, **kwargs):
            response = real_open(client, *args, **kwargs)
            try:
                request_path = response.request.path
                recorder.bodies.append((request_path, response.status_code,
                                        response.get_data(as_text=True),
                                        dict(response.headers)))
            except Exception:
                pass
            return response
        patcher2 = mock.patch("werkzeug.test.Client.open", recording)
        patcher2.start()
        self.addCleanup(patcher2.stop)

        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.tokens = set()      # session and csrf tokens, learned as they are issued

    def _put_back(self):
        for name in self.names:
            path = os.path.join(self.m.DATA_DIR, name)
            if name in self._originals:
                with open(path, "wb") as handle:
                    handle.write(self._originals[name])
            elif os.path.exists(path):
                os.remove(path)

    # ---- helpers -----------------------------------------------------
    def remote_client(self, address=None):
        # Each client is its own address unless the scenario means them to share one: the
        # throttle is per address, and the failures below are meant to meet it.
        self._next = getattr(self, "_next", 0) + 1
        client = self.m.APP.test_client()
        client.environ_base["luma.listener"] = "remote"
        client.environ_base["REMOTE_ADDR"] = address or "198.18.1.%d" % self._next
        return client

    def login(self, name, password, address=None):
        client = self.remote_client(address)
        r = client.post("/api/auth/login", json={"username": name, "password": password})
        if r.status_code == 200:
            body = r.get_json()
            client.csrf = body["csrf_token"]
            self.tokens.add(body["csrf_token"])
        return client, r

    def admin_post(self, path, body):
        return self.local.post(path, json=body)

    def user_id(self, name):
        return next(u["id"] for u in self.store.list_accounts() if u["username"] == name)

    def run_the_sequence(self):
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER,
                          luma_users.Permissions([A], [D1]))
        # Failures, then a success.
        for guess in WRONG:
            self.login("alex", guess, address="198.18.9.9")
        self.login("nobody-here", WRONG[0], address="198.18.9.9")
        client, ok = self.login("alex", USER_PASSWORD)
        self.assertEqual(ok.status_code, 200)
        # Throttle: many more failures from one address, until it answers 429.
        throttled = []
        for i in range(12):
            throttled.append(self.login("alex", "spam-%d" % i, address="198.18.9.9")[1].status_code)
        self.assertIn(429, throttled, "the sequence is meant to meet the throttle")
        # Password change, through the administrator.
        self.admin_post("/api/admin/users/" + self.user_id("alex"),
                        {"action": "set_password", "password": NEW_PASSWORD})
        # Direct Access.
        first = self.admin_post("/api/admin/users/%s/direct_access" % self.user_id("alex"),
                                {"action": "enable"}).get_json()
        self.link_one = first["link"]
        second = self.admin_post("/api/admin/users/%s/direct_access" % self.user_id("alex"),
                                 {"action": "regenerate"}).get_json()
        self.link_two = second["link"]
        for link in (self.link_one, self.link_two):
            c = self.remote_client()
            c.get("/access/" + link.rsplit("/", 1)[1])
        self.direct_tokens = {self.link_one.rsplit("/", 1)[1], self.link_two.rsplit("/", 1)[1]}
        # A backup and a restore.
        self.m._save_persisted_config()
        blob = self.local.post("/api/backup/export", json={"passphrase": PASSPHRASE}).data
        self.local.post("/api/backup/restore", content_type="multipart/form-data",
                        data={"backup": (io.BytesIO(blob), "b.lumabak"), "passphrase": PASSPHRASE})
        # A wrong backup passphrase.
        self.local.post("/api/backup/inspect", content_type="multipart/form-data",
                        data={"backup": (io.BytesIO(blob), "b.lumabak"), "passphrase": WRONG[1]})
        # An account deleted under an open session.
        c2, _ = self.login("alex", NEW_PASSWORD)
        self.admin_post("/api/admin/users/" + self.user_id("alex"), {"action": "delete"})
        c2.get("/api/user/state")
        with self.store._lock:
            self.tokens.update(self.store._sessions.keys())
            self.tokens.update(s.csrf_token for s in self.store._sessions.values())

    def data_files(self):
        out = {}
        for folder, _dirs, files in os.walk(self.m.DATA_DIR):
            for name in files:
                path = os.path.join(folder, name)
                try:
                    out[path] = open(path, "rb").read().decode("utf-8", "replace")
                except OSError:
                    pass
        return out

    # ---- the hunt ------------------------------------------------------
    def test_no_password_or_passphrase_is_written_or_said_anywhere(self):
        self.run_the_sequence()
        passwords = [ADMIN_PASSWORD, USER_PASSWORD, NEW_PASSWORD, PASSPHRASE] + WRONG + \
                    ["spam-%d" % i for i in range(12)]
        audit = json.dumps(self.store.audit_entries(limit=5000), default=str)
        logs = "\n".join(self.capture.lines)
        files = self.data_files()
        responses = "\n".join("%s %s %s %s" % (p, s, t, h) for p, s, t, h in self.bodies)
        for secret in passwords:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, logs, "a log line")
                self.assertNotIn(secret, audit, "the activity record")
                for path, text in files.items():
                    if os.path.basename(path) == "producer_config.json":
                        continue        # the device sign-in, which is a different secret
                    self.assertNotIn(secret, text, path)
                self.assertNotIn(secret, responses, "a response")

    def test_no_token_is_written_where_it_is_not_the_point(self):
        self.run_the_sequence()
        tokens = set(self.tokens) | set(self.direct_tokens)
        self.assertTrue(tokens)
        audit = json.dumps(self.store.audit_entries(limit=5000), default=str)
        logs = "\n".join(self.capture.lines)
        for token in tokens:
            with self.subTest(token=token[:6] + "..."):
                self.assertNotIn(token, logs, "a log line")
                self.assertNotIn(token, audit, "the activity record")
                for path, text in self.data_files().items():
                    self.assertNotIn(token, text, path)

    def test_a_direct_access_token_leaves_only_in_the_link_answer(self):
        self.run_the_sequence()
        for token in self.direct_tokens:
            carriers = [p for p, s, t, h in self.bodies if token in t or token in str(h)]
            for path in carriers:
                self.assertTrue(
                    "direct_access" in path or path.startswith("/access/"),
                    "the token came back from %s" % path)

    def test_the_stored_link_is_not_the_token(self):
        self.run_the_sequence()
        text = self.data_files().get(os.path.join(self.m.DATA_DIR, "users.json"), "")
        for token in self.direct_tokens:
            self.assertNotIn(token, text)

    def test_no_response_carries_a_traceback_or_an_internal_path(self):
        self.run_the_sequence()
        for path, status, text, _headers in self.bodies:
            with self.subTest(path=path, status=status):
                self.assertNotIn("Traceback (most recent call last)", text)
                self.assertNotRegex(text, r"[A-Za-z]:\\\\Users\\\\")
        statuses = {s for _p, s, _t, _h in self.bodies}
        self.assertFalse({s for s in statuses if s >= 500} - {503},
                         "an unexpected server error during the sequence: %s" % sorted(statuses))

    def test_the_session_cookie_is_the_only_header_carrying_a_session(self):
        self.run_the_sequence()
        for path, _status, _text, headers in self.bodies:
            for name, value in headers.items():
                if name.lower() == "set-cookie":
                    self.assertIn("HttpOnly", value, path)
                    self.assertIn("SameSite", value, path)
                else:
                    for token in self.tokens:
                        self.assertNotIn(token, str(value), "%s carried a token in %s" % (path, name))

    def test_the_launcher_keeps_request_lines_out_of_the_log(self):
        # The request line of /access/<token> contains the token. The launcher sets the
        # werkzeug logger to ERROR, which silences the line; this holds that decision in place.
        source = open(os.path.join(os.path.dirname(__file__), "..", "app_launcher.py"),
                      encoding="utf-8").read()
        code = re.sub(r"(?m)^\s*#.*$", "", source)
        self.assertRegex(code, r"getLogger\(['\"]werkzeug['\"]\)\s*\n\s*log\.setLevel\(logging\.ERROR\)")


if __name__ == "__main__":
    unittest.main()
