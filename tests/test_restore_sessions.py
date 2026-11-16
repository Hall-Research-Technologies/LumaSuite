"""What a restore does to sessions that are already open.

A restore replaces accounts, permissions and Direct Access links. A session is a credential that
was valid for the account as it stood when it was opened, so after a restore it must be valid only
if the account it names is still, in every way that matters, the account it was opened for:

  * an account the backup does not hold                  -> every session of it ends
  * an account restored with a different password        -> its password sessions end
  * an account restored with a different Direct Access   -> its link sessions end
    link, or none
  * an account restored disabled                         -> every session of it ends
  * an account restored with any other role or any other  -> every session of it ends, password and
    permission (sources, displays, groups, walls, either    link, administrator or user alike
    routing interface), or with another enabled state

An existing session survives a restore ONLY if its account is security-equivalent afterwards (same
password hash, role, permissions, enabled state and Direct Access link). An account that came back
exactly as it was keeps its sessions -- an unchanged administrator stays signed in -- because
signing everybody out for a restore that did not touch them would be a cost with no security
benefit. Nothing the request carries can keep a session, and the restored account is also judged
live on every request, which is the second line of defence and is reached deliberately below.

Real application, isolated state, network fenced.
"""
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, SOURCE_A, SOURCE_B,
                      RealApp, RealRemote, identity)

import luma_backup
import luma_users

A, B = identity(SOURCE_A), identity(SOURCE_B)
D1, D2 = identity(DISPLAY_1), identity(DISPLAY_2)
PASS = "a good backup passphrase"
NEW_PASSWORD = "a different passphrase!"


class Base(unittest.TestCase):
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
        self.snapshot_dir = tempfile.mkdtemp(prefix="luma-restore-sessions-")
        self.addCleanup(shutil.rmtree, self.snapshot_dir, True)
        patcher = mock.patch.object(self.m, "BACKUP_SNAPSHOT_DIR", self.snapshot_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reset_flags)
        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)

    # ---- plumbing ----------------------------------------------------
    def _put_back(self):
        for name in self.names:
            path = os.path.join(self.m.DATA_DIR, name)
            if name in self._originals:
                with open(path, "wb") as handle:
                    handle.write(self._originals[name])
            elif os.path.exists(path):
                os.remove(path)

    def _reset_flags(self):
        self.m._restore_in_progress = False
        self.m._restart_required = False

    def user_id(self, name):
        return next(u["id"] for u in self.store.list_accounts() if u["username"] == name)

    def admin_post(self, path, body):
        return self.local.post(path, json=body)

    def make_user(self, name="alex", **kwargs):
        defaults = {"sources": [A], "destinations": [D1]}
        defaults.update(kwargs)
        result = self.store.create(name, USER_PASSWORD, luma_users.ROLE_USER,
                                   luma_users.Permissions(**defaults))
        self.assertTrue(result["ok"], result)
        return result["user"]

    def direct(self, action, name="alex"):
        return self.admin_post("/api/admin/users/%s/direct_access" % self.user_id(name),
                               {"action": action}).get_json()

    def open_link(self, link):
        client = self.m.APP.test_client()
        client.environ_base["luma.listener"] = "remote"
        response = client.get("/access/" + link.rsplit("/", 1)[1])
        return client, response

    def sign_in(self, name, password=USER_PASSWORD):
        return self.remote.sign_in(name, password)

    def export(self):
        self.m._save_persisted_config()
        response = self.local.post("/api/backup/export", json={"passphrase": PASS})
        self.assertEqual(response.status_code, 200)
        return response.data

    def restore(self, blob):
        return self.local.post(
            "/api/backup/restore", content_type="multipart/form-data",
            data={"backup": (io.BytesIO(blob), "b.lumabak"), "passphrase": PASS})

    def inspect(self, blob):
        return self.local.post(
            "/api/backup/inspect", content_type="multipart/form-data",
            data={"backup": (io.BytesIO(blob), "b.lumabak"), "passphrase": PASS})

    def status(self, client, path="/api/user/state"):
        return client.get(path).status_code


