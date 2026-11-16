"""Adversarial inventory protection: the combinations a single-feature test does not reach.

Every scenario is run against both removals (Clear Units and Remove Selected), with Remote Access
on and off, on the real application with isolated state. What these hold together:

  * an assigned identity cannot be destroyed while Remote Access is on, by any route and with any
    request body;
  * a unit is protected by who it is, not where it is: a unit that came back at another address, a
    unit that is offline, and an assignment to a unit that is not (yet) in the inventory;
  * the decision is made once, under the locks the assignments live behind, so an assignment made
    while it is being made either lands first or waits;
  * a removal that kept everything does not disturb a scan that is still running;
  * what was kept is still kept, and still assigned, after the stores are read back from disk.
"""
import copy
import threading
import unittest

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, SOURCE_C, RealApp, RealRemote, identity)

import luma_liveness
import luma_store
import luma_users

from test_clear_assigned import Base, A, B, C, D1, D2, D3, D4


class Inventory(Base):
    """Both removals behind one interface, so a scenario is written once."""

    def go(self, how, ips=None, client=None):
        if how == "clear":
            return self.clear(client=client)
        chosen = ips if ips is not None else [u["ip"] for u in self.units()]
        return self.remote.post(client or self.admin, "/api/remove_units", {"ips": list(chosen)})

    def go_local(self, how, ips=None):
        if how == "clear":
            return self.clear_local()
        chosen = ips if ips is not None else [u["ip"] for u in self.units()]
        return self.local.post("/api/remove_units", json={"ips": list(chosen)})

    def units(self):
        with self.m.cache_lock:
            return copy.deepcopy(list(self.m.cache_units.values()))

    def on_disk(self):
        rows = luma_store.read_json(self.m.CACHE_FILE, default=[])
        rows = rows if isinstance(rows, list) else rows.get("units", [])
        return sorted(identity(u) for u in rows if u.get("mac"))

    def generation(self):
        with self.m.cache_lock:
            return self.m.cache_generation

    def mark_offline(self, unit):
        with self.m.cache_lock:
            row = self.m.cache_units[unit["ip"]]
            luma_liveness.record(row, False)
            luma_liveness.record(row, False)
            assert luma_liveness.is_offline(row)

    def reload_stores_from_disk(self):
        """What a restart reads: every store rebuilt from its file."""
        self.system.store._load()
        self.system.aliases._load()
        self.system.groups.reload()


HOWS = ("clear", "remove")


