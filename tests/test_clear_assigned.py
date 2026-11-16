"""Clear Units while Remote Access is on keeps what Remote Access depends on.

Every scenario runs on the real application with its state isolated, the device transport
faked and the network fenced. The identities, addresses and names are the shared synthetic
fixtures; none of them is a lab value.
"""
import copy
import json
import os
import threading
import unittest

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, SOURCE_C, RealApp, RealRemote, identity,
                      saved_wall, wall_device_id)

import luma_audit_text
import luma_inventory
import luma_users

A, B, C = identity(SOURCE_A), identity(SOURCE_B), identity(SOURCE_C)
D1, D2, D3, D4 = (identity(DISPLAY_1), identity(DISPLAY_2), identity(DISPLAY_3),
                  identity(DISPLAY_4))


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.system = self.remote.remote
        self.store = self.remote.store
        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.local = self.m.APP.test_client()
        self.admin = self.remote.sign_in("root", ADMIN_PASSWORD)
        self.addCleanup(self._remove_walls)

    def _remove_walls(self):
        self.m._save_video_wall_state({"walls": [], "ignored_candidates": []})

    # ---- helpers -----------------------------------------------------
    def listener_off(self):
        listener = self.system.listener
        saved = (listener._server, listener._bound)
        listener._server, listener._bound = None, None

        def back():
            listener._server, listener._bound = saved
        self.addCleanup(back)

    def clear(self, body=None, client=None):
        return self.remote.post(client or self.admin, "/api/clear_units", body or {})

    def clear_local(self, body=None):
        return self.local.post("/api/clear_units", json=body or {})

    def ips(self):
        with self.m.cache_lock:
            return sorted(self.m.cache_units)

    def macs(self):
        with self.m.cache_lock:
            return sorted(identity(u) for u in self.m.cache_units.values())

    def user(self, name="alex", sources=(), destinations=(), groups=(), walls=()):
        result = self.store.create(name, USER_PASSWORD, luma_users.ROLE_USER,
                                   luma_users.Permissions(sources, destinations, groups, walls))
        self.assertTrue(result["ok"], result)
        return result["user"]

    def wall(self, members, source=None, wid="w1", name="Lobby Wall"):
        entry = saved_wall(wid, name, members, source_id=wall_device_id(source) if source else "")
        state = self.m._load_video_wall_state()
        state["walls"] = [w for w in state["walls"] if w.get("id") != wid] + [entry]
        self.m._save_video_wall_state(state)

    def perms(self, name="alex"):
        return next(a for a in self.store.list_accounts() if a["username"] == name)["permissions"]


class RemoteOffTests(Base):
    """Remote Access off keeps the full-clear behaviour."""

    def test_off_and_nothing_assigned_clears_everything(self):
        self.listener_off()
        body = self.clear_local().get_json()
        self.assertEqual((body["scope"], body["cleared"]), ("all", 7))
        self.assertEqual(self.ips(), [])

    def test_off_and_assigned_clears_everything_with_the_assignments(self):
        self.user(sources=[A], destinations=[D1])
        group = self.system.groups.create("Lobby", [D2])["group"]
        self.wall([DISPLAY_3])
        self.listener_off()
        body = self.clear_local().get_json()
        self.assertEqual(body["scope"], "all")
        self.assertEqual(self.ips(), [])
        self.assertEqual(self.perms()["sources"], [])
        self.assertEqual(self.system.groups.get(group["id"])["members"], [])


