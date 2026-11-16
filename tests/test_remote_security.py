"""The gate, authentication, sessions, CSRF and Direct Access, driven through HTTP.

Every test here uses the Flask test client with synthetic equipment and a stubbed
route; nothing reaches a device. A request is made to look remote by tagging its
environ the way the real remote listener does (`luma.listener`), and remote access
is made to look on by marking the listener running, without opening a socket.
"""
import time
import unittest
from unittest import mock

import _support
from _support import ADMIN_PASSWORD, USER_PASSWORD, System, identity

import luma_gate
import luma_projection
import luma_remote
import luma_users


class LocalVersusRemoteTests(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)

    def test_a_local_request_is_untouched_by_the_gate(self):
        local = self.s.client(remote=False)
        for path in ("/api/config", "/api/list_dir", "/ui/index.html", "/"):
            self.assertEqual(local.get(path).status_code, 200, path)
        self.assertEqual(local.post("/api/route_matrix", json={}).status_code, 200)

    def test_remote_access_off_refuses_every_remote_request(self):
        remote = self.s.client()
        for path in ("/api/config", "/ui/user.js", "/login", "/api/auth/session"):
            response = remote.get(path)
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(self.s.hits, [])

    def test_remote_access_off_answers_a_browser_with_a_page_not_json(self):
        remote = self.s.client()
        response = remote.get("/login", headers={"Sec-Fetch-Mode": "navigate",
                                                 "Accept": "text/html"})
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"works only on the computer", response.data)

    def test_trust_comes_from_the_listener_not_a_header(self):
        self.s.turn_remote_on()
        remote = self.s.client()
        # Every header a client could send to claim to be local.
        spoof = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1",
                 "Forwarded": "for=127.0.0.1", "X-Luma-Listener": "local",
                 "luma.listener": "local", "Host": "localhost"}
        response = remote.get("/api/list_dir", headers=spoof,
                              environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.s.hits, [])

    def test_the_listener_tag_cannot_be_produced_from_the_wire(self):
        # A dotted key cannot come from an HTTP header (HTTP_UPPER_UNDERSCORE).
        self.assertEqual(luma_remote.LISTENER_KEY, "luma.listener")
        self.assertIn(".", luma_remote.LISTENER_KEY)
        self.assertTrue(luma_remote.is_local_request({}))
        self.assertFalse(luma_remote.is_local_request({"luma.listener": "remote"}))
        self.assertTrue(luma_remote.is_local_request({"HTTP_LUMA_LISTENER": "remote"}))

    def test_a_tagged_listener_overwrites_whatever_the_request_claimed(self):
        seen = {}

        def app(environ, start_response):
            seen.update(environ)
            start_response("200 OK", [])
            return [b""]

        wrapped = luma_remote._ListenerTag(app, luma_remote.REMOTE)
        wrapped({"luma.listener": "local", "REQUEST_METHOD": "GET"}, lambda *a: None)
        self.assertEqual(seen["luma.listener"], luma_remote.REMOTE)

    def test_a_non_loopback_bind_is_remote_and_loopback_is_local(self):
        self.assertEqual(luma_remote.tag_for_bind("127.0.0.1"), luma_remote.LOCAL)
        self.assertEqual(luma_remote.tag_for_bind("192.0.2.5"), luma_remote.REMOTE)
        self.assertEqual(luma_remote.tag_for_bind("0.0.0.0"), luma_remote.REMOTE)


