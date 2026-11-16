"""Account, Direct Access and permission lifecycle, end to end through the HTTP surface.

Each test drives the real routes with independent cookie jars (admin, password User,
Direct Access User) and asserts what the *server* answers, because the server is the
authority: a page that is slow to repaint must never be what keeps a permission alive.
"""
import unittest

import _support  # noqa: F401  (installs the fence)
from _support import ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, SOURCE_A, SOURCE_B, System, identity

A, B = identity(SOURCE_A), identity(SOURCE_B)
D1, D2, D3 = (identity(u) for u in (DISPLAY_1, DISPLAY_2, DISPLAY_3))
ROUTE = "/api/user/route"
ROUTE_GROUP = "/api/user/route_group"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.admin_user = self.s.admin("root")
        self.local = self.s.client(remote=False)          # the local operator administers

    def admin_post(self, path, body):
        return self.local.post(path, json=body)

    def user_id(self, name="alex"):
        return next(u["id"] for u in self.s.store.list_accounts() if u["username"] == name)

    def set_permissions(self, name="alex", sources=(), destinations=(), groups=()):
        return self.admin_post("/api/admin/users/" + self.user_id(name), {
            "action": "set_permissions",
            "permissions": {"sources": list(sources), "destinations": list(destinations),
                            "groups": list(groups)}})

    def state(self, client):
        return client.get("/api/user/state")

    def direct(self, action, name="alex"):
        return self.admin_post("/api/admin/users/%s/direct_access" % self.user_id(name),
                               {"action": action})

    def open_link(self, link):
        client = self.s.client(remote=True)
        path = "/access/" + link.rsplit("/", 1)[1]
        response = client.get(path)
        return client, response


class LivePermissionTests(Fixture):
    def test_an_open_session_sees_new_permissions_without_signing_in_again(self):
        self.s.user("alex", sources=[A], destinations=[D1])
        client = self.s.sign_in("alex", USER_PASSWORD)
        g = self.s.remote.groups.create("Pair", [D2, D3])["group"]
        self.set_permissions(sources=[A], destinations=[], groups=[g["id"]])
        body = self.state(client).get_json()
        self.assertTrue(body["ok"], body)
        self.assertEqual([d["id"] for d in body["destinations"]], [])
        self.assertEqual([x["id"] for x in body["groups"]], [g["id"]])

    def test_a_removed_display_is_refused_by_the_old_session_at_once(self):
        self.s.user("alex", sources=[A], destinations=[D1])
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.assertEqual(self.s.post(client, ROUTE, {"source": A, "destination": D1}).status_code, 200)
        self.set_permissions(sources=[A], destinations=[])
        response = self.s.post(client, ROUTE, {"source": A, "destination": D1})
        self.assertEqual(response.status_code, 403, response.get_json())

    def test_a_group_resolves_to_its_current_members(self):
        g = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        self.s.user("alex", sources=[A], groups=[g["id"]])
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.s.post(client, ROUTE_GROUP, {"source": A, "group": g["id"]})
        self.assertEqual(sorted(d for d, _ in self.s.router.calls),
                         sorted([DISPLAY_1["ip"], DISPLAY_2["ip"]]))
        self.s.router.calls.clear()
        self.admin_post("/api/admin/groups/" + g["id"], {"action": "set_members", "members": [D2, D3]})
        self.s.post(client, ROUTE_GROUP, {"source": A, "group": g["id"]})
        self.assertEqual(sorted(d for d, _ in self.s.router.calls),
                         sorted([DISPLAY_2["ip"], DISPLAY_3["ip"]]))

    def test_the_revision_moves_when_permissions_move(self):
        self.s.user("alex", sources=[A], destinations=[D1])
        client = self.s.sign_in("alex", USER_PASSWORD)
        first = client.get("/api/user/revision").get_json()
        self.assertTrue(first["ok"])
        self.assertEqual(client.get("/api/user/revision").get_json()["rev"], first["rev"])
        self.set_permissions(sources=[A], destinations=[D2])
        self.assertNotEqual(client.get("/api/user/revision").get_json()["rev"], first["rev"])

    def test_the_revision_moves_when_a_group_changes_or_a_name_changes(self):
        g = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        self.s.user("alex", sources=[A], groups=[g["id"]])
        client = self.s.sign_in("alex", USER_PASSWORD)
        r0 = client.get("/api/user/revision").get_json()["rev"]
        self.admin_post("/api/admin/groups/" + g["id"], {"action": "set_members", "members": [D2, D3]})
        r1 = client.get("/api/user/revision").get_json()["rev"]
        self.assertNotEqual(r0, r1)
        self.admin_post("/api/admin/aliases", {"aliases": {A: "Stage Camera"}})
        self.assertNotEqual(client.get("/api/user/revision").get_json()["rev"], r1)

    def test_the_revision_moves_when_only_a_groups_membership_changes(self):
        # A change that alters nothing else the digest sees: the new member is a device
        # that is not in the inventory, so only the group record itself differs.
        g = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        self.s.user("alex", groups=[g["id"]])
        client = self.s.sign_in("alex", USER_PASSWORD)
        before = client.get("/api/user/revision").get_json()["rev"]
        self.admin_post("/api/admin/groups/" + g["id"], {"action": "set_members", "members": [D1, D2, "02:00:5e:99:00:01"]})
        self.assertNotEqual(client.get("/api/user/revision").get_json()["rev"], before)

    def test_revision_needs_a_session(self):
        self.assertEqual(self.s.client(remote=True).get("/api/user/revision").status_code, 401)

    def test_a_friendly_name_change_reaches_the_user_and_removal_restores_the_fallback(self):
        self.s.user("alex", sources=[A])
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.admin_post("/api/admin/aliases", {"aliases": {A: "Stage Camera"}})
        self.assertEqual(self.state(client).get_json()["sources"][0]["name"], "Stage Camera")
        self.admin_post("/api/admin/aliases", {"aliases": {A: "Pit Camera"}})
        self.assertEqual(self.state(client).get_json()["sources"][0]["name"], "Pit Camera")
        self.admin_post("/api/admin/aliases", {"aliases": {A: ""}})
        self.assertEqual(self.state(client).get_json()["sources"][0]["name"], "Alpha Camera")

    def test_a_friendly_name_is_never_authority(self):
        self.s.user("alex", sources=[A], destinations=[D1])
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.admin_post("/api/admin/aliases", {"aliases": {B: "Alpha Camera", D2: "Lobby One"}})
        self.assertEqual(self.s.post(client, ROUTE, {"source": B, "destination": D1}).status_code, 403)
        self.assertEqual(self.s.post(client, ROUTE, {"source": A, "destination": D2}).status_code, 403)