class RemoteOnTests(Base):
    def test_on_and_nothing_assigned_clears_everything(self):
        body = self.clear().get_json()
        self.assertEqual((body["scope"], body["cleared"], body["kept"]), ("unassigned", 7, 0))
        self.assertEqual(self.ips(), [])

    def test_on_and_everything_assigned_clears_nothing(self):
        self.user(sources=[A, B, C], destinations=[D1, D2, D3, D4])
        body = self.clear().get_json()
        self.assertEqual((body["cleared"], body["kept"]), (0, 7))
        self.assertEqual(len(self.ips()), 7)

    def test_mixed_inventory_keeps_exactly_the_assigned_units(self):
        # A by a user, D2 in a group, D1 in a wall, B / C / D3 / D4 unassigned.
        self.user(sources=[A])
        group = self.system.groups.create("Lobby", [D2])["group"]
        self.wall([DISPLAY_1])
        body = self.clear().get_json()
        self.assertEqual((body["cleared"], body["kept"]), (4, 3))
        self.assertEqual(self.macs(), sorted([A, D1, D2]))
        # Nothing Remote Access holds was edited.
        self.assertEqual(self.perms()["sources"], [A])
        self.assertEqual(self.system.groups.get(group["id"])["members"], [D2])
        self.assertEqual([w["id"] for w in self.m._load_video_wall_state()["walls"]], ["w1"])
        self.assertEqual((body["accounts_updated"], body["groups_updated"]), (0, 0))
        self.assertEqual(len([a for a in self.store.list_accounts()]), 2)

    def test_a_user_destination_alone_protects_a_unit(self):
        self.user(destinations=[D3])
        self.clear()
        self.assertEqual(self.macs(), [D3])

    def test_group_membership_alone_protects_a_unit(self):
        self.system.groups.create("Lobby", [D4])
        self.clear()
        self.assertEqual(self.macs(), [D4])

    def test_a_wall_member_is_protected_and_the_walls_source_too(self):
        self.wall([DISPLAY_2], source=SOURCE_C)
        self.clear()
        self.assertEqual(self.macs(), sorted([D2, C]))

    def test_a_wall_grant_does_not_protect_by_itself(self):
        # A grant to a wall protects through the wall's own members, not the grant row.
        self.wall([DISPLAY_2], wid="w1")
        self.user(walls=["w1"])
        self.clear()
        self.assertEqual(self.macs(), [D2])
        self.assertEqual(self.perms()["walls"], ["w1"])

    def test_a_disabled_account_still_protects_its_units(self):
        made = self.user(sources=[B])
        self.store.set_enabled(made["id"], False)
        self.clear()
        self.assertEqual(self.macs(), [B])

    def test_direct_access_accounts_protect_their_units(self):
        made = self.user(sources=[A], destinations=[D1])
        link = self.local.post("/api/admin/users/%s/direct_access" % made["id"],
                               json={"action": "enable"}).get_json()
        self.assertTrue(link["ok"], link)
        self.clear()
        self.assertEqual(self.macs(), sorted([A, D1]))
        direct = self.m.APP.test_client()
        direct.environ_base["luma.listener"] = "remote"
        self.assertEqual(direct.get("/access/" + link["link"].rsplit("/", 1)[1]).status_code, 200)
        state = direct.get("/api/user/state").get_json()
        self.assertEqual((len(state["sources"]), len(state["destinations"])), (1, 1))

    def test_a_unit_that_changed_address_is_still_the_same_assigned_unit(self):
        self.user(sources=[A])
        with self.m.cache_lock:
            moved = self.m.cache_units.pop(SOURCE_A["ip"])
            moved["ip"] = "192.0.2.77"
            self.m.cache_units["192.0.2.77"] = moved
        self.clear()
        self.assertEqual(self.ips(), ["192.0.2.77"])

    def test_a_unit_without_a_hardware_identity_is_never_assigned(self):
        with self.m.cache_lock:
            self.m.cache_units["192.0.2.88"] = {"ip": "192.0.2.88", "type": "encoder",
                                                "hostname": "no-mac"}
        self.user(sources=[A])
        self.clear()
        self.assertEqual(self.macs(), [A])

    def test_friendly_names_of_kept_units_stay_and_cleared_ones_go(self):
        self.user(sources=[A])
        self.system.aliases.set_alias(A, "Stage Camera")
        self.system.aliases.set_alias(B, "Spare Camera")
        self.clear()
        self.assertEqual(self.system.aliases.all(), {A: "Stage Camera"})

    def test_cleared_units_come_back_through_the_ordinary_path(self):
        self.user(sources=[A])
        self.clear()
        with self.m.cache_lock:
            self.m.cache_units[SOURCE_B["ip"]] = copy.deepcopy(SOURCE_B)
        ids = [d["id"] for d in self.admin.get("/api/admin/devices").get_json()["devices"]]
        self.assertEqual(sorted(ids), sorted([A, B]))
        self.assertEqual(self.perms()["sources"], [A])

    def test_sessions_and_permission_revision_do_not_change(self):
        made = self.user(sources=[A], destinations=[D1])
        client = self.remote.sign_in("alex", USER_PASSWORD)
        before = client.get("/api/user/revision").get_json()
        self.clear()
        self.assertEqual(client.get("/api/user/revision").get_json(), before)
        state = client.get("/api/user/state").get_json()
        self.assertEqual((len(state["sources"]), len(state["destinations"])), (1, 1))

    def test_the_inventory_survives_a_restart(self):
        self.user(sources=[A])
        self.clear()
        path = self.m.CACHE_FILE
        rows = json.load(open(path, encoding="utf-8"))
        self.assertEqual([r["mac"] for r in rows], [SOURCE_A["mac"]])
        with self.m.cache_lock:
            self.m.cache_units.clear()
        self.m._merge_disk_into_memory()
        self.assertEqual(self.macs(), [A])