class ListenerTaggingTests(unittest.TestCase):
    """The trust decision, proven over real loopback sockets."""

    def serve(self, bind_host, system):
        import threading
        server = luma_remote.serve_tagged(bind_host, 0, system.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(5)))
        return server.server_port

    def fetch(self, port, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as err:
            return err.code

    def test_a_loopback_listener_serves_the_local_operator(self):
        s = System()
        self.addCleanup(s.close)
        port = self.serve("127.0.0.1", s)
        self.assertEqual(self.fetch(port, "/api/list_dir"), 200)
        self.assertEqual(s.hits, ["list_dir"])

    def test_a_listener_bound_to_a_network_address_is_remote_even_when_reached_over_loopback(self):
        # Bound to every interface, so it is a network listener; connecting to it via
        # 127.0.0.1 does not make its requests local. Trust is the listener, not the peer.
        s = System()
        self.addCleanup(s.close)
        port = self.serve("0.0.0.0", s)
        self.assertEqual(self.fetch(port, "/api/list_dir"), 404)      # remote access is off
        self.assertEqual(s.hits, [])

    def test_a_network_listener_with_remote_access_on_still_never_serves_a_local_only_route(self):
        s = System()
        self.addCleanup(s.close)
        s.turn_remote_on()
        port = self.serve("0.0.0.0", s)
        self.assertEqual(self.fetch(port, "/api/list_dir"), 404)
        self.assertEqual(self.fetch(port, "/api/config"), 401)
        self.assertEqual(s.hits, [])

    def test_no_code_opens_a_listener_except_through_the_tagging_factory(self):
        import os
        import re
        offenders = []
        pattern = re.compile(r"(app|APP)\.run\(|make_server\(|serve_forever\(|HTTPServer\(")
        for name in sorted(os.listdir(_support.ROOT)):
            if not name.endswith(".py") or name.startswith("test"):
                continue
            text = open(os.path.join(_support.ROOT, name), encoding="utf-8").read()
            for line_no, line in enumerate(text.splitlines(), 1):
                code = line.split("#", 1)[0]
                if pattern.search(code) and name != "luma_remote.py":
                    offenders.append("%s:%d %s" % (name, line_no, code.strip()))
        # the two entry points call serve_forever on a server they got from serve_tagged
        offenders = [o for o in offenders if "serve_forever" not in o or "serve_tagged" not in
                     open(os.path.join(_support.ROOT, o.split(":")[0]), encoding="utf-8").read()]
        self.assertEqual(offenders, [])


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        self.s.user("alex")

    def test_no_session_is_401_for_an_api_caller(self):
        response = self.s.client().get("/api/config")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["reason"], "no_session")
        self.assertEqual(self.s.hits, [])

    def test_no_session_sends_a_browser_to_the_sign_in_page(self):
        response = self.s.client().get("/ui/index.html", headers={
            "Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("/login?next="))

    def test_an_administrator_reaches_administrative_routes_and_pages(self):
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        self.assertEqual(admin.get("/api/config").status_code, 200)
        self.assertEqual(admin.get("/ui/index.html").status_code, 200)
        self.assertEqual(admin.get("/").status_code, 200)

    def test_a_user_cannot_reach_administrative_routes_or_pages_by_name(self):
        user = self.s.sign_in("alex", USER_PASSWORD)
        for path in ("/api/config", "/ui/index.html", "/ui/settings.js", "/ui/admin_users.js",
                     "/ui/manual.html", "/", "/api/admin/users"):
            response = user.get(path)
            self.assertEqual(response.status_code, 403, path)
        self.assertEqual(self.s.hits, [])

    def test_a_user_can_load_the_files_the_routing_page_is_made_of(self):
        user = self.s.sign_in("alex", USER_PASSWORD)
        for name in ("user.js", "family.css", "csrf.js", "remote.css"):
            self.assertEqual(user.get("/ui/" + name).status_code, 200, name)

    def test_an_unauthenticated_client_can_load_only_the_public_files(self):
        anon = self.s.client()
        self.assertEqual(anon.get("/ui/login.js").status_code, 200)
        self.assertEqual(anon.get("/ui/family.css").status_code, 200)
        self.assertEqual(anon.get("/ui/index.html").status_code, 401)
        self.assertEqual(anon.get("/ui/settings.js").status_code, 401)

    def test_local_only_routes_are_unreachable_remotely_even_for_an_administrator(self):
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        response = admin.get("/api/list_dir")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["reason"], "local_only")
        self.assertNotIn("list_dir", self.s.hits)

    def test_a_denial_is_a_sentence_with_a_reason_and_never_a_status_word(self):
        user = self.s.sign_in("alex", USER_PASSWORD)
        body = user.get("/api/admin/users").get_json()
        self.assertEqual(body["reason"], "not_admin")
        self.assertEqual(body["error"], luma_gate.DENIAL_SENTENCES["not_admin"])

    def test_a_denied_request_is_audited_with_its_reason(self):
        user = self.s.sign_in("alex", USER_PASSWORD)
        user.get("/api/admin/users")
        denied = [e for e in self.s.store.audit_entries(limit=50) if e["event"] == "access.denied"]
        self.assertTrue(denied)
        self.assertEqual(denied[0]["detail"]["reason"], "not_admin")

    def test_an_unclassified_route_is_denied_remotely_for_everyone(self):
        # Built on its own app: a rule cannot be added once an app has served a request.
        other = System()
        self.addCleanup(other.close)
        other.turn_remote_on()
        other.admin("root")
        other.app.add_url_rule("/api/never-classified", "never",
                               lambda: other.hits.append("never") or "hit")
        admin = other.sign_in("root", ADMIN_PASSWORD)
        response = admin.get("/api/never-classified")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["reason"], "local_only")
        self.assertNotIn("never", other.hits)

    def test_a_path_that_matches_no_route_is_denied_the_same_way_for_everyone(self):
        # Nothing is disclosed about what exists: a stranger and an administrator
        # get the identical refusal.
        stranger = self.s.client().get("/api/nothing-here")
        admin = self.s.sign_in("root", ADMIN_PASSWORD).get("/api/nothing-here")
        self.assertEqual((stranger.status_code, admin.status_code), (404, 404))
        self.assertEqual(stranger.get_json(), admin.get_json())