class DirectAccessLifecycleTests(Fixture):
    def setUp(self):
        super().setUp()
        self.s.user("alex", sources=[A], destinations=[D1])

    def test_regeneration_ends_the_old_link_and_opens_the_new_one(self):
        first = self.direct("enable").get_json()
        link_a = first["link"]
        self.assertTrue(link_a)
        _client, response = self.open_link(link_a)
        self.assertEqual(response.status_code, 200)
        second = self.direct("regenerate").get_json()
        link_b = second["link"]
        self.assertNotEqual(link_a, link_b)
        self.assertEqual(self.open_link(link_a)[1].status_code, 404)
        client_b, response_b = self.open_link(link_b)
        self.assertEqual(response_b.status_code, 200)
        self.assertTrue(client_b.get("/api/user/state").get_json()["ok"])

    def test_trying_the_old_link_many_times_never_locks_out_the_new_one(self):
        # The defect an operator actually hit: checking that the old link is dead
        # (a handful of refusals from the same browser) throttled the address, and
        # the NEW link was then refused as well -- which looked exactly like
        # "regenerate does not work".
        link_a = self.direct("enable").get_json()["link"]
        link_b = self.direct("regenerate").get_json()["link"]
        for _ in range(12):
            self.assertEqual(self.open_link(link_a)[1].status_code, 404)
        client_b, response_b = self.open_link(link_b)
        self.assertEqual(response_b.status_code, 200)
        self.assertTrue(client_b.get("/api/user/state").get_json()["ok"])

    def test_guessing_is_still_throttled_and_audited_once_per_refusal_until_throttled(self):
        for _ in range(8):
            self.assertEqual(self.open_link("https://x/access/guess-%s" % _)[1].status_code, 404)
        refusals = [e for e in self.s.store.audit_entries(limit=100) if e["event"] == "direct_access.used"]
        self.assertLessEqual(len(refusals), 5)      # throttled refusals are not each audited

    def test_the_session_the_old_link_opened_ends_with_the_regeneration(self):
        link_a = self.direct("enable").get_json()["link"]
        client_a, _ = self.open_link(link_a)
        self.assertEqual(client_a.get("/api/user/state").status_code, 200)
        self.direct("regenerate")
        self.assertEqual(client_a.get("/api/user/state").status_code, 401)

    def test_a_password_change_leaves_the_link_and_its_session_alone(self):
        # Direct Access follows its own token lifecycle. A password change ends password
        # sessions; it neither regenerates the link nor ends a session the link opened.
        link = self.direct("enable").get_json()["link"]
        client, _ = self.open_link(link)
        password_session = self.s.sign_in("alex", USER_PASSWORD)
        self.admin_post("/api/admin/users/" + self.user_id(),
                        {"action": "set_password", "password": "second passphrase!"})
        self.assertEqual(password_session.get("/api/user/state").status_code, 401)
        self.assertEqual(client.get("/api/user/state").status_code, 200)
        self.assertEqual(self.open_link(link)[1].status_code, 200)

    def test_regeneration_does_not_end_a_password_session(self):
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.direct("enable")
        self.direct("regenerate")
        self.assertEqual(client.get("/api/user/state").status_code, 200)

    def test_the_admin_can_read_the_same_link_again_later(self):
        self.direct("enable")
        link = self.direct("regenerate").get_json()["link"]
        again = self.local.get("/api/admin/users/%s/direct_access" % self.user_id()).get_json()
        self.assertEqual(again["link"], link)

    def test_disable_then_enable_gives_a_new_working_link_and_the_old_stays_dead(self):
        link_a = self.direct("enable").get_json()["link"]
        self.direct("disable")
        self.assertEqual(self.open_link(link_a)[1].status_code, 404)
        link_b = self.direct("enable").get_json()["link"]
        self.assertEqual(self.open_link(link_a)[1].status_code, 404)
        self.assertEqual(self.open_link(link_b)[1].status_code, 200)

    def test_a_password_change_leaves_the_link_alone(self):
        link = self.direct("enable").get_json()["link"]
        self.admin_post("/api/admin/users/" + self.user_id(),
                        {"action": "set_password", "password": "a brand new passphrase"})
        self.assertEqual(self.open_link(link)[1].status_code, 200)

    def test_a_permission_change_keeps_the_link_and_session_but_not_the_old_grant(self):
        link = self.direct("enable").get_json()["link"]
        client, _ = self.open_link(link)
        self.set_permissions(sources=[A], destinations=[D2])
        self.assertEqual(client.get("/api/user/state").status_code, 200)
        self.assertEqual([d["id"] for d in client.get("/api/user/state").get_json()["destinations"]], [D2])
        self.assertEqual(self.s.post(client, ROUTE, {"source": A, "destination": D1}).status_code, 403)

    def test_a_disabled_account_link_is_dead(self):
        link = self.direct("enable").get_json()["link"]
        self.admin_post("/api/admin/users/" + self.user_id(), {"action": "set_enabled", "enabled": False})
        self.assertEqual(self.open_link(link)[1].status_code, 404)

    def test_a_deleted_account_link_and_session_are_dead(self):
        link = self.direct("enable").get_json()["link"]
        client, _ = self.open_link(link)
        self.admin_post("/api/admin/users/" + self.user_id(), {"action": "delete"})
        self.assertEqual(client.get("/api/user/state").status_code, 401)
        self.assertEqual(self.open_link(link)[1].status_code, 404)
        self.assertEqual(self.s.store.active_session_count(), 0)

    def test_promoting_the_account_kills_the_link(self):
        link = self.direct("enable").get_json()["link"]
        self.admin_post("/api/admin/users/" + self.user_id(), {"action": "set_role", "role": "admin"})
        self.assertEqual(self.open_link(link)[1].status_code, 404)

    def test_a_regenerate_with_the_feature_off_is_a_refusal_not_an_error(self):
        self.assertEqual(self.direct("regenerate").status_code, 400)

    def test_a_link_cannot_be_made_for_an_administrator(self):
        self.assertEqual(self.direct("enable", "root").status_code, 400)