class ManipulatedRequestTests(Base):
    BODIES = ({}, {"scope": "all"}, {"all": True}, {"force": True}, {"remote_access": False},
              {"remote_enabled": False}, {"clear_assigned": True}, {"unassigned_only": False},
              {"ips": [SOURCE_A["ip"]]}, {"identities": [A]})

    def test_no_request_body_removes_an_assigned_unit_while_remote_access_is_on(self):
        self.user(sources=[A], destinations=[D1])
        for body in self.BODIES:
            response = self.clear(body)
            self.assertEqual(response.status_code, 200, body)
            self.assertEqual(response.get_json()["scope"], "unassigned", body)
            self.assertEqual(self.macs(), sorted([A, D1]), body)
            for mac, unit in ((A, SOURCE_A), (B, SOURCE_B), (C, SOURCE_C), (D2, DISPLAY_2),
                              (D3, DISPLAY_3), (D4, DISPLAY_4)):
                with self.m.cache_lock:
                    self.m.cache_units.setdefault(unit["ip"], copy.deepcopy(unit))

    def test_headers_cannot_switch_the_mode(self):
        self.user(sources=[A])
        response = self.admin.post("/api/clear_units", json={},
                                   headers={"X-LumaSuite-CSRF": self.admin.csrf,
                                            "X-Remote-Access": "off"})
        self.assertEqual(response.get_json()["scope"], "unassigned")
        self.assertIn(A, self.macs())

    def test_a_restricted_user_cannot_clear_or_read_assignments(self):
        self.user(sources=[A])
        client = self.remote.sign_in("alex", USER_PASSWORD)
        self.assertEqual(self.remote.post(client, "/api/clear_units", {}).status_code, 403)
        self.assertEqual(client.get("/api/clear_units/assignments").status_code, 403)
        self.assertEqual(len(self.ips()), 7)

    def test_an_unauthenticated_remote_client_is_refused(self):
        anonymous = self.m.APP.test_client()
        anonymous.environ_base["luma.listener"] = "remote"
        self.assertIn(anonymous.post("/api/clear_units", json={}).status_code, (401, 403))
        self.assertIn(anonymous.get("/api/clear_units/assignments").status_code, (401, 403))
        self.assertEqual(len(self.ips()), 7)

    def test_the_gate_classifies_the_assignments_route(self):
        import luma_access
        self.assertEqual(luma_access.ROUTES["/api/clear_units/assignments"], luma_access.Access.ADMIN)


