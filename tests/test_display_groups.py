"""Display Groups: store, authorization, sequential routing and failure behaviour."""
import json
import os
import threading
import time
import unittest

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, System, identity)

import luma_groups
import luma_projection
import luma_store

A, B = identity(SOURCE_A), identity(SOURCE_B)
D1, D2, D3, D4 = (identity(u) for u in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4))
canon = luma_projection.canonical_identity


class GroupStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="lumasuite-groups-")
        self.path = os.path.join(self.dir, "display_groups.json")
        self.store = luma_groups.GroupStore(self.path, canon)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_group_has_an_opaque_id_independent_of_its_name_and_members(self):
        one = self.store.create("Lobby", [D1, D2])["group"]
        two = self.store.create("Lobby", [D1, D2])["group"]
        self.assertNotEqual(one["id"], two["id"])
        self.assertTrue(one["id"].startswith("grp_"))
        self.assertNotIn("lobby", one["id"].lower())
        renamed = self.store.update(one["id"], name="Foyer")["group"]
        self.assertEqual(renamed["id"], one["id"])
        moved = self.store.update(one["id"], members=[D3])["group"]
        self.assertEqual(moved["id"], one["id"])

    def test_membership_is_by_hardware_identity_in_every_spelling(self):
        group = self.store.create("Lobby", ["02-00-5E-20-00-01", "02005e200002", D1])["group"]
        self.assertEqual(group["members"], [D1, D2])          # normalised, de-duplicated, ordered

    def test_an_address_or_a_name_is_refused_as_a_member(self):
        for bad in ("198.51.100.21", "Lobby One", "", "02:00:5e:20:00", None, 5):
            result = self.store.create("Bad", [D1, bad])
            self.assertFalse(result["ok"], repr(bad))
        self.assertEqual(self.store.all(), [])

    def test_a_display_may_belong_to_several_groups(self):
        self.store.create("One", [D1])
        self.store.create("Two", [D1, D2])
        self.assertEqual(len(self.store.groups_containing(D1)), 2)
        self.assertEqual(self.store.referenced_identities()[D1], ["One", "Two"])

    def test_a_group_needs_a_name_and_names_are_cleaned(self):
        self.assertFalse(self.store.create("   ", [D1])["ok"])
        group = self.store.create("  Main \t  Lobby\n", [D1])["group"]
        self.assertEqual(group["name"], "Main Lobby")
        self.assertLessEqual(len(self.store.create("x" * 500, [D1])["group"]["name"]), luma_groups.MAX_NAME_LENGTH)

    def test_the_list_is_deterministic(self):
        for name in ("Zebra", "apple", "Mango"):
            self.store.create(name, [D1])
        self.assertEqual([g["name"] for g in self.store.all()], ["apple", "Mango", "Zebra"])

    def test_groups_persist_and_survive_a_reload(self):
        made = self.store.create("Lobby", [D1, D2])["group"]
        again = luma_groups.GroupStore(self.path, canon)
        self.assertEqual(again.get(made["id"])["members"], [D1, D2])

    def test_a_malformed_file_is_quarantined_and_reads_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        again = luma_groups.GroupStore(self.path, canon)
        self.assertEqual(again.all(), [])
        self.assertTrue(os.path.exists(self.path + ".corrupt"))
        self.assertTrue(again.create("Fresh", [D1])["ok"])     # and it keeps working

    def test_a_file_of_the_wrong_shape_does_not_crash_or_propagate(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"groups": "nope"}, handle)
        self.assertEqual(luma_groups.GroupStore(self.path, canon).all(), [])

    def test_a_damaged_record_is_skipped_and_the_rest_are_kept(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"groups": [{"id": "grp_ok", "name": "Fine", "members": [D1]},
                                  {"nope": 1}, "garbage"]}, handle)
        store = luma_groups.GroupStore(self.path, canon)
        self.assertEqual([g["id"] for g in store.all()], ["grp_ok"])

    def test_writes_leave_no_temporary_file_behind(self):
        self.store.create("Lobby", [D1])
        self.assertEqual(sorted(os.listdir(self.dir)), ["display_groups.json"])

    def test_concurrent_edits_do_not_lose_each_other(self):
        ids = [self.store.create("G%d" % i, [D1])["group"]["id"] for i in range(6)]
        errors = []

        def rename(i, gid):
            try:
                self.store.update(gid, name="Renamed %d" % i)
            except Exception as exc:                          # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=rename, args=(i, g)) for i, g in enumerate(ids)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        names = sorted(g["name"] for g in luma_groups.GroupStore(self.path, canon).all())
        self.assertEqual(names, sorted("Renamed %d" % i for i in range(6)))

    def test_deleting_a_missing_group_is_a_clean_miss(self):
        self.assertTrue(self.store.delete("grp_nope").get("missing"))

    def test_a_failed_write_leaves_memory_as_it_was(self):
        made = self.store.create("Lobby", [D1])["group"]
        original = luma_store.atomic_write_json
        luma_store.atomic_write_json = lambda *a, **k: False
        try:
            self.assertFalse(self.store.update(made["id"], name="Changed")["ok"])
            self.assertFalse(self.store.delete(made["id"])["ok"])
        finally:
            luma_store.atomic_write_json = original
        self.assertEqual(self.store.get(made["id"])["name"], "Lobby")