class CsrfTests(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        self.admin = self.s.sign_in("root", ADMIN_PASSWORD)

    def test_a_state_changing_request_without_the_token_is_refused(self):
        response = self.admin.post("/api/config", json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "csrf")
        self.assertNotIn("config", self.s.hits)

    def test_a_wrong_token_is_refused(self):
        response = self.admin.post("/api/config", json={},
                                   headers={"X-LumaSuite-CSRF": "not-the-token"})
        self.assertEqual(response.status_code, 403)

    def test_the_right_token_is_accepted(self):
        response = self.admin.post("/api/config", json={},
                                   headers={"X-LumaSuite-CSRF": self.admin.csrf})
        self.assertEqual(response.status_code, 200)

    def test_a_token_from_another_session_is_refused(self):
        other = self.s.sign_in("root", ADMIN_PASSWORD)
        response = self.admin.post("/api/config", json={},
                                   headers={"X-LumaSuite-CSRF": other.csrf})
        self.assertEqual(response.status_code, 403)

    def test_reads_do_not_need_the_token(self):
        self.assertEqual(self.admin.get("/api/config").status_code, 200)

    def test_csrf_is_checked_after_the_session_so_a_stranger_gets_401(self):
        response = self.s.client().post("/api/config", json={})
        self.assertEqual(response.status_code, 401)

    def test_the_token_is_returned_by_the_session_endpoint_for_a_page_that_never_signed_in(self):
        body = self.admin.get("/api/auth/session").get_json()
        self.assertEqual(body["csrf_token"], self.admin.csrf)
        self.assertEqual(body["method"], "password")


class LoginHardeningTests(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")

    def login(self, name, password, client=None):
        client = client or self.s.client()
        return client.post("/api/auth/login", json={"username": name, "password": password})

    def test_a_wrong_password_and_an_unknown_user_say_the_same_thing(self):
        wrong = self.login("root", "nope-nope-nope")
        unknown = self.login("nobody", "nope-nope-nope")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.get_json(), unknown.get_json())

    def test_a_disabled_account_reads_as_a_wrong_password(self):
        user = self.s.user("sam")
        self.s.store.set_enabled(user["id"], False)
        again = self.login("sam", USER_PASSWORD)
        self.assertEqual(again.status_code, 401)
        self.assertEqual(again.get_json(), self.login("nobody", "x" * 12).get_json())

    def test_an_unknown_username_still_costs_a_password_verification(self):
        # What is asserted is that the obvious difference is absent, and that is
        # read from the code path rather than timed: a timing measurement inside a
        # test suite would be flaky and would prove less than the code does.
        with mock.patch.object(luma_users, "verify_password",
                               wraps=luma_users.verify_password) as verify:
            self.login("nobody-at-all", "irrelevant-password")
            self.login("root", "wrong-wrong-wrong")
        self.assertEqual(verify.call_count, 2)

    def test_an_unknown_username_costs_one_derivation_not_two(self):
        # It used to be checked against a hash made on the spot: a second key
        # derivation on top of the check, so an unknown name cost about twice a known
        # one and a real username answered faster. Counted, not timed.
        luma_users.dummy_hash()                      # made once; not part of a sign-in
        for name in ("nobody-at-all", "root"):
            with mock.patch.object(luma_users, "hash_password",
                                   wraps=luma_users.hash_password) as made:
                self.login(name, "wrong-wrong-wrong")
            self.assertEqual(made.call_count, 0, name)
        self.assertEqual(luma_users.dummy_hash(), luma_users.dummy_hash())

    def test_repeated_failures_lock_out_and_a_correct_password_is_then_refused(self):
        for _ in range(luma_users.MAX_FAILURES):
            self.assertEqual(self.login("root", "wrong-wrong-wrong").status_code, 401)
        blocked = self.login("root", ADMIN_PASSWORD)
        self.assertEqual(blocked.status_code, 429)

    def test_a_success_clears_the_failure_count(self):
        for _ in range(luma_users.MAX_FAILURES - 1):
            self.login("root", "wrong-wrong-wrong")
        self.assertEqual(self.login("root", ADMIN_PASSWORD).status_code, 200)
        for _ in range(luma_users.MAX_FAILURES - 1):
            self.assertEqual(self.login("root", "wrong-wrong-wrong").status_code, 401)

    def test_the_cookie_is_httponly_samesite_and_secure_only_over_tls(self):
        response = self.login("root", ADMIN_PASSWORD)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)          # plain HTTP: a Secure cookie would never be sent
        secure = self.s.client()
        response = secure.post("/api/auth/login", json={"username": "root", "password": ADMIN_PASSWORD},
                               base_url="https://192.0.2.200:18089")
        self.assertIn("Secure", response.headers.get("Set-Cookie", ""))

    def test_the_session_token_is_never_in_the_response_body(self):
        body = self.login("root", ADMIN_PASSWORD).get_json()
        self.assertNotIn("token", body)
        self.assertEqual(set(body), {"ok", "user", "csrf_token"})
        self.assertNotIn("password_hash", body["user"])

    def test_a_password_is_never_written_to_the_audit_log(self):
        self.login("root", "a-secret-password-value")
        text = repr(self.s.store.audit_entries(limit=100))
        self.assertNotIn("a-secret-password-value", text)

    def test_passwords_are_stored_as_salted_scrypt_hashes(self):
        account = self.s.store.get(self.s.store.list_accounts()[0]["id"])
        self.assertTrue(account.password_hash.startswith("scrypt:"))
        self.assertNotIn(ADMIN_PASSWORD, account.password_hash)

    def test_a_password_shorter_than_eight_characters_is_refused(self):
        result = self.s.store.create("shorty", "1234567", luma_users.ROLE_USER)
        self.assertFalse(result["ok"])
        self.assertTrue(self.s.store.create("longer", "12345678", luma_users.ROLE_USER)["ok"])

    def test_a_password_reset_is_held_to_the_same_minimum_and_changes_nothing_when_refused(self):
        account = self.s.user("resetme")
        for weak in ("", "1234567", None):
            self.assertFalse(self.s.store.set_password(account["id"], weak)["ok"], repr(weak))
        self.assertEqual(self.login("resetme", USER_PASSWORD).status_code, 200)       # still the old one
        self.assertTrue(self.s.store.set_password(account["id"], "12345678")["ok"])    # length only
        self.assertEqual(self.login("resetme", "12345678").status_code, 200)

    def test_an_administrator_resetting_a_password_through_the_api_is_held_to_it_too(self):
        account = self.s.user("viaapi")
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        response = self.s.post(admin, "/api/admin/users/" + account["id"],
                               {"action": "set_password", "password": "short"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.login("viaapi", USER_PASSWORD).status_code, 200)


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.admin_account = self.s.admin("root")
        self.user_account = self.s.user("alex")

    def test_logout_revokes_the_session(self):
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.assertEqual(client.get("/api/auth/session").status_code, 200)
        self.s.post(client, "/api/auth/logout")
        self.assertEqual(client.get("/api/auth/session").status_code, 401)

    def test_disabling_an_account_ends_its_session_on_the_next_request(self):
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.s.store.set_enabled(self.user_account["id"], False)
        self.assertEqual(client.get("/api/auth/session").status_code, 401)

    def test_an_account_disabled_by_a_path_that_did_not_revoke_still_loses_its_session(self):
        # `set_enabled` revokes in the same step, so the check on every request is a second
        # line of defence the first never lets anyone reach. A restored file, or a code path
        # added later, can disable an account without revoking: reach the check directly.
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.s.store.get(self.user_account["id"]).enabled = False
        self.assertEqual(client.get("/api/auth/session").status_code, 401)

    def test_deleting_an_account_ends_its_session(self):
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.s.store.delete(self.user_account["id"])
        self.assertEqual(client.get("/api/auth/session").status_code, 401)

    def test_changing_a_password_ends_the_sessions(self):
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.s.store.set_password(self.user_account["id"], "a brand new passphrase")
        self.assertEqual(client.get("/api/auth/session").status_code, 401)

    def test_an_idle_session_expires(self):
        client = self.s.sign_in("alex", USER_PASSWORD)
        future = time.time() + luma_users.IDLE_TIMEOUT_SECONDS + 5
        with mock.patch.object(luma_users, "_now", return_value=future):
            self.assertEqual(client.get("/api/auth/session").status_code, 401)

    def test_an_old_session_expires_whatever_its_activity(self):
        client = self.s.sign_in("alex", USER_PASSWORD)
        base = time.time()
        step = luma_users.IDLE_TIMEOUT_SECONDS - 60
        clock = {"now": base}
        with mock.patch.object(luma_users, "_now", side_effect=lambda: clock["now"]):
            for _ in range(4):                       # stay active past the absolute limit
                clock["now"] += step
                client.get("/api/auth/session")
        self.assertEqual(client.get("/api/auth/session").status_code, 401)

    def test_the_last_enabled_administrator_cannot_be_removed(self):
        admin_id = self.admin_account["id"]
        self.assertFalse(self.s.store.set_enabled(admin_id, False)["ok"])
        self.assertFalse(self.s.store.delete(admin_id)["ok"])
        self.assertFalse(self.s.store.set_role(admin_id, luma_users.ROLE_USER)["ok"])

    def test_turning_remote_access_off_revokes_every_session(self):
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.assertGreaterEqual(self.s.store.active_session_count(), 1)
        self.s.store.revoke_every_session()
        self.assertEqual(client.get("/api/auth/session").status_code, 401)


class DirectAccessTests(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        self.user_account = self.s.user("panel", sources=[identity(_support.SOURCE_A)],
                                        destinations=[identity(_support.DISPLAY_1)])
        self.minted = self.s.store.enable_direct_access(self.user_account["id"])
        self.token = self.minted["token"]

    def test_the_link_serves_the_routing_page_in_place_with_a_session(self):
        client = self.s.client()
        response = client.get("/access/" + self.token)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"user.html", response.data)
        self.assertIn("lumasuite_session", response.headers.get("Set-Cookie", ""))
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual(client.get("/api/auth/session").get_json()["method"], "direct_link")

    def test_one_answer_for_every_refusal(self):
        unknown = self.s.client().get("/access/" + "x" * 43)
        self.s.store.disable_direct_access(self.user_account["id"])
        revoked = self.s.client().get("/access/" + self.token)
        self.assertEqual((unknown.status_code, revoked.status_code), (404, 404))
        self.assertEqual(unknown.data, revoked.data)

    def test_a_link_session_cannot_reach_the_administrative_surface(self):
        client = self.s.client()
        client.get("/access/" + self.token)
        for path in ("/api/config", "/ui/index.html", "/api/admin/users"):
            self.assertEqual(client.get(path).status_code, 403, path)
        self.assertEqual(client.get("/api/admin/users").get_json()["reason"], "direct_link_not_admin")

    def test_a_link_session_stays_barred_after_the_account_is_promoted(self):
        client = self.s.client()
        client.get("/access/" + self.token)
        self.s.store.set_role(self.user_account["id"], luma_users.ROLE_ADMIN)
        # Promotion revokes sessions; a new link visit must not mint an admin session either.
        again = self.s.client()
        self.assertEqual(again.get("/access/" + self.token).status_code, 404)

    def test_a_link_session_on_an_administrator_account_does_not_skip_the_resource_check(self):
        # An administrator cannot be given a link and a promotion revokes link sessions, so this
        # second line of defence is unreachable through the product. Reach it directly: open the
        # session by hand, then ask for a source and display the account was never granted.
        boss = self.s.store.create("boss", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)["user"]
        session = self.s.store.open_session(boss["id"], method=luma_users.BY_DIRECT_LINK)
        client = self.s.client()
        client.set_cookie("lumasuite_session", session.token)
        response = client.post("/api/user/route",
                               json={"source": identity(_support.SOURCE_B),
                                     "destination": identity(_support.DISPLAY_2)},
                               headers={"X-LumaSuite-CSRF": session.csrf_token})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "source_not_permitted")
        self.assertEqual(self.s.router.calls, [])
        # and it is still barred from the administrative surface
        self.assertEqual(client.get("/api/admin/users").status_code, 403)

    def test_an_administrator_cannot_be_given_a_link(self):
        result = self.s.store.enable_direct_access(self.s.store.list_accounts()[0]["id"])
        if self.s.store.list_accounts()[0]["role"] == "admin":
            self.assertFalse(result["ok"])

    def test_exactly_one_link_is_live_and_regenerating_ends_the_old_sessions(self):
        client = self.s.client()
        client.get("/access/" + self.token)
        fresh = self.s.store.regenerate_direct_access(self.user_account["id"])["token"]
        self.assertNotEqual(fresh, self.token)
        self.assertEqual(client.get("/api/auth/session").status_code, 401)
        self.assertEqual(self.s.client().get("/access/" + self.token).status_code, 404)
        self.assertEqual(self.s.client().get("/access/" + fresh).status_code, 200)

    def test_a_link_session_does_not_expire_with_time(self):
        client = self.s.client()
        client.get("/access/" + self.token)
        future = time.time() + luma_users.ABSOLUTE_TIMEOUT_SECONDS * 5
        with mock.patch.object(luma_users, "_now", return_value=future):
            self.assertEqual(client.get("/api/auth/session").status_code, 200)

    def test_the_token_is_never_returned_by_the_public_account_record(self):
        record = self.s.store.get(self.user_account["id"]).public()
        self.assertNotIn(self.token, repr(record))
        self.assertEqual(set(record["direct_access"]), {"enabled", "protection", "created_at"})

    def test_refused_links_are_throttled(self):
        client = self.s.client()
        for _ in range(luma_users.MAX_FAILURES):
            client.get("/access/" + "y" * 43)
        before = len([e for e in self.s.store.audit_entries(limit=500) if e["event"] == "direct_access.used"])
        for _ in range(10):
            self.assertEqual(client.get("/access/" + "z" * 43).status_code, 404)
        after = len([e for e in self.s.store.audit_entries(limit=500) if e["event"] == "direct_access.used"])
        self.assertEqual(before, after, "throttled guesses cost no audit entry")
        # A valid link is never refused for what other links did: a 256-bit token cannot
        # be guessed, and refusing it would make a freshly regenerated link look broken.
        self.assertEqual(client.get("/access/" + self.token).status_code, 200)

    def test_a_valid_link_works_only_while_remote_access_is_on(self):
        self.s.remote.listener._server = None
        self.s.remote.listener._bound = None
        self.assertEqual(self.s.client().get("/access/" + self.token).status_code, 404)


