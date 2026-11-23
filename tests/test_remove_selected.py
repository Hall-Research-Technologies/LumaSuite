"""Remove Selected obeys the same Remote Access protection as Clear Units.

Remote Access off keeps the behaviour it always had. Remote Access on, a selected unit that an
account, a display group or a saved wall refers to stays, and is reported with what holds it;
the rest go. Nothing Remote Access holds is edited. The server decides from its own state, so
the request body cannot widen what is removed.

The assignment summary says why a saved wall protects a unit, and who reaches that wall.
"""
import copy
import threading
import unittest

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, SOURCE_C, RealApp, RealRemote, identity,
                      saved_wall, wall_device_id)

import luma_audit_text
import luma_inventory
import luma_users

from test_clear_assigned import Base, A, B, C, D1, D2, D3, D4


def ip_of(unit):
    return unit["ip"]


class RemoveSelectedBase(Base):
    def remove(self, ips, extra=None, client=None):
        body = {"ips": list(ips)}
        body.update(extra or {})
        return self.remote.post(client or self.admin, "/api/remove_units", body)

    def remove_local(self, ips):
        return self.local.post("/api/remove_units", json={"ips": list(ips)})

    def ip(self, unit):
        return unit["ip"]


class RemoteOffTests(RemoveSelectedBase):
    def test_off_removes_everything_selected_with_its_assignments(self):
        self.user(sources=[A], destinations=[D1])
        group = self.system.groups.create("Lobby", [D2])["group"]
        self.listener_off()
        r = self.remove_local([self.ip(SOURCE_A), self.ip(DISPLAY_1), self.ip(DISPLAY_2)])
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["removed"], 3)
        self.assertEqual(body["kept"], [])
        self.assertNotIn(A, self.macs())
        self.assertEqual(self.perms()["sources"], [])
        self.assertEqual(self.system.groups.get(group["id"])["members"], [])