class AccountsTheBackupDoesNotHoldTests(Base):
    def test_a_removed_accounts_password_session_ends(self):
        self.make_user("alex")
        blob = self.export()
        self.make_user("zed")
        zed = self.sign_in("zed")
        self.assertEqual(self.status(zed), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(zed), 401)

    def test_a_removed_accounts_direct_access_session_and_link_end(self):
        self.make_user("alex")
        blob = self.export()
        self.make_user("zed")
        link = self.direct("enable", "zed")["link"]
        client, response = self.open_link(link)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status(client), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(client), 401)
        self.assertEqual(self.open_link(link)[1].status_code, 404)

    def test_an_account_replaced_by_a_new_record_of_the_same_name_does_not_inherit_a_session(self):
        # Same username, different identity inside the store: the session names the old record.
        self.make_user("alex")
        blob = self.export()
        self.store.delete(self.user_id("alex"))
        self.make_user("alex")
        old = self.sign_in("alex")
        self.assertEqual(self.status(old), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(old), 401)


class CredentialsRestoredToOtherValuesTests(Base):
    def test_a_password_that_the_restore_changed_ends_the_sessions_made_with_the_other_one(self):
        self.make_user("alex")
        blob = self.export()
        self.admin_post("/api/admin/users/" + self.user_id("alex"),
                        {"action": "set_password", "password": NEW_PASSWORD})
        newer = self.sign_in("alex", NEW_PASSWORD)
        self.assertEqual(self.status(newer), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(newer), 401, "a session made with a password the restore replaced")
        # And the restored password is the one that works.
        fresh = self.sign_in("alex", USER_PASSWORD)
        self.assertEqual(self.status(fresh), 200)
        refused = self.m.APP.test_client()
        refused.environ_base["luma.listener"] = "remote"
        self.assertEqual(refused.post("/api/auth/login", json={"username": "alex",
                                                               "password": NEW_PASSWORD}).status_code, 401)

    def test_a_link_the_restore_replaced_ends_the_session_it_opened(self):
        self.make_user("alex")
        self.direct("enable")
        blob = self.export()
        newer_link = self.direct("regenerate")["link"]
        client, response = self.open_link(newer_link)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status(client), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(client), 401, "a session opened by a link the restore replaced")
        self.assertEqual(self.open_link(newer_link)[1].status_code, 404)

    def test_a_link_the_backup_did_not_have_ends_the_session_it_opened(self):
        self.make_user("alex")
        blob = self.export()
        link = self.direct("enable")["link"]
        client, _ = self.open_link(link)
        self.assertEqual(self.status(client), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(client), 401)
        self.assertEqual(self.open_link(link)[1].status_code, 404)

    def test_the_backups_own_link_still_works_after_the_restore(self):
        self.make_user("alex")
        link = self.direct("enable")["link"]
        blob = self.export()
        self.direct("regenerate")
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.open_link(link)[1].status_code, 200)