class AssignmentViewTests(Base):
    def test_the_assignments_view_says_why_each_unit_is_kept(self):
        self.user("alex", sources=[A])
        self.user("blair", sources=[A], destinations=[D1])
        self.system.groups.create("Lobby", [D1, D2])
        self.wall([DISPLAY_1], source=SOURCE_C, name="Stage Wall")
        self.system.aliases.set_alias(A, "Stage Camera")
        body = self.admin.get("/api/clear_units/assignments").get_json()
        self.assertTrue(body["remote_access"])
        self.assertEqual((body["protected"], body["clearable"]), (4, 3))
        rows = {r["id"]: r for r in body["units"]}
        self.assertEqual(set(rows), {A, D1, D2, C})
        self.assertEqual(rows[A]["users"], ["alex", "blair"])
        self.assertEqual(rows[A]["friendly_name"], "Stage Camera")
        self.assertEqual(rows[A]["type"], "Encoder")
        self.assertEqual(rows[D1]["users"], ["blair"])
        self.assertEqual(rows[D1]["groups"], ["Lobby"])
        self.assertEqual(rows[D1]["walls"], ["Stage Wall"])
        self.assertEqual(rows[D1]["count"], 3)
        self.assertEqual(rows[C]["walls"], ["Stage Wall"])
        self.assertEqual(rows[D2]["type"], "Decoder")

    def test_it_carries_no_secret_and_no_address(self):
        self.user(sources=[A])
        text = self.admin.get("/api/clear_units/assignments").get_data(as_text=True)
        for needle in (SOURCE_A["ip"], "password", "token", "csrf", "digest", "blob"):
            self.assertNotIn(needle, text.lower())

    def test_with_remote_access_off_it_lists_nothing_as_protected(self):
        self.user(sources=[A])
        self.listener_off()
        body = self.local.get("/api/clear_units/assignments").get_json()
        self.assertFalse(body["remote_access"])
        self.assertEqual((body["protected"], body["clearable"], body["units"]), (0, 7, []))

    def test_the_view_and_the_clear_agree(self):
        self.user(sources=[A])
        self.system.groups.create("Lobby", [D3])
        self.wall([DISPLAY_4])
        view = self.admin.get("/api/clear_units/assignments").get_json()
        body = self.clear().get_json()
        self.assertEqual((view["protected"], view["clearable"]), (body["kept"], body["cleared"]))
        self.assertEqual(sorted(r["id"] for r in view["units"]), self.macs())

    def test_the_device_list_carries_the_same_derived_summary(self):
        self.user(sources=[A])
        self.system.groups.create("Lobby", [A, D2])
        self.wall([DISPLAY_2], name="Stage Wall")
        rows = {d["id"]: d for d in self.admin.get("/api/admin/devices").get_json()["devices"]}
        self.assertEqual(set(rows[A]["assignments"]), luma_inventory.ASSIGNMENT_FIELDS)
        self.assertEqual((rows[A]["assignments"]["users"], rows[A]["assignments"]["groups"]), (["alex"], ["Lobby"]))
        self.assertEqual(rows[D2]["assignments"]["walls"], ["Stage Wall"])
        self.assertEqual(rows[B]["assignments"], {"users": [], "direct_access": [],
                                                   "groups": [], "walls": [], "wall_reasons": [],
                                                   "wall_access": [], "count": 0})

    def test_the_device_summary_follows_every_administrative_change(self):
        def count(mac):
            rows = {d["id"]: d for d in self.admin.get("/api/admin/devices").get_json()["devices"]}
            return rows[mac]["assignments"]["count"]
        self.assertEqual(count(D1), 0)
        made = self.user(destinations=[D1])
        self.assertEqual(count(D1), 1)
        self.store.set_permissions(made["id"], luma_users.Permissions([], [], []))
        self.assertEqual(count(D1), 0)
        group = self.system.groups.create("Lobby", [D1])["group"]
        self.assertEqual(count(D1), 1)
        self.system.groups.update(group["id"], members=[D2])
        self.assertEqual((count(D1), count(D2)), (0, 1))
        self.wall([DISPLAY_1])
        self.assertEqual(count(D1), 1)
        self._remove_walls()
        self.assertEqual(count(D1), 0)
        self.store.delete(made["id"])
        self.system.groups.delete(group["id"])
        self.assertEqual(count(D2), 0)

    def test_direct_access_is_named_in_the_summary(self):
        made = self.user(sources=[A])
        self.local.post("/api/admin/users/%s/direct_access" % made["id"], json={"action": "enable"})
        rows = {d["id"]: d for d in self.admin.get("/api/admin/devices").get_json()["devices"]}
        self.assertEqual(rows[A]["assignments"]["direct_access"], ["alex"])


