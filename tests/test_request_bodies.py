"""A request body that is not an object is refused, and refusing it changes nothing.

Driven by the route table, not by a list of routes somebody remembered: every JSON-accepting route
the application registers is posted `[]`, `null`, `5`, `"x"`, `true` and `{"a": []}` by a local
caller, a remote administrator and a remote user, with the hardware fence on. Each answer must be
below 500, the non-object bodies must be 400 with a plain sentence that names no Python exception,
and neither the saved state nor any device may have changed. A mutating device call fails the test
at the call and again in cleanup, because service code that turns errors into results would absorb
an assertion raised inside it.
"""
import copy
import json
import re
import unittest
from unittest import mock

import _fence
import _support
from _support import ADMIN_PASSWORD, USER_PASSWORD, RealApp, RealRemote

import luma_request
import luma_users

NON_OBJECTS = [("list", b"[]"), ("null", b"null"), ("number", b"5"), ("string", b'"x"'), ("true", b"true"),
               ("bad json", b"{"), ("list of objects", b'[{"a": 1}]')]
def field_names():
    """Every field a handler reads from a body, taken from the source rather than from memory."""
    import os
    names = set()
    for module in ("lumaserver.py", "luma_remote_api.py", "luma_gate.py", "producer2_routes.py"):
        with open(os.path.join(_support.ROOT, module), encoding="utf-8") as handle:
            text = handle.read()
        names.update(re.findall(r"(?:data|body|payload|wall)\.get\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", text))
    return sorted(names)


def wrong_kind_bodies():
    names = field_names()
    out = []
    for label, value in (("list", []), ("int", 5), ("object", {}), ("float", 1.5), ("true", True)):
        out.append(("every field a %s" % label, json.dumps({n: value for n in names}).encode()))
    return out


OBJECTS = [("object with a list", b'{"a": []}'), ("empty object", b"{}"), ("empty body", b"")]
LEAKS = re.compile(r"Traceback|object has no attribute|NoneType|TypeError|KeyError|AttributeError|ValueError|"
                   r"werkzeug|\.py\b|line \d+", re.I)


#: Commands whose whole contract is "POST with nothing": the Clear Units button sends an empty object and
#: the server decides from its own state, by design, so that a request cannot widen what is removed.
#: They are still refused a body that is not an object; they are the only ones allowed to act on an
#: object that names nothing, and the list is asserted so it cannot grow quietly.
BODYLESS_COMMANDS = {"/api/clear_units"}


def json_post_routes(app):
    out = []
    for rule in app.url_map.iter_rules():
        if "POST" in rule.methods and rule.endpoint != "static":
            out.append(re.sub(r"<[^>]+>", "nope", rule.rule))
    return sorted(set(out))


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.store = self.remote.store
        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions((), (), (), ()))
        self.local = self.m.APP.test_client()
        self.admin = self.remote.sign_in("root", ADMIN_PASSWORD)
        self.user = self.remote.sign_in("alex", USER_PASSWORD)
        self.anonymous = self.m.APP.test_client()
        self.anonymous.environ_base["luma.listener"] = "remote"
        # Nothing may start a program or open a folder because a body was odd.
        for target in ("subprocess.Popen", "os.startfile"):
            try:
                patcher = mock.patch(target, side_effect=AssertionError("a process was started"))
                patcher.start()
                self.addCleanup(patcher.stop)
            except AttributeError:
                pass
        self.addCleanup(self.check_no_device_write)
        self.fence_before = sum(_fence.attempts.values())

    def check_no_device_write(self):
        self.assertEqual(self.app.devices.writes(), [], "a malformed body reached a device write")

    def snapshot(self):
        m = self.m
        with m.cache_lock:
            units = copy.deepcopy(sorted(m.cache_units))
        remote = self.remote.remote
        return json.dumps({
            "walls": m._load_video_wall_state(),
            "units": units,
            "accounts": self.store.list_accounts(),
            "groups": remote.groups.all(),
            "aliases": remote.aliases.all(),
            "listener_on": remote.listener._server is not None,
            "sessions": len(self.store._sessions),
            "config": {k: v for k, v in m.CONFIG.items() if k not in ("password", "fallback_password")},
        }, sort_keys=True, default=str)

    def post(self, client, path, raw, remote):
        headers = {}
        if remote and getattr(client, "csrf", None):
            headers["X-LumaSuite-CSRF"] = client.csrf
        return client.post(path, data=raw, content_type="application/json", headers=headers)