class AccountsRestoredWithLessTests(Base):
    def test_an_account_restored_disabled_loses_its_sessions(self):
        self.make_user("alex")
        self.admin_post("/api/admin/users/" + self.user_id("alex"), {"action": "set_enabled", "enabled": False})
        blob = self.export()
        self.admin_post("/api/admin/users/" + self.user_id("alex"), {"action": "set_enabled", "enabled": True})
        client = self.sign_in("alex")
        self.assertEqual(self.status(client), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(client), 401)

    def test_reduced_permissions_apply_to_the_open_session_at_its_next_request(self):
        self.make_user("alex", matrix_routing=False)
        blob = self.export()
        self.admin_post("/api/admin/users/" + self.user_id("alex"), {
            "action": "set_permissions",
            "permissions": {"sources": [A], "destinations": [D1], "groups": [],
                            "standard_routing": True, "matrix_routing": True}})
        client = self.sign_in("alex")
        self.assertEqual(self.status(client, "/api/user/matrix"), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        # The account came back with other permissions, so the session opened for the old ones ends.
        self.assertEqual(self.status(client, "/api/user/matrix"), 401, "Matrix was taken away by the restore")
        self.assertEqual(self.status(client), 401, "a session survived a change of permissions")
        # Signing in again gives the restored account, without Matrix.
        again = self.sign_in("alex")
        self.assertEqual(self.status(again, "/api/user/matrix"), 403)
        self.assertEqual(self.status(again), 200)

    def test_a_source_removed_by_the_restore_ends_the_open_session_and_is_gone_from_the_next(self):
        self.make_user("alex", sources=[A])
        blob = self.export()
        self.admin_post("/api/admin/users/" + self.user_id("alex"), {
            "action": "set_permissions",
            "permissions": {"sources": [A, B], "destinations": [D1], "groups": []}})
        client = self.sign_in("alex")
        ids = [s["id"] for s in client.get("/api/user/state").get_json()["sources"]]
        self.assertIn(B, ids)
        self.restore(blob)
        self.assertEqual(self.status(client), 401, "the session outlived a change of sources")
        ids = [s["id"] for s in self.sign_in("alex").get("/api/user/state").get_json()["sources"]]
        self.assertEqual(ids, [A])

    def test_a_demotion_by_the_restore_takes_the_administrative_surface_away(self):
        self.make_user("alex")
        self.admin_post("/api/admin/users/" + self.user_id("alex"), {"action": "set_role", "role": "admin"})
        self.admin_post("/api/admin/users/" + self.user_id("alex"), {"action": "set_role", "role": "user"})
        blob = self.export()
        self.admin_post("/api/admin/users/" + self.user_id("alex"), {"action": "set_role", "role": "admin"})
        admin = self.sign_in("alex")
        self.assertEqual(admin.get("/api/admin/users").status_code, 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(admin.get("/api/admin/users").status_code, 401,
                         "the session opened for an administrator ended when the restore demoted the account")


class AccountsThatDidNotChangeTests(Base):
    def test_an_account_the_restore_left_exactly_as_it_was_keeps_its_session(self):
        self.make_user("alex")
        link = self.direct("enable")["link"]
        blob = self.export()
        password_client = self.sign_in("alex")
        link_client, _ = self.open_link(link)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(password_client), 200)
        self.assertEqual(self.status(link_client), 200)


class TheRestoreSaysWhatItWillDoToAccountsTests(Base):
    def test_inspect_names_the_accounts_that_will_be_removed_replaced_and_added(self):
        self.make_user("alex")
        self.make_user("kept")
        blob = self.export()
        self.store.delete(self.user_id("alex"))
        self.make_user("zed")
        self.admin_post("/api/admin/users/" + self.user_id("kept"),
                        {"action": "set_password", "password": NEW_PASSWORD})
        body = self.inspect(blob).get_json()
        self.assertTrue(body["ok"], body)
        accounts = body["summary"]["accounts"]
        self.assertEqual(accounts["removed"], ["zed"])
        self.assertEqual(accounts["added"], ["alex"])
        self.assertIn("kept", accounts["replaced"])
        notes = " ".join(body["summary"]["notes"]).lower()
        self.assertIn("1 account", notes)
        self.assertIn("removed", notes)

    def test_nothing_is_claimed_when_the_accounts_are_the_same(self):
        self.make_user("alex")
        blob = self.export()
        accounts = self.inspect(blob).get_json()["summary"]["accounts"]
        self.assertEqual((accounts["removed"], accounts["added"]), ([], []))


class ABackupWithNoAccountsTests(Base):
    """Accounts are part of the authoritative set: a backup that held none removes the current ones."""

    def setUp(self):
        super().setUp()
        # A backup made on an installation that has never had an account. The last
        # administrator cannot be deleted through the store, so the store is emptied as a
        # fresh installation would be.
        with self.store._lock:
            self.store._accounts.clear()
            self.store._sessions.clear()
        self.assertTrue(self.store.is_empty)
        users_path = os.path.join(self.m.DATA_DIR, "users.json")
        if os.path.exists(users_path):
            os.remove(users_path)
        self.blob = self.export()
        # Then accounts appear on this computer, with sessions and a link.
        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.make_user("alex")

    def test_the_inspection_warns_that_accounts_will_be_removed(self):
        body = self.inspect(self.blob).get_json()
        self.assertTrue(body["ok"], body)
        removed = body["summary"]["accounts"]["removed"]
        self.assertEqual(sorted(removed), ["alex", "root"])
        self.assertIn("will_remove", body["summary"])
        self.assertIn("accounts", body["summary"]["will_remove"])

    def test_restoring_removes_every_account_and_every_session_and_link(self):
        link = self.direct("enable")["link"]
        link_client, _ = self.open_link(link)
        password_client = self.sign_in("alex")
        response = self.restore(self.blob)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.store.list_accounts(), [])
        self.assertEqual(self.status(password_client), 401)
        self.assertEqual(self.status(link_client), 401)
        self.assertEqual(self.open_link(link)[1].status_code, 404)

    def test_the_local_operator_can_still_create_an_administrator_afterwards(self):
        self.restore(self.blob)
        response = self.admin_post("/api/admin/users", {"username": "recovery", "password": NEW_PASSWORD,
                                                       "role": "admin"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        client = self.sign_in("recovery", NEW_PASSWORD)
        self.assertEqual(client.get("/api/admin/users").status_code, 200)

    def test_a_restore_response_tells_the_operator_how_to_recover(self):
        body = self.restore(self.blob).get_json()
        self.assertIn("admin_recovery", body)
        self.assertEqual(body["admin_recovery"]["administrators"], [])
        note = body["admin_recovery"]["note"]
        self.assertIn("No administrator account exists", note)
        self.assertIn("create one", note)

    def test_the_note_is_the_ordinary_one_when_an_administrator_survives(self):
        # The backup in this class held no accounts; build one that did.
        self.store.create("keeper", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.m._save_persisted_config()
        with_accounts = self.export()
        body = self.restore(with_accounts).get_json()
        self.assertTrue(body["admin_recovery"]["administrators"])
        self.assertNotIn("No administrator account exists", body["admin_recovery"]["note"])


class AFailedRestoreLeavesSessionsAloneTests(Base):
    def test_a_restore_that_fails_changes_no_session(self):
        self.make_user("alex")
        blob = self.export()
        self.admin_post("/api/admin/users/" + self.user_id("alex"),
                        {"action": "set_password", "password": NEW_PASSWORD})
        newer = self.sign_in("alex", NEW_PASSWORD)
        real = self.m._restore_write

        def failing(path, content):
            raise OSError("the disk went away")
        self.m._restore_write = failing
        self.addCleanup(setattr, self.m, "_restore_write", real)
        response = self.restore(blob)
        self.assertEqual(response.status_code, 500)
        self.m._restore_write = real
        self.assertEqual(self.status(newer), 200, "nothing was restored, so nothing was revoked")


GROUP, WALL = "grp-restore-a", "wall-restore-a"
KNOWN_GRANT = dict(sources=[A], destinations=[D1], groups=[GROUP], walls=[WALL],
                   standard_routing=True, matrix_routing=True)


class UnchangedAccountsKeepEverythingTests(Base):
    def test_an_unchanged_administrator_keeps_the_session_and_the_surface(self):
        blob = self.export()
        root = self.sign_in("root", ADMIN_PASSWORD)
        self.assertEqual(self.status(root, "/api/admin/users"), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(root, "/api/admin/users"), 200, "an untouched administrator was signed out")

    def test_an_unchanged_user_with_every_grant_keeps_both_kinds_of_session(self):
        self.make_user("alex", **KNOWN_GRANT)
        link = self.direct("enable")["link"]
        blob = self.export()
        password_client = self.sign_in("alex")
        link_client, _ = self.open_link(link)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(password_client), 200)
        self.assertEqual(self.status(link_client), 200)


class AdministratorsThatChangedTests(Base):
    def setUp(self):
        super().setUp()
        self.store.create("boss", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.boss_id = self.user_id("boss")

    def test_an_administrator_restored_with_another_password_is_signed_out(self):
        blob = self.export()
        self.store.set_password(self.boss_id, NEW_PASSWORD)
        newer = self.sign_in("boss", NEW_PASSWORD)
        self.assertEqual(self.status(newer, "/api/admin/users"), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(newer, "/api/admin/users"), 401)

    def test_an_administrator_restored_with_another_role_is_signed_out(self):
        blob = self.export()
        self.store.set_role(self.boss_id, "user")
        demoted = self.sign_in("boss", ADMIN_PASSWORD)
        self.assertEqual(self.status(demoted), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(demoted), 401, "the role the session was opened under was replaced")

    def test_an_administrator_restored_with_other_permissions_is_signed_out(self):
        blob = self.export()
        self.store.set_permissions(self.boss_id, luma_users.Permissions(sources=[A]))
        client = self.sign_in("boss", ADMIN_PASSWORD)
        self.assertEqual(self.status(client, "/api/admin/users"), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(client, "/api/admin/users"), 401)

    def test_an_administrator_the_backup_does_not_hold_is_signed_out(self):
        blob = self.export()
        self.store.create("late", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        late = self.sign_in("late", ADMIN_PASSWORD)
        self.assertEqual(self.status(late, "/api/admin/users"), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(late, "/api/admin/users"), 401)

    def test_an_administrator_restored_disabled_is_signed_out(self):
        self.store.set_enabled(self.boss_id, False)
        blob = self.export()
        self.store.set_enabled(self.boss_id, True)
        client = self.sign_in("boss", ADMIN_PASSWORD)
        self.assertEqual(self.status(client, "/api/admin/users"), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(client, "/api/admin/users"), 401)

    def test_the_administrator_who_did_not_change_stays_signed_in_beside_one_who_did(self):
        blob = self.export()
        self.store.set_password(self.boss_id, NEW_PASSWORD)
        root = self.sign_in("root", ADMIN_PASSWORD)
        boss = self.sign_in("boss", NEW_PASSWORD)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(root, "/api/admin/users"), 200)
        self.assertEqual(self.status(boss, "/api/admin/users"), 401)


class EachPermissionFieldAloneTests(Base):
    """One field differs at a time, from a known grant, so a forgotten field cannot hide behind another."""

    def setUp(self):
        super().setUp()
        self.make_user("alex", **KNOWN_GRANT)
        self.blob = self.export()
        self.uid = self.user_id("alex")

    def differs_in(self, **changed):
        grant = dict(KNOWN_GRANT)
        grant.update(changed)
        self.assertNotEqual(luma_users.Permissions(**grant).to_json(),
                            luma_users.Permissions(**KNOWN_GRANT).to_json(), "the case changes nothing")
        self.store.set_permissions(self.uid, luma_users.Permissions(**grant))
        client = self.sign_in("alex")
        self.assertEqual(self.status(client), 200)
        self.assertEqual(self.restore(self.blob).status_code, 200)
        self.assertEqual(self.status(client), 401, "a session survived a change of %s" % sorted(changed))
        self.assertEqual(self.status(self.sign_in("alex")), 200, "the restored account itself works")

    def test_sources(self):
        self.differs_in(sources=[A, B])

    def test_destinations(self):
        self.differs_in(destinations=[D1, D2])

    def test_groups(self):
        self.differs_in(groups=[GROUP, "grp-restore-b"])

    def test_walls(self):
        self.differs_in(walls=[WALL, "wall-restore-b"])

    def test_standard_routing(self):
        self.differs_in(standard_routing=False)

    def test_matrix_routing(self):
        self.differs_in(matrix_routing=False)

    def test_the_same_grant_in_another_order_is_not_a_change(self):
        self.store.set_permissions(self.uid, luma_users.Permissions(
            sources=[A], destinations=[D1], groups=[GROUP], walls=[WALL], standard_routing=True, matrix_routing=True))
        client = self.sign_in("alex")
        self.assertEqual(self.restore(self.blob).status_code, 200)
        self.assertEqual(self.status(client), 200)


class OtherAccountFieldsTests(Base):
    def test_a_role_that_differs_ends_a_users_session(self):
        self.make_user("alex")
        blob = self.export()
        self.store.set_role(self.user_id("alex"), "admin")
        client = self.sign_in("alex")
        self.assertEqual(self.status(client, "/api/admin/users"), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(client, "/api/admin/users"), 401)

    def test_an_enabled_state_that_differs_ends_a_session_the_table_still_holds(self):
        # Switching an account off normally takes its sessions with it. Put one back, as a session
        # that outlived the switch would be, so the enabled field itself is what decides.
        self.make_user("alex")
        uid = self.user_id("alex")
        blob = self.export()
        self.store.set_enabled(uid, False)
        token = "held-across-the-switch"
        with self.store._lock:
            self.store._sessions[token] = luma_users.Session(token, uid, "csrf")
        self.assertEqual(self.restore(blob).status_code, 200)
        with self.store._lock:
            self.assertNotIn(token, self.store._sessions)

    def test_a_direct_access_session_ends_when_the_password_of_its_account_differs(self):
        self.make_user("alex")
        link = self.direct("enable")["link"]
        blob = self.export()
        self.store.set_password(self.user_id("alex"), NEW_PASSWORD)
        client, response = self.open_link(link)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status(client), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(client), 401)

    def test_a_direct_access_session_ends_when_the_permissions_of_its_account_differ(self):
        self.make_user("alex", sources=[A])
        link = self.direct("enable")["link"]
        blob = self.export()
        self.store.set_permissions(self.user_id("alex"), luma_users.Permissions(sources=[A, B], destinations=[D1]))
        client, _ = self.open_link(link)
        self.assertEqual(self.status(client), 200)
        self.assertEqual(self.restore(blob).status_code, 200)
        self.assertEqual(self.status(client), 401)
        # The link is the backup's own, so it still opens a session -- for the restored account.
        again, response = self.open_link(link)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s["id"] for s in again.get("/api/user/state").get_json()["sources"]], [A])

    def test_the_count_says_why_each_session_ended(self):
        # The store is asked directly: what the fingerprint held when the sessions were opened is
        # edited one field at a time, as the restore would find it different.
        self.make_user("alex", sources=[A], destinations=[D1])
        link = self.direct("enable")["link"]
        uid = self.user_id("alex")
        cases = (("password", "password", dict(password="a different hash")),
                 ("changed", "role", dict(role="admin")),
                 ("changed", "sources", dict(sources=["somebody else"])),
                 ("changed", "enabled", dict(enabled=False)),
                 ("gone", "an account that did not exist", None))
        for expected, label, edit in cases:
            self.sign_in("alex")
            before = self.store.credential_fingerprints()
            if edit is None:
                before = {}
            else:
                before[uid] = dict(before[uid], **edit)
            ended = self.store.revoke_sessions_outdated_by(before)
            self.assertEqual(ended[expected], 1, "%s: %s" % (label, ended))
            self.assertEqual(sum(ended.values()), 1, "%s: %s" % (label, ended))
        # A link session whose link differs says "direct"; one whose password differs says "changed".
        client, _ = self.open_link(link)
        before = self.store.credential_fingerprints()
        before[uid] = dict(before[uid], direct="another link")
        self.assertEqual(self.store.revoke_sessions_outdated_by(before)["direct"], 1)
        client, _ = self.open_link(link)
        before = self.store.credential_fingerprints()
        before[uid] = dict(before[uid], password="another hash")
        self.assertEqual(self.store.revoke_sessions_outdated_by(before)["changed"], 1)