class RaceTests(Base):
    """The decision and the removal are one critical section."""

    def _clear_with_pause(self, intruder):
        """Run a clear that stalls between classifying and removing, with `intruder` racing it."""
        order = []
        classified = threading.Event()
        release = threading.Event()
        real_split = luma_inventory.split

        def slow_split(cache_units, assigned):
            result = real_split(cache_units, assigned)
            classified.set()
            release.wait(2.0)
            return result

        luma_inventory.split = slow_split
        self.addCleanup(setattr, luma_inventory, "split", real_split)
        outcome = {}
        worker = threading.Thread(target=lambda: outcome.update(body=self.clear().get_json()))
        worker.start()
        self.assertTrue(classified.wait(5.0), "the clear never reached its classification")
        racer = threading.Thread(target=lambda: (intruder(), order.append("intruder")))
        racer.start()
        racer.join(0.4)
        order.append("blocked" if racer.is_alive() else "ran-during-clear")
        release.set()
        worker.join(10)
        racer.join(10)
        return outcome["body"], order

    def test_a_group_member_added_while_clearing_waits_for_the_clear(self):
        group = self.system.groups.create("Lobby", [D1])["group"]
        body, order = self._clear_with_pause(
            lambda: self.system.groups.update(group["id"], members=[D1, D2]))
        self.assertEqual(order[0], "blocked")
        self.assertEqual(body["scope"], "unassigned")
        self.assertIn(D1, self.macs())

    def test_a_grant_added_while_clearing_waits_for_the_clear(self):
        made = self.user(sources=[A])
        body, order = self._clear_with_pause(
            lambda: self.store.set_permissions(made["id"],
                                               luma_users.Permissions([A, B], [], [])))
        self.assertEqual(order[0], "blocked")
        self.assertIn(A, self.macs())

    def test_a_wall_saved_while_clearing_waits_for_the_clear(self):
        body, order = self._clear_with_pause(lambda: self.wall([DISPLAY_2]))
        self.assertEqual(order[0], "blocked")

    def test_a_scan_result_arriving_while_clearing_is_not_swept_away(self):
        arrival = copy.deepcopy(SOURCE_C)
        arrival["ip"] = "192.0.2.99"
        arrival["mac"] = "02:00:5e:10:00:99"

        def scan_lands():
            with self.m.cache_lock:
                self.m.cache_units[arrival["ip"]] = arrival
        body, order = self._clear_with_pause(scan_lands)
        self.assertEqual(order[0], "blocked")
        self.assertIn("02:00:5e:10:00:99", self.macs())

    def test_a_read_that_began_before_the_clear_cannot_repopulate_it(self):
        generation = self.m.cache_generation
        self.clear()
        self.assertGreater(self.m.cache_generation, generation)

    def test_a_failed_inventory_write_removes_nothing_and_keeps_the_names(self):
        self.system.aliases.set_alias(B, "Spare Camera")
        self.user(sources=[A])
        real = self.m.luma_store.atomic_write_json

        def fail_for_the_inventory(path, payload, *a, **k):
            return False if str(path) == str(self.m.CACHE_FILE) else real(path, payload, *a, **k)

        from unittest import mock
        with mock.patch.object(self.m.luma_store, "atomic_write_json", fail_for_the_inventory):
            response = self.clear()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(self.ips()), 7)
        self.assertEqual(self.system.aliases.all(), {B: "Spare Camera"})

    def test_a_failure_updating_names_clears_nothing(self):
        self.system.aliases.set_alias(B, "Spare Camera")
        original = self.system.aliases.forget_many
        self.system.aliases.forget_many = lambda ids: {"ok": False, "removed": 0, "before": {}}
        try:
            response = self.clear()
        finally:
            self.system.aliases.forget_many = original
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(self.ips()), 7)


class AuditTests(Base):
    def test_clearing_unassigned_units_is_audited_with_counts_and_no_secret(self):
        self.user(sources=[A])
        self.clear()
        entry = [e for e in self.store.audit_entries(limit=50) if e["event"] == "units.cleared_unassigned"][0]
        self.assertEqual((entry["detail"]["cleared"], entry["detail"]["kept"]), (6, 1))
        self.assertIn("unassigned", luma_audit_text.event_label("units.cleared_unassigned").lower())
        self.assertNotIn("password", json.dumps(entry).lower())

    def test_a_full_clear_is_audited_under_its_own_event(self):
        self.listener_off()
        self.clear_local()
        events = [e["event"] for e in self.store.audit_entries(limit=50)]
        self.assertIn("units.cleared", events)
        self.assertNotIn("units.cleared_unassigned", events)


if __name__ == "__main__":
    unittest.main()
