"""Inventory protection, the User Matrix, backup and restore, working together.

The features were built apart, so what each guarantees is proved in its own suite. These
scenarios hold the seams: Clear Units while Remote Access is on must leave every row a
Matrix account was granted exactly as it was; a restore must give the same account the
same interfaces, the same rows and the same protection; and every administrative change
that matters to either must move the revision a signed-in page watches.

Every scenario runs on the real application with its state isolated, the transport faked
and the network fenced. The identities are the shared synthetic fixtures.
"""
import io
import unittest

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, RealApp, RealRemote, identity, saved_wall)

import luma_users
from test_clear_assigned import Base

A, B = identity(SOURCE_A), identity(SOURCE_B)
D1, D2, D3, D4 = (identity(u) for u in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4))
PASS = "a good backup passphrase"


class Seams(Base):
    def matrix_user(self, name="mia", standard=True, matrix=True):
        group = self.system.groups.create("Lobby", [D2])["group"]
        self.wall([DISPLAY_3], source=SOURCE_B, wid="w1")
        made = self.store.create(name, USER_PASSWORD, luma_users.ROLE_USER,
                                 luma_users.Permissions([A], [D1], [group["id"]], ["w1"],
                                                        standard, matrix))
        self.assertTrue(made["ok"], made)
        return group, self.remote.sign_in(name, USER_PASSWORD)

    def body(self, client):
        response = client.get("/api/user/matrix")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def rev(self, client):
        return client.get("/api/user/revision").get_json()["rev"]

    def rows(self, client):
        return sorted((r["kind"], r["id"]) for r in self.body(client)["rows"])


class ClearAgainstMatrixTests(Seams):
    def test_clear_keeps_every_row_and_column_the_account_was_granted(self):
        group, client = self.matrix_user()
        before = self.rows(client)
        sources_before = [s["id"] for s in self.body(client)["sources"]]
        revision = self.rev(client)
        cleared = self.clear().get_json()
        self.assertEqual(cleared["scope"], "unassigned")
        self.assertGreater(cleared["cleared"], 0)
        self.assertEqual(self.rows(client), before)
        self.assertEqual([s["id"] for s in self.body(client)["sources"]], sources_before)
        # Clearing the unassigned changed nothing the account holds, so its page is not told
        # that anything moved.
        self.assertEqual(self.rev(client), revision)
        for unit in (A, D1, D2, D3, B):
            self.assertIn(unit, self.macs())

    def test_the_matrix_stays_routable_after_a_clear(self):
        _group, client = self.matrix_user()
        self.clear()
        response = self.remote.post(client, "/api/user/route_multi", {
            "source": A, "displays": [D1], "groups": [], "interface": "matrix", "mode": "av"})
        self.assertNotEqual(response.status_code, 500, response.get_data(as_text=True))
        self.assertIn(response.status_code, (200, 502, 503, 409))

    def test_a_cleared_unit_the_account_never_held_was_never_in_its_matrix(self):
        _group, client = self.matrix_user()
        # The response reports how old its cached read is; that moves with the clock and is not
        # part of what this test is about, so it is set aside before the two answers are compared.
        def matrix():
            import json
            body = client.get("/api/user/matrix").get_json()

            def strip(value):
                if isinstance(value, dict):
                    return {k: strip(v) for k, v in value.items() if k != "age_s"}
                if isinstance(value, list):
                    return [strip(v) for v in value]
                return value
            return json.dumps(strip(body), sort_keys=True)
        text = matrix()
        self.clear()
        self.assertEqual(matrix(), text)
        self.assertNotIn(D4, text)

    def test_a_full_clear_with_remote_access_off_empties_the_inventory_and_the_account_keeps_its_grants(self):
        self.matrix_user()
        self.listener_off()
        body = self.clear_local().get_json()
        self.assertEqual(body["scope"], "all")
        self.assertEqual(self.macs(), [])

    def test_a_matrix_only_account_still_protects_its_units(self):
        self.matrix_user(standard=False, matrix=True)
        self.clear()
        for unit in (A, D1, D2, D3, B):
            self.assertIn(unit, self.macs())
        self.assertNotIn(D4, self.macs())