class NothingTheRequestCarriesKeepsASessionTests(Base):
    def test_no_field_of_the_request_keeps_a_session_of_an_account_that_changed(self):
        self.make_user("alex", sources=[A])
        blob = self.export()
        self.store.set_permissions(self.user_id("alex"), luma_users.Permissions(sources=[A, B], destinations=[D1]))
        client = self.sign_in("alex")
        self.assertEqual(self.status(client), 200)
        data = {"backup": (io.BytesIO(blob), "b.lumabak"), "passphrase": PASS}
        for name in ("keep_sessions", "keepSessions", "preserve_sessions", "keep_signed_in", "sessions",
                     "revoke_sessions", "exit_multiview", "accept_insecure", "force"):
            data[name] = "true"
        response = self.local.post("/api/backup/restore?keep_sessions=1&sessions=keep",
                                   content_type="multipart/form-data", data=data,
                                   headers={"X-Keep-Sessions": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status(client), 401)

    def test_the_restore_handler_reads_no_such_field(self):
        import inspect
        handler = next(f for rule, f in ((r.rule, self.m.APP.view_functions[r.endpoint])
                                         for r in self.m.APP.url_map.iter_rules())
                       if rule == "/api/backup/restore")
        source = inspect.getsource(handler)
        for word in ("keep_sessions", "preserve", "keep_signed", "revoke_sessions"):
            self.assertNotIn(word, source)


class TheRestoredAccountIsAlsoJudgedLiveTests(Base):
    """The second line of defence: a session put back after the restore meets the restored account."""

    def test_a_session_the_revocation_did_not_catch_is_refused_by_the_per_request_check(self):
        self.make_user("alex", matrix_routing=False)
        blob = self.export()
        self.store.set_permissions(self.user_id("alex"), luma_users.Permissions(
            sources=[A], destinations=[D1], standard_routing=True, matrix_routing=True))
        client = self.sign_in("alex")
        self.assertEqual(self.status(client, "/api/user/matrix"), 200)
        with self.store._lock:
            held = dict(self.store._sessions)
        self.assertEqual(len([s for s in held.values() if s.user_id == self.user_id("alex")]), 1)
        self.assertEqual(self.restore(blob).status_code, 200)
        with self.store._lock:
            self.assertFalse([s for s in self.store._sessions.values() if s.user_id == self.user_id("alex")],
                             "the first line of defence did not end the session")
            self.store._sessions.update(held)               # the revocation is taken out of the way
        self.assertEqual(self.status(client, "/api/user/matrix"), 403,
                         "the restored account was not what authorised this request")
        self.assertEqual(self.status(client), 200)


class TheRestoreNamesWhoWillBeSignedOutTests(Base):
    def setUp(self):
        super().setUp()
        self.make_user("alex", **KNOWN_GRANT)
        self.make_user("steady", **KNOWN_GRANT)
        self.blob = self.export()
        self.alex = self.user_id("alex")

    def replaced(self):
        return self.inspect(self.blob).get_json()["summary"]["accounts"]["replaced"]

    def test_an_account_whose_role_differs_is_named(self):
        self.store.set_role(self.alex, "admin")
        self.assertEqual(self.replaced(), ["alex"])

    def test_an_account_whose_permissions_differ_is_named(self):
        self.store.set_permissions(self.alex, luma_users.Permissions(**dict(KNOWN_GRANT, sources=[A, B])))
        self.assertEqual(self.replaced(), ["alex"])

    def test_an_account_whose_matrix_grant_differs_is_named(self):
        self.store.set_permissions(self.alex, luma_users.Permissions(**dict(KNOWN_GRANT, matrix_routing=False)))
        self.assertEqual(self.replaced(), ["alex"])

    def test_an_account_that_comes_back_the_same_is_not_named(self):
        self.assertEqual(self.replaced(), [])

    def test_the_wording_says_who_is_signed_out_and_who_stays(self):
        self.store.set_role(self.alex, "admin")
        body = self.inspect(self.blob).get_json()
        notes = " ".join(body["summary"]["notes"])
        self.assertIn("signed out", notes)
        self.assertIn("stays signed in", notes)
        self.assertNotIn("sessions made with the old password or link end", notes)


if __name__ == "__main__":
    unittest.main()