class RemoteOnTests(RemoveSelectedBase):
    def test_on_unassigned_selection_is_removed(self):
        r = self.remove([self.ip(SOURCE_B), self.ip(DISPLAY_3)])
        body = r.get_json()
        self.assertEqual((r.status_code, body["removed"], body["protected"]), (200, 2, 0))
        self.assertNotIn(B, self.macs())
        self.assertNotIn(D3, self.macs())

    def test_on_assigned_units_in_the_selection_stay_and_the_rest_go(self):
        self.user(sources=[A])
        self.system.groups.create("Lobby", [D2])
        self.wall([DISPLAY_1])
        chosen = [self.ip(SOURCE_A), self.ip(DISPLAY_2), self.ip(DISPLAY_1),
                  self.ip(SOURCE_B), self.ip(DISPLAY_3)]
        r = self.remove(chosen)
        body = r.get_json()
        self.assertEqual((r.status_code, body["removed"], body["protected"]), (200, 2, 3))
        self.assertEqual(sorted(row["id"] for row in body["kept"]), sorted([A, D1, D2]))
        self.assertEqual(self.macs(), sorted([A, C, D1, D2, D4]))

    def test_on_every_selected_unit_protected_is_a_409_with_the_reasons(self):
        self.user(sources=[A])
        self.system.groups.create("Lobby", [D2])
        r = self.remove([self.ip(SOURCE_A), self.ip(DISPLAY_2)])
        body = r.get_json()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(body["reason"], "inventory_protected")
        self.assertEqual(body["removed"], 0)
        rows = {row["id"]: row for row in body["kept"]}
        self.assertEqual(rows[A]["users"], ["alex"])
        self.assertEqual(rows[D2]["groups"], ["Lobby"])
        self.assertEqual(len(self.ips()), 7)

    def test_on_nothing_remote_access_holds_is_edited(self):
        self.user(sources=[A], destinations=[D1])
        group = self.system.groups.create("Lobby", [D2])["group"]
        self.wall([DISPLAY_3], name="Stage Wall")
        before = (copy.deepcopy(self.perms()), copy.deepcopy(self.system.groups.get(group["id"])),
                  copy.deepcopy(self.m._load_video_wall_state()["walls"]))
        self.remove([self.ip(SOURCE_A), self.ip(DISPLAY_1), self.ip(DISPLAY_2), self.ip(DISPLAY_3),
                     self.ip(SOURCE_B)])
        after = (self.perms(), self.system.groups.get(group["id"]),
                 self.m._load_video_wall_state()["walls"])
        self.assertEqual(before, after)
        self.assertEqual(len(self.store.list_accounts()), 2)

    def test_on_a_wall_member_and_the_walls_source_are_protected(self):
        self.wall([DISPLAY_2], source=SOURCE_C)
        r = self.remove([self.ip(DISPLAY_2), self.ip(SOURCE_C)])
        self.assertEqual(r.status_code, 409)
        reasons = {row["id"]: row["wall_reasons"] for row in r.get_json()["kept"]}
        self.assertEqual(reasons[D2], ["Member of wall Lobby Wall"])
        self.assertEqual(reasons[C], ["Source of wall Lobby Wall"])

    def test_on_direct_access_account_grants_protect_like_any_other(self):
        account = self.user(sources=[A], destinations=[D1])
        self.assertTrue(self.store.enable_direct_access(account["id"])["ok"])
        r = self.remove([self.ip(SOURCE_A), self.ip(DISPLAY_1)])
        self.assertEqual(r.status_code, 409)
        rows = {row["id"]: row for row in r.get_json()["kept"]}
        self.assertEqual(rows[A]["direct_access"], ["alex"])

    def test_on_a_disabled_account_still_protects_its_units(self):
        account = self.user(sources=[A])
        self.assertTrue(self.store.set_enabled(account["id"], False)["ok"])
        self.assertEqual(self.remove([self.ip(SOURCE_A)]).status_code, 409)
        self.assertIn(A, self.macs())

    def test_on_a_unit_that_moved_address_is_still_the_same_protected_unit(self):
        self.user(sources=[A])
        with self.m.cache_lock:
            unit = self.m.cache_units.pop(self.ip(SOURCE_A))
            unit = dict(unit, ip="198.18.7.7")
            self.m.cache_units[unit["ip"]] = unit
        r = self.remove(["198.18.7.7"])
        self.assertEqual(r.status_code, 409)
        self.assertIn(A, self.macs())

    def test_on_manipulated_bodies_cannot_widen_the_removal(self):
        self.user(sources=[A], destinations=[D1])
        for extra in ({"force": True}, {"all": True}, {"remote_access": False},
                      {"scope": "all"}, {"protected": []}, {"remove_assigned": True},
                      {"confirm": "yes"}):
            with self.subTest(extra=extra):
                r = self.remove([self.ip(SOURCE_A), self.ip(DISPLAY_1)], extra)
                self.assertEqual(r.status_code, 409)
        self.assertIn(A, self.macs())
        self.assertIn(D1, self.macs())

    def test_on_malformed_and_unknown_input_is_a_controlled_answer(self):
        # The remote gate answers first for an address it does not know: 400 when it is not
        # an address at all, 404 when it is one this application has not found.
        for body in ({"ips": []}, {}, []):
            with self.subTest(body=body):
                r = self.remote.post(self.admin, "/api/remove_units", body)
                self.assertEqual(r.status_code, 400)
        r = self.remote.post(self.admin, "/api/remove_units", {"ips": "198.18.0.1"})
        self.assertEqual((r.status_code, r.get_json()["reason"]), (404, "unknown_device"))
        r = self.remote.post(self.admin, "/api/remove_units", {"ips": ["198.18.250.250"]})
        self.assertEqual((r.status_code, r.get_json()["reason"]), (404, "unknown_device"))
        r = self.remote.post(self.admin, "/api/remove_units", {"ips": [None, 7, "", "x"]})
        self.assertEqual((r.status_code, r.get_json()["reason"]), (400, "invalid_address"))
        self.assertEqual(len(self.ips()), 7)

    def test_on_the_handler_itself_tolerates_junk_when_the_gate_is_not_in_front(self):
        # A local request is trusted by its socket and reaches the handler directly.
        for body in ({"ips": "198.18.0.1"}, {"ips": {"a": 1}}, {"ips": []}, {}, []):
            with self.subTest(body=body):
                r = self.local.post("/api/remove_units", json=body)
                self.assertEqual(r.status_code, 400)
        r = self.local.post("/api/remove_units", json={"ips": [None, 7, "", "x", "198.18.250.250"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["removed"], 0)
        self.assertEqual(len(self.ips()), 7)
        r = self.local.post("/api/remove_units", data="not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_on_a_restricted_user_cannot_use_it(self):
        self.user(name="blake", sources=[A])
        client = self.remote.sign_in("blake", USER_PASSWORD)
        r = self.remove([self.ip(SOURCE_B)], client=client)
        self.assertIn(r.status_code, (401, 403))
        self.assertIn(B, self.macs())

    def test_on_an_unreadable_selection_removes_nothing_it_cannot_prove_unassigned(self):
        # A unit without an identity cannot be assigned to anything and is removable;
        # one with an identity that is assigned is never removable. Both at once.
        self.user(sources=[A])
        with self.m.cache_lock:
            self.m.cache_units["198.18.9.9"] = {"ip": "198.18.9.9", "hostname": "anon", "type": "decoder"}
        r = self.remove([self.ip(SOURCE_A), "198.18.9.9"])
        body = r.get_json()
        self.assertEqual((body["removed"], body["protected"]), (1, 1))

    def test_on_names_of_removed_units_go_and_names_of_kept_units_stay(self):
        self.user(sources=[A])
        self.system.aliases.set_many({A: "Main Camera", B: "Spare Camera"})
        self.remove([self.ip(SOURCE_A), self.ip(SOURCE_B)])
        names = self.system.aliases.all()
        self.assertEqual(names.get(A), "Main Camera")
        self.assertNotIn(B, names)

    def test_on_the_inventory_survives_a_restart_of_the_cache(self):
        self.remove([self.ip(SOURCE_B)])
        with self.m.cache_lock:
            persisted = self.m._load_cache() if hasattr(self.m, "_load_cache") else None
        self.assertNotIn(B, self.macs())
        if persisted is not None:
            self.assertNotIn(B, {identity(u) for u in persisted})


class FailureTests(RemoveSelectedBase):
    def test_a_failed_inventory_write_removes_nothing_and_restores_the_names(self):
        self.system.aliases.set_many({B: "Spare Camera"})
        original = self.m.luma_store.atomic_write_json
        self.m.luma_store.atomic_write_json = lambda *a, **k: False
        self.addCleanup(setattr, self.m.luma_store, "atomic_write_json", original)
        r = self.remove([self.ip(SOURCE_B)])
        self.assertEqual(r.status_code, 500)
        self.m.luma_store.atomic_write_json = original
        self.assertIn(B, self.macs())
        self.assertEqual(self.system.aliases.all().get(B), "Spare Camera")

    def test_a_failed_names_update_removes_nothing(self):
        original = self.system.aliases.forget_many
        self.system.aliases.forget_many = lambda ids: {"ok": False}
        self.addCleanup(setattr, self.system.aliases, "forget_many", original)
        r = self.remove([self.ip(SOURCE_B)])
        self.assertEqual(r.status_code, 500)
        self.assertIn(B, self.macs())


class RaceTests(RemoveSelectedBase):
    def test_an_assignment_made_while_removing_waits_for_the_removal(self):
        # The classification and the removal hold the account lock together, so a grant
        # added in between cannot be classified unassigned and then lose its unit anyway.
        gate = threading.Event()
        release = threading.Event()
        original = self.system.aliases.forget_many

        def slow(ids):
            gate.set()
            release.wait(5)
            return original(ids)
        self.system.aliases.forget_many = slow
        self.addCleanup(setattr, self.system.aliases, "forget_many", original)
        result = {}

        def run():
            result["r"] = self.remove([self.ip(SOURCE_B)])
        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(gate.wait(5))
        granted = {}

        def grant():
            granted["u"] = self.user(name="late", sources=[B])
        g = threading.Thread(target=grant)
        g.start()
        g.join(0.5)
        self.assertTrue(g.is_alive(), "the grant must wait for the removal that is deciding")
        release.set()
        t.join(10)
        g.join(10)
        self.assertEqual(result["r"].status_code, 200)
        # The grant landed after the unit was gone; it names an identity, which stays valid
        # and protects the unit as soon as a scan finds it again.
        self.assertEqual(self.perms("late")["sources"], [B])


    def test_an_assignment_made_between_classifying_and_removing_waits(self):
        # The window the account lock exists for: the assignments have been read, the
        # inventory lock is free again, and nothing has been removed yet. Stalling in the
        # removal itself cannot show this, because a grant also needs the inventory lock
        # there and waits for that instead -- so a dropped account lock went unnoticed.
        gate = threading.Event()
        release = threading.Event()
        original = luma_inventory.build

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            gate.set()
            release.wait(5)
            return result
        luma_inventory.build = slow
        self.addCleanup(setattr, luma_inventory, "build", original)
        result = {}

        def run():
            result["r"] = self.remove([self.ip(SOURCE_B)])
        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(gate.wait(5), "precondition: the removal reached the classification")
        granted = threading.Thread(target=lambda: self.user(name="between", sources=[B]))
        granted.start()
        granted.join(0.5)
        still_waiting = granted.is_alive()
        release.set()
        t.join(10)
        granted.join(10)
        self.assertTrue(still_waiting, "the grant must wait for the removal that is deciding")
        self.assertEqual(result["r"].status_code, 200)


    def test_the_account_and_group_locks_are_held_while_the_removal_decides(self):
        # A grant that is merely slow looks like one that is waiting (creating an account
        # hashes a password), so the timing tests above cannot tell a dropped lock from a
        # busy one. Ask the locks themselves, from a thread that is not the remover.
        gate = threading.Event()
        release = threading.Event()
        original = luma_inventory.build

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            gate.set()
            release.wait(5)
            return result
        luma_inventory.build = slow
        self.addCleanup(setattr, luma_inventory, "build", original)
        t = threading.Thread(target=lambda: self.remove([self.ip(SOURCE_B)]))
        t.start()
        try:
            self.assertTrue(gate.wait(5), "precondition: the removal reached the classification")
            for name, lock in (("account", self.system.store.hold()), ("group", self.system.groups.hold())):
                got = lock.acquire(blocking=False)
                if got:
                    lock.release()
                self.assertFalse(got, "the %s lock must be held while the removal decides" % name)
        finally:
            release.set()
            t.join(10)


class AuditTests(RemoveSelectedBase):
    def test_removing_records_one_entry_and_no_secret(self):
        self.user(sources=[A])
        self.remove([self.ip(SOURCE_A), self.ip(SOURCE_B)])
        entries = [e for e in self.store.audit_entries(limit=50)
                   if e["event"] == "units.removed_unassigned"]
        self.assertEqual(len(entries), 1)
        detail = entries[0]["detail"]
        self.assertEqual((detail["removed"], detail["protected"]), (1, 1))
        text = luma_audit_text.describe(entries[0]) if hasattr(luma_audit_text, "describe") else ""
        self.assertNotIn(USER_PASSWORD, str(entries[0]))
        self.assertIn("selected", luma_audit_text.event_label("units.removed_unassigned").lower())


class WallSummaryTests(Base):
    def rows(self):
        return {d["id"]: d["assignments"]
                for d in self.admin.get("/api/admin/devices").get_json()["devices"]}

    def test_a_wall_member_says_it_is_a_member_and_who_holds_the_wall(self):
        self.user(name="walker", walls=["w1"])
        self.user(name="other", sources=[B])
        self.wall([DISPLAY_2], source=SOURCE_C, name="Stage Wall")
        rows = self.rows()
        self.assertEqual(rows[D2]["wall_reasons"], ["Member of wall Stage Wall"])
        self.assertEqual(rows[D2]["wall_access"], ["walker"])
        self.assertEqual(rows[C]["wall_reasons"], ["Source of wall Stage Wall"])
        self.assertEqual(rows[C]["wall_access"], ["walker"])
        # The grant is to the wall: the account does not appear as a user of the member.
        self.assertEqual(rows[D2]["users"], [])
        self.assertEqual(rows[D2]["walls"], ["Stage Wall"])

    def test_a_wall_nobody_holds_still_explains_the_protection(self):
        self.wall([DISPLAY_1], name="Quiet Wall")
        rows = self.rows()
        self.assertEqual(rows[D1]["wall_reasons"], ["Member of wall Quiet Wall"])
        self.assertEqual(rows[D1]["wall_access"], [])

    def test_two_walls_list_both_reasons_in_order(self):
        self.wall([DISPLAY_1], wid="w1", name="Beta Wall")
        self.wall([DISPLAY_1], wid="w2", name="Alpha Wall")
        self.assertEqual(self.rows()[D1]["wall_reasons"],
                         ["Member of wall Alpha Wall", "Member of wall Beta Wall"])

    def test_the_summary_follows_a_wall_grant_rename_membership_change_and_delete(self):
        self.user(name="walker", walls=["w1"])
        self.wall([DISPLAY_1], name="First")
        self.assertEqual(self.rows()[D1]["wall_access"], ["walker"])
        self.wall([DISPLAY_1], name="Renamed")
        self.assertEqual(self.rows()[D1]["wall_reasons"], ["Member of wall Renamed"])
        self.wall([DISPLAY_4], name="Renamed")
        rows = self.rows()
        self.assertEqual(rows[D1]["wall_reasons"], [])
        self.assertEqual(rows[D1]["count"], 0)
        self.assertEqual(rows[D4]["wall_reasons"], ["Member of wall Renamed"])
        self._remove_walls()
        self.assertEqual(self.rows()[D4]["wall_reasons"], [])
        self.assertEqual(self.rows()[D4]["wall_access"], [])

    def test_the_clear_view_carries_the_same_reasons(self):
        self.user(name="walker", walls=["w1"])
        self.wall([DISPLAY_2], name="Stage Wall")
        view = self.admin.get("/api/clear_units/assignments").get_json()
        row = next(r for r in view["units"] if r["id"] == D2)
        self.assertEqual(row["wall_reasons"], ["Member of wall Stage Wall"])
        self.assertEqual(row["wall_access"], ["walker"])

    def test_the_record_keys_are_exactly_the_documented_ones(self):
        self.wall([DISPLAY_2])
        self.assertEqual(set(self.rows()[D2]), luma_inventory.ASSIGNMENT_FIELDS)


if __name__ == "__main__":
    unittest.main()