class SequentialRunTests(unittest.TestCase):
    def test_members_are_routed_strictly_one_after_another(self):
        order, active, peak = [], [0], [0]

        def route_one(index, identity_):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            order.append(("start", index))
            time.sleep(0.02)
            order.append(("end", index))
            active[0] -= 1
            return {"ok": True}

        summary = luma_groups.run_sequential([D1, D2, D3, D4], route_one)
        self.assertEqual(peak[0], 1)
        self.assertEqual(order, [(k, i) for i in range(4) for k in ("start", "end")])
        self.assertEqual((summary["routed"], summary["total"], summary["ok"]), (4, 4, True))

    def test_a_failed_member_does_not_stop_the_rest_and_nothing_is_undone(self):
        calls = []

        def route_one(index, identity_):
            calls.append(index)
            return {"ok": index != 1, "result": "unreachable"}

        summary = luma_groups.run_sequential([D1, D2, D3], route_one)
        self.assertEqual(calls, [0, 1, 2])                    # no rollback call, none skipped
        self.assertEqual((summary["routed"], summary["total"], summary["ok"]), (2, 3, False))
        self.assertEqual([r["ok"] for r in summary["results"]], [True, False, True])

    def test_an_exception_is_one_members_failure_only(self):
        def route_one(index, identity_):
            if index == 0:
                raise RuntimeError("boom")
            return {"ok": True}

        summary = luma_groups.run_sequential([D1, D2], route_one)
        self.assertEqual((summary["routed"], summary["total"]), (1, 2))
        self.assertEqual(summary["results"][0]["reason"], "error")

    def test_a_member_that_returns_nothing_is_a_failure(self):
        self.assertFalse(luma_groups.run_sequential([D1], lambda i, m: None)["ok"])

    def test_an_empty_group_is_not_a_success(self):
        summary = luma_groups.run_sequential([], lambda i, m: {"ok": True})
        self.assertEqual((summary["ok"], summary["total"]), (False, 0))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        self.groups = self.s.remote.groups
        self.group = self.groups.create("Lobby", [D1, D2, D3, D4])["group"]

    def user(self, name="alex", **grants):
        self.s.user(name, **grants)
        return self.s.sign_in(name, USER_PASSWORD)

    def route_group(self, client, source=A, group=None):
        return self.s.post(client, "/api/user/route_group",
                           {"source": source, "group": group or self.group["id"]})