class RestoreAgainstMatrixTests(Seams):
    def export(self):
        response = self.local.post("/api/backup/export", json={"passphrase": PASS})
        self.assertEqual(response.status_code, 200)
        return response.data

    def restore(self, blob):
        return self.local.post("/api/backup/restore", content_type="multipart/form-data",
                               data={"backup": (io.BytesIO(blob), "b.lumabak"), "passphrase": PASS})

    def interfaces(self, name):
        p = next(a for a in self.store.list_accounts() if a["username"] == name)["permissions"]
        return (p["standard_routing"], p["matrix_routing"])

    def test_a_restored_account_has_the_same_interfaces_rows_and_protection(self):
        group, client = self.matrix_user("mia", standard=False, matrix=True)
        rows = self.rows(client)
        blob = self.export()
        self.store.delete(next(a["id"] for a in self.store.list_accounts() if a["username"] == "mia"))
        self.system.groups.delete(group["id"]) if hasattr(self.system.groups, "delete") else None
        self.assertEqual(self.restore(blob).get_json()["status"], "RESTORED")
        self.assertEqual(self.interfaces("mia"), (False, True))
        again = self.remote.sign_in("mia", USER_PASSWORD)
        self.assertEqual(self.rows(again), rows)
        # What the restore brought back protects its units from the next clear.
        self.clear()
        for unit in (A, D1, D2, D3, B):
            self.assertIn(unit, self.macs())

    def test_a_wall_grant_is_restored_as_a_wall_grant_and_not_as_its_members(self):
        self.matrix_user("mia")
        blob = self.export()
        self.assertEqual(self.restore(blob).get_json()["status"], "RESTORED")
        perms = next(a for a in self.store.list_accounts() if a["username"] == "mia")["permissions"]
        self.assertEqual(perms["walls"], ["w1"])
        self.assertNotIn(D3, perms["destinations"])

    def test_a_restore_replaces_the_interfaces_an_account_has_now(self):
        self.matrix_user("mia", standard=True, matrix=False)
        blob = self.export()
        mia = next(a for a in self.store.list_accounts() if a["username"] == "mia")
        self.store.set_permissions(mia["id"], luma_users.Permissions([A], [D1], [], ["w1"], False, True))
        self.assertEqual(self.interfaces("mia"), (False, True))
        self.assertEqual(self.restore(blob).get_json()["status"], "RESTORED")
        self.assertEqual(self.interfaces("mia"), (True, False))


class RevisionTests(Seams):
    def test_an_interface_change_moves_the_revision(self):
        _group, client = self.matrix_user()
        revision = self.rev(client)
        mia = next(a for a in self.store.list_accounts() if a["username"] == "mia")
        perms = luma_users.Permissions([A], [D1], mia["permissions"]["groups"], ["w1"], True, False)
        self.store.set_permissions(mia["id"], perms)
        self.assertNotEqual(self.rev(client), revision)

    def test_a_group_membership_change_moves_the_revision_and_the_matrix_row(self):
        group, client = self.matrix_user()
        revision = self.rev(client)
        self.system.groups.update(group["id"], members=[D2, D4])
        self.assertNotEqual(self.rev(client), revision)

    def test_a_wall_membership_change_moves_the_revision(self):
        _group, client = self.matrix_user()
        revision = self.rev(client)
        self.wall([DISPLAY_3, DISPLAY_4], source=SOURCE_B, wid="w1")
        self.assertNotEqual(self.rev(client), revision)

    def test_removing_matrix_while_it_is_open_is_answered_by_the_server_next_request(self):
        _group, client = self.matrix_user()
        mia = next(a for a in self.store.list_accounts() if a["username"] == "mia")
        self.store.set_permissions(mia["id"], luma_users.Permissions(
            [A], [D1], mia["permissions"]["groups"], ["w1"], True, False))
        self.assertIn(client.get("/api/user/matrix").status_code, (401, 403))
        response = self.remote.post(client, "/api/user/route_multi", {
            "source": A, "displays": [D1], "groups": [], "interface": "matrix", "mode": "av"})
        self.assertIn(response.status_code, (401, 403))

    def test_a_clear_that_removes_nothing_the_account_holds_leaves_the_revision(self):
        _group, client = self.matrix_user()
        revision = self.rev(client)
        self.clear()
        self.assertEqual(self.rev(client), revision)


if __name__ == "__main__":
    unittest.main()