class RemoteOnScenarioTests(Inventory):
    def test_every_scenario_for_both_removals(self):
        for how in HOWS:
            with self.subTest(how=how):
                self.setUp_again()
                self.user(sources=[A], destinations=[D1])
                self.system.groups.create("Lobby", [D2])
                self.wall([DISPLAY_3], source=SOURCE_C)
                body = self.go(how).get_json()
                self.assertEqual(self.macs(), sorted([A, C, D1, D2, D3]), how)
                self.assertEqual(body["cleared" if how == "clear" else "removed"], 2)
                self.assertEqual(self.on_disk(), sorted([A, C, D1, D2, D3]))

    def setUp_again(self):
        """A clean inventory and clean stores between the two removals of one scenario."""
        self.app.seed()
        with self.system.store._lock:
            self.system.store._accounts = {k: v for k, v in self.system.store._accounts.items()
                                           if v.is_admin}
        for group in list(self.system.groups.all()):
            self.system.groups.delete(group["id"])
        self._remove_walls()

    def test_everything_assigned_nothing_goes_either_way(self):
        for how in HOWS:
            with self.subTest(how=how):
                self.setUp_again()
                self.user(sources=[A, B, C], destinations=[D1, D2, D3, D4])
                r = self.go(how)
                self.assertEqual(len(self.macs()), 7)
                if how == "remove":
                    self.assertEqual(r.status_code, 409)

    def test_nothing_assigned_everything_goes_either_way(self):
        for how in HOWS:
            with self.subTest(how=how):
                self.app.seed()
                self.go(how)
                self.assertEqual(self.macs(), [])
                self.assertEqual(self.on_disk(), [])

    def test_an_offline_assigned_unit_is_kept_and_an_offline_unassigned_one_goes(self):
        self.user(sources=[A])
        offline = [u for u in self.units() if identity(u) in (A, B)]
        for unit in offline:
            self.mark_offline(unit)
        for how in HOWS:
            with self.subTest(how=how):
                self.app.seed()
                for unit in [u for u in self.units() if identity(u) in (A, B)]:
                    self.mark_offline(unit)
                self.go(how)
                self.assertIn(A, self.macs())
                self.assertNotIn(B, self.macs())

    def test_a_manipulated_request_cannot_widen_either_removal(self):
        self.user(sources=[A], destinations=[D1])
        everything = [u["ip"] for u in self.units()]
        for extra in ({"force": True}, {"scope": "all"}, {"remote_access": False}, {"all": True},
                      {"confirm": "yes"}, {"ips": everything}):
            with self.subTest(extra=extra):
                self.remote.post(self.admin, "/api/clear_units", extra)
                self.remote.post(self.admin, "/api/remove_units", dict({"ips": everything}, **extra))
                self.assertIn(A, self.macs())
                self.assertIn(D1, self.macs())

    def test_a_restricted_user_cannot_remove_anything(self):
        self.user(name="blake", sources=[A])
        client = self.remote.sign_in("blake", USER_PASSWORD)
        for path, body in (("/api/clear_units", {}), ("/api/remove_units", {"ips": ["198.18.0.1"]})):
            r = self.remote.post(client, path, body)
            self.assertIn(r.status_code, (401, 403), path)
        self.assertEqual(len(self.macs()), 7)

    def test_the_removals_never_touch_what_remote_access_holds(self):
        self.user(sources=[A], destinations=[D1])
        group = self.system.groups.create("Lobby", [D2])["group"]
        self.wall([DISPLAY_3], name="Stage Wall")
        before = (copy.deepcopy(self.perms()), copy.deepcopy(self.system.groups.get(group["id"])),
                  copy.deepcopy(self.m._load_video_wall_state()["walls"]),
                  sorted(a["username"] for a in self.store.list_accounts()))
        for how in HOWS:
            self.go(how)
        after = (self.perms(), self.system.groups.get(group["id"]),
                 self.m._load_video_wall_state()["walls"],
                 sorted(a["username"] for a in self.store.list_accounts()))
        self.assertEqual(before, after)


class RemoteOffScenarioTests(Inventory):
    def test_off_removes_assigned_units_with_what_pointed_at_them_both_ways(self):
        for how in HOWS:
            with self.subTest(how=how):
                self.app.seed()
                self.user(name="u-" + how, sources=[A], destinations=[D1])
                self.listener_off()
                self.go_local(how)
                self.assertNotIn(A, self.macs())
                self.assertEqual(self.perms("u-" + how)["sources"], [])
                self.assertEqual(self.perms("u-" + how)["destinations"], [])


class IdentityNotAddressTests(Inventory):
    def move(self, unit, new_ip):
        with self.m.cache_lock:
            row = self.m.cache_units.pop(unit["ip"])
            row = dict(row, ip=new_ip)
            self.m.cache_units[new_ip] = row
        return new_ip

    def test_a_unit_that_came_back_at_another_address_is_still_protected(self):
        self.user(sources=[A])
        source = next(u for u in self.units() if identity(u) == A)
        new_ip = self.move(source, "198.18.44.44")
        for how in HOWS:
            with self.subTest(how=how):
                r = self.go(how, ips=[new_ip])
                self.assertIn(A, self.macs())
                self.assertEqual(self.on_disk().count(A), 1)

    def test_the_old_address_names_nothing_once_the_unit_has_moved(self):
        self.user(sources=[A])
        source = next(u for u in self.units() if identity(u) == A)
        old_ip = source["ip"]
        self.move(source, "198.18.44.44")
        r = self.remote.post(self.admin, "/api/remove_units", {"ips": [old_ip]})
        # The gate answers first for an address the application no longer holds.
        self.assertIn(r.status_code, (400, 404))
        self.assertIn(A, self.macs())

    def test_an_assignment_to_an_identity_that_is_not_in_the_inventory_waits_for_the_unit(self):
        ghost = "02:00:5e:99:99:99"
        row = copy.deepcopy(next(u for u in self.units() if identity(u) == B))
        self.user(name="ghosted", sources=[ghost])
        self.go("clear")
        # Nothing was removed on its account, and nothing was invented for it either.
        self.assertEqual(self.perms("ghosted")["sources"], [ghost])
        # The unit turns up (a scan found it): it is protected from that moment.
        row.update({"ip": "198.18.77.77", "mac": ghost})
        with self.m.cache_lock:
            self.m.cache_units[row["ip"]] = row
        self.go("clear")
        self.assertIn(ghost, self.macs())

    def test_two_rows_with_one_identity_are_both_kept_when_it_is_assigned(self):
        self.user(sources=[A])
        twin = copy.deepcopy(next(u for u in self.units() if identity(u) == A))
        twin["ip"] = "198.18.55.55"
        with self.m.cache_lock:
            self.m.cache_units[twin["ip"]] = twin
        self.go("clear")
        self.assertEqual(self.macs().count(A), 2)