class GroupRoutingTests(Fixture):
    def test_one_action_routes_every_member_and_reports_them_in_order(self):
        client = self.user(sources=[A], groups=[self.group["id"]])
        body = self.route_group(client).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual((body["routed"], body["total"]), (4, 4))
        self.assertEqual(sorted(c[0] for c in self.s.router.calls),
                         ["198.51.100.21", "198.51.100.22", "198.51.100.23", "198.51.100.24"])
        self.assertEqual([r["index"] for r in body["results"]], [0, 1, 2, 3], "results in membership order")
        self.assertLessEqual(self.s.router.max_active, luma_groups.DISPATCH_WORKERS)

    def test_a_group_grant_works_without_any_individual_display_grant(self):
        client = self.user(sources=[A], groups=[self.group["id"]])      # destinations: none
        self.assertEqual(self.route_group(client).status_code, 200)
        state = client.get("/api/user/state").get_json()
        self.assertEqual(state["destinations"], [])                    # not individually routable
        refused = self.s.post(client, "/api/user/route", {"source": A, "destination": D1})
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.get_json()["reason"], "destination_not_permitted")

    def test_an_individual_display_grant_does_not_grant_the_group(self):
        client = self.user(sources=[A], destinations=[D1, D2, D3, D4])
        response = self.route_group(client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "group_not_permitted")
        self.assertEqual(self.s.router.calls, [])

    def test_the_source_must_be_permitted_too(self):
        client = self.user(sources=[A], groups=[self.group["id"]])
        response = self.route_group(client, source=B)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "source_not_permitted")
        self.assertEqual(self.s.router.calls, [])

    def test_another_group_is_refused_and_a_missing_group_reads_the_same(self):
        other = self.groups.create("Other", [D1])["group"]
        client = self.user(sources=[A], groups=[self.group["id"]])
        real = self.route_group(client, group=other["id"])
        invented = self.route_group(client, group="grp_0000000000000000")
        self.assertEqual((real.status_code, invented.status_code), (403, 403))
        self.assertEqual(real.get_json(), invented.get_json())

    def test_a_partial_failure_reports_each_display_and_the_aggregate(self):
        self.s.router.fail["198.51.100.22"] = "unreachable"
        self.s.router.fail["198.51.100.24"] = "not_verified"
        client = self.user(sources=[A], groups=[self.group["id"]])
        body = self.route_group(client).get_json()
        self.assertFalse(body["ok"])
        self.assertEqual((body["routed"], body["total"]), (2, 4))
        self.assertEqual([r["ok"] for r in body["results"]], [True, False, True, False])
        self.assertEqual([r["result"] for r in body["results"]],
                         ["verified", "unreachable", "verified", "not_verified"])
        self.assertEqual(len(self.s.router.calls), 4)          # none skipped, none rolled back

    def test_the_reply_names_displays_but_never_identifies_them(self):
        client = self.user(sources=[A], groups=[self.group["id"]])
        text = self.route_group(client).get_data(as_text=True)
        for member in (D1, D2, D3, D4):
            self.assertNotIn(member, text)
        self.assertNotIn("198.51.100", text)
        self.assertIn("Lobby One", text)

    def test_membership_is_resolved_when_the_request_is_made(self):
        client = self.user(sources=[A], groups=[self.group["id"]])
        self.groups.update(self.group["id"], members=[D2])
        body = self.route_group(client).get_json()
        self.assertEqual((body["routed"], body["total"]), (1, 1))
        self.assertEqual(self.s.router.calls, [("198.51.100.22", "192.0.2.10")])

    def test_a_member_that_is_not_known_is_reported_and_the_rest_continue(self):
        self.groups.update(self.group["id"], members=[D1, "02:00:5e:99:99:99", D3])
        client = self.user(sources=[A], groups=[self.group["id"]])
        body = self.route_group(client).get_json()
        self.assertEqual([r["result"] for r in body["results"]], ["verified", "not_found", "verified"])
        self.assertEqual((body["routed"], body["total"]), (2, 3))

    def test_a_group_whose_members_are_all_missing_is_a_failure_not_a_success(self):
        self.groups.update(self.group["id"], members=["02:00:5e:99:99:98"])
        client = self.user(sources=[A], groups=[self.group["id"]])
        body = self.route_group(client).get_json()
        self.assertFalse(body["ok"])

    def test_each_member_takes_its_own_display_lock(self):
        client = self.user(sources=[A], groups=[self.group["id"]])
        held = []
        lock = self.s.remote.locks

        def watch(decoder_ip, encoder_ip):
            held.append(set(lock.busy()))

        self.s.router.on_call = watch
        self.route_group(client)
        self.assertEqual(len(held), 4)
        for seen, identity_ in zip(held, (D1, D2, D3, D4)):
            self.assertEqual(seen, {"decoder:" + identity_})

    def test_permission_withdrawn_midway_stops_the_remaining_members(self):
        client = self.user(sources=[A], groups=[self.group["id"]])
        user_id = next(a["id"] for a in self.s.store.list_accounts() if a["username"] == "alex")

        def revoke_after_first(decoder_ip, encoder_ip):
            if len(self.s.router.calls) == 1:
                self.s.store.set_enabled(user_id, False)

        self.s.router.on_call = revoke_after_first
        self.route_group(client)
        self.assertEqual(len(self.s.router.calls), 1)          # the account was disabled mid-group

    def test_the_audit_records_who_what_group_each_display_and_the_result(self):
        self.s.router.fail["198.51.100.23"] = "unreachable"
        client = self.user(sources=[A], groups=[self.group["id"]])
        self.route_group(client)
        entry = [e for e in self.s.store.audit_entries(limit=20) if e["event"] == "route_group"][0]
        detail = entry["detail"]
        self.assertEqual(entry["actor"], "alex")
        self.assertEqual((detail["source"], detail["group"]), (A, self.group["id"]))
        self.assertEqual((detail["routed"], detail["total"]), (3, 4))
        self.assertEqual([r["display"] for r in detail["results"]], [D1, D2, D3, D4])
        self.assertEqual([r["ok"] for r in detail["results"]], [True, True, False, True])
        self.assertNotIn("198.51.100", repr(entry))

    def test_group_status_follows_the_members(self):
        client = self.user(groups=[self.group["id"]])
        row = lambda: client.get("/api/user/state").get_json()["groups"][0]
        self.assertEqual(row()["showing"]["mode"], "none")
        for unit in self.s.units:
            if unit["type"] == "decoder":
                unit["dec"]["video"]["streamname"] = "stream-alpha"
        again = self.user("sam", sources=[A], groups=[self.group["id"]])
        self.assertEqual(again.get("/api/user/state").get_json()["groups"][0]["showing"]["source_name"],
                         "Alpha Camera")
        self.s.units[4]["dec"]["video"]["streamname"] = "stream-bravo"
        self.assertEqual(again.get("/api/user/state").get_json()["groups"][0]["showing"]["mode"], "mixed")
        self.s.units[4]["status"] = "failed"
        degraded = again.get("/api/user/state").get_json()["groups"][0]
        self.assertTrue(degraded["degraded"])
        self.assertEqual((degraded["members"], degraded["online_members"]), (4, 3))

    def test_a_group_with_no_readable_member_is_unavailable_not_unrouted(self):
        for unit in self.s.units:
            if unit["type"] == "decoder":
                unit["status"] = "failed"
        client = self.user(groups=[self.group["id"]])
        self.assertEqual(client.get("/api/user/state").get_json()["groups"][0]["showing"]["mode"],
                         "unavailable")

    def test_group_routing_does_not_use_video_wall_machinery(self):
        src = open(os.path.join(_support.ROOT, "luma_remote_api.py"), encoding="utf-8").read()
        self.assertNotIn("video_wall", src)
        self.assertNotIn("/api/video_wall", src)