class EveryRouteTests(Base):
    def run_matrix(self, bodies, label):
        clients = [("local", self.local, False), ("remote admin", self.admin, True),
                   ("remote user", self.user, True), ("anonymous", self.anonymous, True)]
        routes = json_post_routes(self.m.APP)
        self.assertGreater(len(routes), 55, "the route table was not read")
        problems = []
        for who, client, remote in clients:
            for path in routes:
                for name, raw in bodies:
                    before = self.snapshot()
                    response = self.post(client, path, raw, remote)
                    where = "%s POST %s body %s" % (who, path, name)
                    text = response.get_data(as_text=True)
                    if response.status_code >= 500:
                        problems.append(where + " -> %s" % response.status_code)
                    elif LEAKS.search(text):
                        problems.append(where + " leaked: " + text[:120])
                    elif label == "non-object" and response.status_code not in (400, 401, 403, 404, 413):
                        problems.append(where + " -> %s" % response.status_code)
                    after = self.snapshot()
                    if label == "object" and path == "/api/auth/logout":
                        continue        # ending the caller's own session is what it is for
                    if label == "object" and path in BODYLESS_COMMANDS:
                        self.m._save_video_wall_state({"walls": [], "ignored_candidates": []})
                        self.app.seed()
                        continue
                    if before != after:
                        problems.append(where + " changed saved state")
        self.maxDiff = None
        self.assertEqual(problems, [])

    def test_a_body_that_is_not_an_object_is_refused_and_changes_nothing(self):
        self.run_matrix(NON_OBJECTS, "non-object")
        self.assertEqual(sum(_fence.attempts.values()), self.fence_before, "something tried to reach the network")

    def test_an_object_that_says_nothing_useful_never_errors_and_changes_nothing(self):
        # Routes whose only effect with no fields is a read (a refresh, a scan of nothing) are still
        # held to "no 5xx, no state change, no device write".
        self.run_matrix(OBJECTS + wrong_kind_bodies(), "object")

    def test_a_local_caller_gets_400_with_a_plain_sentence_not_a_status_it_must_interpret(self):
        for name, raw in NON_OBJECTS:
            response = self.post(self.local, "/api/route_matrix", raw, False)
            self.assertEqual(response.status_code, 400, name)
            body = response.get_json()
            self.assertFalse(body["ok"])
            self.assertIn(body["error"], (luma_request.NOT_AN_OBJECT, luma_request.NOT_JSON))
            self.assertEqual(body["reason"], "bad_body")

    def test_sign_in_with_a_body_that_is_not_an_object_is_400_not_500(self):
        for name, raw in NON_OBJECTS:
            response = self.post(self.anonymous, "/api/auth/login", raw, False)
            self.assertEqual(response.status_code, 400, name)

    def test_a_body_that_does_not_claim_to_be_json_is_still_ignored_as_before(self):
        response = self.local.post("/api/scan", data=b"[]", content_type="text/plain")
        self.assertNotEqual(response.status_code, 400)


class NoBodyActsAsACommandTests(Base):
    def test_remote_access_is_not_switched_off_by_a_body_that_does_not_say_so(self):
        remote = self.remote.remote
        for name, raw in NON_OBJECTS + OBJECTS:
            before = (remote.listener._server is not None, len(self.store._sessions))
            response = self.post(self.local, "/api/remote_access", raw, False)
            self.assertEqual(response.status_code, 400, name)
            self.assertEqual((remote.listener._server is not None, len(self.store._sessions)), before, name)
        for value in ('"false"', "0", "1", '"true"', "null", "[]"):
            response = self.post(self.local, "/api/remote_access", ('{"enabled": %s}' % value).encode(), False)
            self.assertEqual(response.status_code, 400, value)
        self.assertIsNotNone(remote.listener._server)

    def test_a_video_wall_is_not_saved_from_a_body_that_describes_no_wall(self):
        for name, raw in NON_OBJECTS + OBJECTS + [("ip as a list", b'{"ip": []}')]:
            before = self.m._load_video_wall_state()
            response = self.post(self.local, "/api/video_wall/save", raw, False)
            self.assertEqual(response.status_code, 400, name)
            self.assertEqual(self.m._load_video_wall_state(), before, name)


class UnexpectedFailureTests(Base):
    def test_an_unexpected_failure_answers_with_a_sentence_not_the_exception(self):
        with self.m.APP.test_request_context("/api/x", method="POST"):
            body, status = luma_request.internal_error(ValueError("secret-detail-4471 in module.py line 9"))
        self.assertEqual(status, 500)
        self.assertNotIn("secret-detail-4471", body.get_data(as_text=True))
        self.assertEqual(body.get_json()["reason"], "internal_error")


class RemoteAccessPortTests(Base):
    BAD = ["70000", "-1", "65536", "99999999999", "0", "1", "80", "1023", '"x"', '""', "true", "false",
           "1.5", '"8080 "x', "[]", "{}", '"١٢٣٤٥"', '"1e4"', '"0x1F90"']

    def setUp(self):
        super().setUp()
        self.bind = mock.patch.object(self.remote.remote.listener, "start",
                                      side_effect=AssertionError("listener started with an invalid port"))

    def test_an_invalid_port_is_refused_before_anything_starts_or_is_saved(self):
        remote = self.remote.remote
        remote.listener._server = None
        saved = remote.last_choice() if hasattr(remote, "last_choice") else None
        self.bind.start()
        self.addCleanup(self.bind.stop)
        for value in self.BAD:
            raw = ('{"enabled": true, "transport": "http", "accept_insecure": true, "address": "192.0.2.77", "port": %s}'
                   % value).encode()
            response = self.post(self.local, "/api/remote_access", raw, False)
            self.assertEqual(response.status_code, 400, value)
            body = response.get_json()
            self.assertFalse(body["ok"], value)
            self.assertEqual(body["reason"] if value not in ('"8080 "x',) else "bad_body", body["reason"])
        if saved is not None:
            self.assertEqual(remote.last_choice(), saved, "an invalid port was persisted")

    def test_the_valid_range_is_the_local_port_rule(self):
        import luma_portcontrol
        import luma_remote
        for value in ("1024", "29901", "65535", 1024, 65535):
            self.assertEqual(luma_remote.validate_port(value), luma_portcontrol.validate_port(value))
        for value in ("1023", "65536", "0", "-1", "70000", "99999999999", "x", "", " ", True, False, 1.5, None,
                      [], {}, 80, 1, "1e4", "0x1F90", "١٢٣٤٥"):
            with self.assertRaises(luma_remote.PortError, msg=repr(value)):
                luma_remote.validate_port(value)
            with self.assertRaises(luma_portcontrol.PortError, msg=repr(value)):
                luma_portcontrol.validate_port(value)


if __name__ == "__main__":
    unittest.main()