class GenerationTests(Inventory):
    """A removal that changed nothing must not discard a scan that is still running."""

    def test_a_clear_that_kept_everything_does_not_bump_the_generation_it_changed_nothing_for(self):
        self.user(sources=[A, B, C], destinations=[D1, D2, D3, D4])
        before = self.generation()
        self.go("clear")
        self.assertEqual(len(self.macs()), 7)
        # The generation exists so a stale scan cannot repopulate removed units. Nothing was
        # removed, so a scan in flight has nothing stale to drop.
        self.assertEqual(self.generation(), before)

    def test_remove_selected_that_kept_everything_does_not_bump_it_either(self):
        self.user(sources=[A])
        before = self.generation()
        self.go("remove", ips=[next(u["ip"] for u in self.units() if identity(u) == A)])
        self.assertEqual(self.generation(), before)

    def test_a_removal_that_removed_something_does_bump_it(self):
        before = self.generation()
        self.go("remove", ips=[next(u["ip"] for u in self.units() if identity(u) == B)])
        self.assertGreater(self.generation(), before)

    def test_a_disk_read_that_began_before_the_removal_does_not_bring_the_unit_back(self):
        victim = next(u for u in self.units() if identity(u) == B)
        stale = copy.deepcopy(victim)
        real_read = luma_store.read_json
        fired = []

        def read_then_remove(path, default=None):
            rows = real_read(path, default=default)
            if not fired and path == self.m.CACHE_FILE:
                fired.append(1)
                self.go("remove", ips=[victim["ip"]])
            return rows
        original = luma_store.read_json
        luma_store.read_json = read_then_remove
        self.addCleanup(setattr, luma_store, "read_json", original)
        self.m._merge_disk_into_memory()
        luma_store.read_json = original
        self.assertTrue(fired)
        self.assertNotIn(B, self.macs())


class ConcurrentEditTests(Inventory):
    """An assignment made while a removal is deciding lands first or waits; never both."""

    def pause_inside(self, how, edit):
        gate, release = threading.Event(), threading.Event()
        real = self.system.aliases.forget_many

        def slow(ids):
            gate.set()
            release.wait(5)
            return real(ids)
        self.system.aliases.forget_many = slow
        self.addCleanup(setattr, self.system.aliases, "forget_many", real)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self.go(how)))
        t.start()
        self.assertTrue(gate.wait(5), "the removal never reached its decision")
        side = threading.Thread(target=lambda: out.setdefault("edit", edit()))
        side.start()
        side.join(0.5)
        waited = side.is_alive()
        release.set()
        t.join(10)
        side.join(10)
        self.system.aliases.forget_many = real
        return out, waited

    def test_a_grant_made_during_a_clear_waits_for_it(self):
        out, waited = self.pause_inside("clear", lambda: self.user(name="late", sources=[B]))
        self.assertTrue(waited, "the grant must wait for the removal that is deciding")

    def test_a_group_change_during_a_clear_waits_for_it(self):
        out, waited = self.pause_inside("clear", lambda: self.system.groups.create("Late", [D4]))
        self.assertTrue(waited, "the group edit must wait for the removal that is deciding")

    def test_a_wall_saved_during_a_clear_either_lands_first_or_waits(self):
        out, waited = self.pause_inside("clear", lambda: self.wall([DISPLAY_4]))
        self.assertTrue(waited)

    def test_a_grant_made_during_remove_selected_waits_for_it(self):
        out, waited = self.pause_inside("remove", lambda: self.user(name="late", sources=[B]))
        self.assertTrue(waited)

    def test_a_group_change_during_remove_selected_waits_for_it(self):
        out, waited = self.pause_inside("remove", lambda: self.system.groups.create("Late", [D4]))
        self.assertTrue(waited)

    def test_a_clear_never_removes_a_unit_assigned_a_moment_before(self):
        # The order the defect needed: classified unassigned, assigned, removed anyway.
        self.user(name="early", sources=[B])
        self.go("clear")
        self.assertIn(B, self.macs())