class SessionRevocationTests(Fixture):
    def test_a_password_change_ends_the_old_password_and_its_sessions(self):
        self.s.user("alex", sources=[A])
        old = self.s.sign_in("alex", USER_PASSWORD)
        self.admin_post("/api/admin/users/" + self.user_id(),
                        {"action": "set_password", "password": "second passphrase!"})
        self.assertEqual(old.get("/api/user/state").status_code, 401)
        self.assertEqual(self.s.client().post("/api/auth/login",
                         json={"username": "alex", "password": USER_PASSWORD}).status_code, 401)
        self.assertEqual(self.s.client().post("/api/auth/login",
                         json={"username": "alex", "password": "second passphrase!"}).status_code, 200)

    def test_demoting_an_administrator_ends_their_admin_access(self):
        self.s.admin("second")
        client = self.s.sign_in("second", ADMIN_PASSWORD)
        self.assertEqual(client.get("/api/admin/users").status_code, 200)
        self.admin_post("/api/admin/users/" + self.user_id("second"), {"action": "set_role", "role": "user"})
        self.assertIn(client.get("/api/admin/users").status_code, (401, 403))

    def test_promoting_a_user_does_not_leave_a_restricted_session_behind(self):
        self.s.user("alex", sources=[A])
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.admin_post("/api/admin/users/" + self.user_id(), {"action": "set_role", "role": "admin"})
        self.assertEqual(client.get("/api/user/state").status_code, 401)

    def test_deleting_an_account_ends_its_session_and_leaves_no_record(self):
        self.s.user("alex", sources=[A], destinations=[D1])
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.admin_post("/api/admin/users/" + self.user_id(), {"action": "delete"})
        self.assertEqual(self.s.post(client, ROUTE, {"source": A, "destination": D1}).status_code, 401)
        self.assertNotIn("alex", [u["username"] for u in self.s.store.list_accounts()])
        self.assertEqual(self.s.store.active_session_count(), 0)

    def test_disabling_an_account_ends_its_session(self):
        self.s.user("alex", sources=[A])
        client = self.s.sign_in("alex", USER_PASSWORD)
        self.admin_post("/api/admin/users/" + self.user_id(), {"action": "set_enabled", "enabled": False})
        self.assertEqual(client.get("/api/user/state").status_code, 401)


