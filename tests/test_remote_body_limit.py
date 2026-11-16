"""The network-facing listener does not let a stranger choose how much body it reads.

The gate parses a request's body before it decides whether the caller may make the request:
it needs the form to know which device a request names. Werkzeug bounds a urlencoded body only
by the content-length limit and spools a multipart file part to disk, so with no limit an
unauthenticated caller chose the memory and disk spent. Measured on the old stack (Werkzeug
2.3.8): a 100 MB urlencoded body cost ~200 MB of resident memory and a 200 MB multipart text
field ~500 MB, both before the 401. The limit is set per request, on the remote listener only,
before the body is touched. The local listener is not limited (a backup restore from the local
computer is unchanged).

A tiny application behind the real gate and a real, empty user store; nothing here is a device.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _support                                           # noqa: E402,F401  (the fence)
from flask import Flask, jsonify, request                 # noqa: E402

import luma_gate                                          # noqa: E402
import luma_users                                         # noqa: E402

#: The contract written out, not read from the constant under test.
LIMIT = 16 * 1024 * 1024
OVER = LIMIT + 1024


class Rig:
    def __init__(self, enabled=True):
        folder = Path(tempfile.mkdtemp(prefix="luma-bodylimit-"))
        self.store = luma_users.UserStore(folder / "users.json", folder / "audit.json")
        self.app = Flask(__name__)
        self.reached = []

        @self.app.post("/api/clear_units")
        def clear_units():                    # a classified, administrative, state-changing rule
            self.reached.append(len(request.get_data() or b""))
            return jsonify({"ok": True})

        luma_gate.install(self.app, lambda: self.store, lambda: enabled)

    def client(self, listener):
        client = self.app.test_client()
        client.environ_base["luma.listener"] = listener
        return client


def padded(kind, size):
    if kind == "urlencoded":
        return dict(data=b"x=" + b"A" * size, content_type="application/x-www-form-urlencoded")
    if kind == "json":
        return dict(data=b'{"x":"' + b"A" * size + b'"}', content_type="application/json")
    if kind == "multipart-field":
        return dict(data={"x": "A" * size})
    if kind == "multipart-file":
        return dict(data={"f": (io.BytesIO(b"A" * size), "a.bin")}, content_type="multipart/form-data")
    raise AssertionError(kind)


class RemoteBodyLimit(unittest.TestCase):

    def test_the_limit_is_what_the_contract_says(self):
        self.assertEqual(luma_gate.REMOTE_MAX_BODY_BYTES, LIMIT)
        self.assertGreater(LIMIT, 10 * 1024 * 1024, "a 1920x1080 slate image must still fit")

    def test_an_oversized_remote_body_is_refused_before_anything_reads_it(self):
        for kind in ("urlencoded", "json", "multipart-field", "multipart-file"):
            with self.subTest(kind=kind):
                rig = Rig()
                response = rig.client("remote").post("/api/clear_units", **padded(kind, OVER))
                self.assertEqual(response.status_code, 413, "%s: %r" % (kind, response.get_data()[:200]))
                body = response.get_json()
                self.assertEqual(body["reason"], "too_large")
                self.assertFalse(body["ok"])
                self.assertEqual(rig.reached, [])

    def test_the_refusal_is_the_same_whether_or_not_remote_access_is_on(self):
        # With remote access off the gate refuses (404) before it reads a body at all, so
        # nothing is parsed either way; this is the other half of "nothing reads it".
        rig = Rig(enabled=False)
        response = rig.client("remote").post("/api/clear_units", **padded("urlencoded", OVER))
        self.assertEqual(response.status_code, 404)

    def test_a_small_remote_body_still_gets_the_gates_own_answer(self):
        for kind in ("urlencoded", "json", "multipart-field", "multipart-file"):
            with self.subTest(kind=kind):
                rig = Rig()
                response = rig.client("remote").post("/api/clear_units", **padded(kind, 1000))
                self.assertEqual(response.status_code, 401, response.get_data()[:200])
                self.assertEqual(rig.reached, [])

    def test_a_slate_sized_remote_body_is_not_refused_for_its_size(self):
        rig = Rig()
        response = rig.client("remote").post("/api/clear_units", **padded("multipart-file", 12 * 1024 * 1024))
        self.assertNotEqual(response.status_code, 413)

    def test_the_local_listener_is_not_limited(self):
        for kind in ("urlencoded", "multipart-file"):
            with self.subTest(kind=kind):
                rig = Rig()
                response = rig.client("local").post("/api/clear_units", **padded(kind, OVER))
                self.assertEqual(response.status_code, 200, response.get_data()[:200])
                self.assertEqual(len(rig.reached), 1)

    def test_the_limit_is_set_on_the_request_not_the_application(self):
        rig = Rig()
        self.assertIsNone(rig.app.config.get("MAX_CONTENT_LENGTH"))
        rig.client("remote").post("/api/clear_units", **padded("urlencoded", 10))
        local = rig.client("local").post("/api/clear_units", **padded("urlencoded", OVER))
        self.assertEqual(local.status_code, 200, "a remote request must not leave a limit behind for a local one")


if __name__ == "__main__":
    unittest.main()