class RestartTests(Inventory):
    def test_what_was_kept_is_still_kept_and_assigned_after_a_restart(self):
        self.user(sources=[A], destinations=[D1])
        self.system.groups.create("Lobby", [D2])
        self.wall([DISPLAY_3])
        self.go("clear")
        kept = self.macs()
        self.assertEqual(kept, sorted([A, D1, D2, D3]))
        self.reload_stores_from_disk()
        self.assertEqual(self.on_disk(), kept)
        rows = {d["id"]: d["assignments"]
                for d in self.admin.get("/api/admin/devices").get_json()["devices"]}
        self.assertEqual(sorted(i for i, a in rows.items() if a["count"]), sorted(kept))
        # And a second clear after the "restart" keeps exactly the same set.
        self.go("clear")
        self.assertEqual(self.macs(), kept)

    def test_remove_selected_after_a_restart_still_protects(self):
        self.user(sources=[A])
        self.reload_stores_from_disk()
        r = self.go("remove", ips=[next(u["ip"] for u in self.units() if identity(u) == A)])
        self.assertEqual(r.status_code, 409)
        self.assertIn(A, self.macs())

    def test_a_rescan_immediately_after_a_clear_brings_unassigned_units_back_normally(self):
        self.user(sources=[A])
        removed = [copy.deepcopy(u) for u in self.units() if identity(u) != A]
        self.go("clear")
        self.assertEqual(self.macs(), [A])
        with self.m.cache_lock:
            for unit in removed:
                self.m.cache_units[unit["ip"]] = unit
        self.assertEqual(len(self.macs()), 7)
        # The returned units are ordinary again, and the assigned one is still protected.
        self.go("clear")
        self.assertEqual(self.macs(), [A])


class SummaryFollowsEveryChangeTests(Inventory):
    def row(self, mac):
        rows = {d["id"]: d["assignments"]
                for d in self.admin.get("/api/admin/devices").get_json()["devices"]}
        return rows[mac]

    def test_the_summary_through_a_whole_lifecycle(self):
        self.assertEqual(self.row(A)["count"], 0)
        user = self.user(sources=[A])
        self.assertEqual(self.row(A)["users"], ["alex"])
        group = self.system.groups.create("Lobby", [A])["group"]
        self.assertEqual(self.row(A)["groups"], ["Lobby"])
        self.system.groups.delete(group["id"])
        self.assertEqual(self.row(A)["groups"], [])
        self.store.delete(user["id"])
        self.assertEqual(self.row(A)["count"], 0, "no ghost reference to a deleted account")
        self.wall([DISPLAY_3], source=SOURCE_A, name="Stage Wall")
        self.assertEqual(self.row(A)["wall_reasons"], ["Source of wall Stage Wall"])
        self._remove_walls()
        self.assertEqual(self.row(A)["count"], 0, "no ghost reference to a deleted wall")

    def test_after_a_clear_the_summary_describes_what_is_left_and_nothing_else(self):
        self.user(sources=[A])
        self.go("clear")
        rows = {d["id"]: d["assignments"]
                for d in self.admin.get("/api/admin/devices").get_json()["devices"]}
        self.assertEqual(list(rows), [A])
        self.assertEqual(rows[A]["users"], ["alex"])

    def test_a_disabled_account_still_counts_and_a_deleted_one_does_not(self):
        user = self.user(sources=[A])
        self.store.set_enabled(user["id"], False)
        self.assertEqual(self.row(A)["users"], ["alex"])
        self.store.delete(user["id"])
        self.assertEqual(self.row(A)["users"], [])


if __name__ == "__main__":
    unittest.main()