class AccountValidationTests(Fixture):
    def create(self, **body):
        base = {"username": "pat", "password": "long enough password", "role": "user"}
        base.update(body)
        return self.admin_post("/api/admin/users", base)

    def test_create_validation(self):
        self.assertEqual(self.create().status_code, 200)
        self.assertEqual(self.create().status_code, 400)                       # duplicate
        self.assertEqual(self.create(username="PAT").status_code, 400)         # case-insensitive duplicate
        self.assertEqual(self.create(username="  ").status_code, 400)
        self.assertEqual(self.create(username="x", password="short").status_code, 400)
        self.assertEqual(self.create(username="y", role="owner").status_code, 400)
        self.assertEqual(self.create(username="z", permissions={"sources": "nope"}).status_code, 200)

    def test_unknown_account_and_action_are_clean_refusals(self):
        self.assertEqual(self.admin_post("/api/admin/users/nope", {"action": "delete"}).status_code, 400)
        self.assertEqual(self.admin_post("/api/admin/users/nope", {"action": "bogus"}).status_code, 400)
        self.assertEqual(self.local.get("/api/admin/users/nope/direct_access").status_code, 404)

    def test_the_only_administrator_cannot_be_removed_or_demoted(self):
        rid = self.user_id("root")
        self.assertEqual(self.admin_post("/api/admin/users/" + rid, {"action": "delete"}).status_code, 400)
        self.assertEqual(self.admin_post("/api/admin/users/" + rid,
                                         {"action": "set_role", "role": "user"}).status_code, 400)
        self.assertEqual(self.admin_post("/api/admin/users/" + rid,
                                         {"action": "set_enabled", "enabled": False}).status_code, 400)

    def test_list_carries_role_permissions_and_direct_access_state(self):
        self.s.user("alex", sources=[A], destinations=[D1])
        row = next(u for u in self.local.get("/api/admin/users").get_json()["users"] if u["username"] == "alex")
        self.assertEqual(row["role"], "user")
        self.assertEqual(row["permissions"]["sources"], [A])
        self.assertIn("direct_access", row)
        self.assertNotIn("password_hash", row)


class LifecycleChurnTests(Fixture):
    """A short deterministic soak: nothing accumulates across account lifecycles."""

    def test_repeated_create_modify_regenerate_delete_leaves_no_residue(self):
        g = self.s.remote.groups.create("Pair", [D1, D2]) ["group"]
        for i in range(20):
            name = "temp%d" % i
            self.s.user(name, sources=[A], destinations=[D1])
            uid = self.user_id(name)
            client = self.s.sign_in(name, USER_PASSWORD)
            self.assertEqual(client.get("/api/user/state").status_code, 200)
            self.set_permissions(name, sources=[A], groups=[g["id"]])
            self.assertEqual([x["id"] for x in client.get("/api/user/state").get_json()["groups"]], [g["id"]])
            link_a = self.direct("enable", name).get_json()["link"]
            link_b = self.direct("regenerate", name).get_json()["link"]
            self.assertEqual(self.open_link(link_a)[1].status_code, 404)
            direct_client, response = self.open_link(link_b)
            self.assertEqual(response.status_code, 200)
            self.admin_post("/api/admin/groups/" + g["id"], {"action": "set_members", "members": [D2, D3] if i % 2 else [D1, D2]})
            self.admin_post("/api/admin/users/" + uid, {"action": "set_password", "password": "rotated passphrase %d" % i})
            self.assertEqual(client.get("/api/user/state").status_code, 401)
            self.admin_post("/api/admin/users/" + uid, {"action": "delete"})
            self.assertEqual(direct_client.get("/api/user/state").status_code, 401)
            self.assertEqual(self.open_link(link_b)[1].status_code, 404)
        self.assertEqual(self.s.store.active_session_count(), 0)
        self.assertEqual([u["username"] for u in self.s.store.list_accounts()], ["root"])
        self.assertLessEqual(len(self.s.store._sessions), 1)
        self.assertEqual(self.s.store.assigned_devices(), {})


if __name__ == "__main__":
    unittest.main()