class GroupAdministrationTests(Fixture):
    def admin(self):
        return self.s.sign_in("root", ADMIN_PASSWORD)

    def test_an_administrator_manages_groups_through_the_api(self):
        admin = self.admin()
        made = self.s.post(admin, "/api/admin/groups", {"name": "Foyer", "members": [D1, D2]}).get_json()
        gid = made["group"]["id"]
        self.assertEqual([m["name"] for m in made["group"]["members"]], ["Lobby One", "Lobby Two"])
        self.s.post(admin, "/api/admin/groups/" + gid, {"action": "rename", "name": "Atrium"})
        self.s.post(admin, "/api/admin/groups/" + gid, {"action": "set_members", "members": [D3]})
        listed = admin.get("/api/admin/groups").get_json()["groups"]
        row = [g for g in listed if g["id"] == gid][0]
        self.assertEqual((row["name"], [m["id"] for m in row["members"]]), ("Atrium", [D3]))

    def test_a_user_cannot_manage_groups(self):
        client = self.user(sources=[A])
        for call in (lambda: client.get("/api/admin/groups"),
                     lambda: self.s.post(client, "/api/admin/groups", {"name": "x", "members": []}),
                     lambda: self.s.post(client, "/api/admin/groups/" + self.group["id"], {"action": "delete"})):
            self.assertEqual(call().status_code, 403)
        self.assertIsNotNone(self.groups.get(self.group["id"]))

    def test_deleting_a_group_removes_it_from_every_grant_and_says_who_was_affected(self):
        client = self.user(sources=[A], groups=[self.group["id"]])
        admin = self.admin()
        body = self.s.post(admin, "/api/admin/groups/" + self.group["id"], {"action": "delete"}).get_json()
        self.assertEqual(body["accounts_affected"], ["alex"])
        account = next(a for a in self.s.store.list_accounts() if a["username"] == "alex")
        self.assertEqual(account["permissions"]["groups"], [])

    def test_a_group_id_is_never_reused_for_a_new_group(self):
        ids = {self.groups.create("G", [D1])["group"]["id"] for _ in range(20)}
        self.assertEqual(len(ids), 20)

    def test_an_admin_screen_member_that_is_gone_is_marked_not_found(self):
        self.groups.update(self.group["id"], members=[D1, "02:00:5e:99:99:97"])
        listed = self.admin().get("/api/admin/groups").get_json()["groups"][0]
        self.assertEqual([m["known"] for m in listed["members"]], [True, False])

    def test_group_changes_are_audited_in_words(self):
        admin = self.admin()
        self.s.post(admin, "/api/admin/groups", {"name": "Foyer", "members": [D1]})
        events = [e["event"] for e in self.s.store.audit_entries(limit=20)]
        self.assertIn("group.created", events)


if __name__ == "__main__":
    unittest.main()