class GateEvaluateTests(unittest.TestCase):
    """The gate's own per-resource check, with no handler behind it.

    The handlers repeat this check, so through HTTP the gate's copy cannot be told apart from
    theirs. `evaluate` is a pure function, so it is tested here on its own: if the handler
    were ever changed, the gate would still refuse.
    """

    A, B = identity(_support.SOURCE_A), identity(_support.SOURCE_B)
    D1, D2 = identity(_support.DISPLAY_1), identity(_support.DISPLAY_2)

    class Store:
        def __init__(self, account, session):
            self.account, self.session = account, session

        def resolve_session(self, token):
            return (self.account, self.session) if token == "tok" else (None, None)

    def decide(self, payload, *, sources=(), displays=(), groups=(), admin=False, link=False,
               rule="/api/user/route", method="POST"):
        from types import SimpleNamespace
        account = SimpleNamespace(is_admin=admin,
                                  permissions=luma_users.Permissions(sources, displays, groups))
        session = SimpleNamespace(csrf_token="c", from_direct_link=link)
        environ = {"luma.listener": "remote", "HTTP_COOKIE": "lumasuite_session=tok",
                   "HTTP_X_LUMASUITE_CSRF": "c"}
        return luma_gate.evaluate(rule, method, environ, payload, self.Store(account, session))

    def test_a_permitted_request_is_allowed(self):
        decision = self.decide({"source": self.A, "destination": self.D1}, sources=[self.A], displays=[self.D1])
        self.assertTrue(decision.allowed)

    def test_an_unpermitted_source_is_refused_by_the_gate_itself(self):
        decision = self.decide({"source": self.B, "destination": self.D1}, sources=[self.A], displays=[self.D1])
        self.assertEqual((decision.allowed, decision.status, decision.reason), (False, 403, "source_not_permitted"))

    def test_an_unpermitted_display_is_refused_by_the_gate_itself(self):
        decision = self.decide({"source": self.A, "destination": self.D2}, sources=[self.A], displays=[self.D1])
        self.assertEqual((decision.allowed, decision.reason), (False, "destination_not_permitted"))

    def test_an_unpermitted_group_is_refused_by_the_gate_itself(self):
        decision = self.decide({"source": self.A, "group": "grp_other"}, sources=[self.A], groups=["grp_mine"],
                               rule="/api/user/route_group")
        self.assertEqual((decision.allowed, decision.reason), (False, "group_not_permitted"))
        ok = self.decide({"source": self.A, "group": "GRP_MINE "}, sources=[self.A], groups=["grp_mine"],
                         rule="/api/user/route_group")
        self.assertTrue(ok.allowed)

    def test_every_name_a_request_may_carry_is_checked_including_lists(self):
        for key in ("destination", "destination_id", "display", "display_id", "decoder", "decoder_id"):
            decision = self.decide({"source": self.A, key: self.D2}, sources=[self.A], displays=[self.D1])
            self.assertEqual(decision.reason, "destination_not_permitted", key)
        for key in ("source", "source_id", "encoder", "encoder_id"):
            decision = self.decide({key: self.B}, sources=[self.A], displays=[self.D1])
            self.assertEqual(decision.reason, "source_not_permitted", key)
        decision = self.decide({"source": self.A, "destination": [self.D1, self.D2]},
                               sources=[self.A], displays=[self.D1])
        self.assertEqual(decision.reason, "destination_not_permitted")

    def test_an_administrator_is_not_held_to_a_users_grants_but_a_link_session_always_is(self):
        self.assertTrue(self.decide({"source": self.B, "destination": self.D2}, admin=True).allowed)
        held = self.decide({"source": self.B, "destination": self.D2}, admin=True, link=True)
        self.assertEqual((held.allowed, held.reason), (False, "source_not_permitted"))

    def test_a_link_session_is_barred_from_administrative_rules_before_anything_else(self):
        decision = self.decide({}, admin=True, link=True, rule="/api/config", method="GET")
        self.assertEqual((decision.allowed, decision.reason), (False, "direct_link_not_admin"))

    def test_the_session_and_csrf_are_checked_before_the_resource(self):
        from types import SimpleNamespace
        account = SimpleNamespace(is_admin=False, permissions=luma_users.Permissions([self.A], [self.D1], []))
        session = SimpleNamespace(csrf_token="c", from_direct_link=False)
        store = self.Store(account, session)
        no_cookie = luma_gate.evaluate("/api/user/route", "POST", {"luma.listener": "remote"}, {}, store)
        self.assertEqual(no_cookie.reason, "no_session")
        bad_csrf = luma_gate.evaluate("/api/user/route", "POST", {
            "luma.listener": "remote", "HTTP_COOKIE": "lumasuite_session=tok",
            "HTTP_X_LUMASUITE_CSRF": "wrong"}, {"source": self.A}, store)
        self.assertEqual(bad_csrf.reason, "csrf")


class StableIdentityTests(unittest.TestCase):
    def test_an_address_is_never_an_identity(self):
        for value in ("192.0.2.10", "198.51.100.21", "192.0.2.10:80", "1.2.3.4", "", None,
                      "not a mac", "02:00:5e:10:00", "02:00:5e:10:00:0a:ff"):
            self.assertEqual(luma_projection.canonical_identity(value), "", repr(value))

    def test_every_spelling_of_one_mac_is_one_identity(self):
        for spelling in ("02:00:5E:10:00:0A", "02-00-5e-10-00-0a", "02005e10000a", "0200.5e10.000a"):
            self.assertEqual(luma_projection.canonical_identity(spelling), "02:00:5e:10:00:0a", spelling)

    def test_a_unit_without_a_hardware_address_has_no_identity(self):
        self.assertEqual(luma_projection.unit_identity({"ip": "192.0.2.50", "type": "encoder"}), "")


if __name__ == "__main__":
    unittest.main()
