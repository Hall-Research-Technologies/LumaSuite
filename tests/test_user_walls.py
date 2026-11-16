"""Saved video walls as a User destination, offline units, and the manipulated drop.

A saved wall is the administrator's configuration. A User may be granted one as a routing
destination of its own: it never grants the decoders the wall is made of, it is shown by
name, it is read from the saved definition on every request, and routing to it goes
through the same validated wall operation the Video Wall page uses and is read back.

An offline unit is never written to, whoever asks and however the request is made; the
server decides that from the same derivation the pages use.
"""
import copy
import json
import threading
import time
import unittest
from unittest import mock

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, System, identity, saved_wall, wall_device_id,
                      RealApp, RealRemote)

import luma_gate
import luma_liveness
import luma_projection
import luma_users
import luma_walls

A, B = identity(SOURCE_A), identity(SOURCE_B)
D1, D2, D3, D4 = (identity(u) for u in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4))
IP = {D1: DISPLAY_1["ip"], D2: DISPLAY_2["ip"], D3: DISPLAY_3["ip"], D4: DISPLAY_4["ip"]}
ROUTE = "/api/user/route_multi"
WALL = "wall-aaaaaaaaaaaa"
WALL2 = "wall-bbbbbbbbbbbb"


def walls():
    return [saved_wall(WALL, "Atrium Wall", [DISPLAY_1, DISPLAY_2]),
            saved_wall(WALL2, "Gallery Wall", [DISPLAY_3, DISPLAY_4])]


def go_offline(system, unit_mac):
    """Two unanswered refreshes in a row, recorded the way the shared refresh records them."""
    for unit in system.units:
        if unit["mac"] == unit_mac:
            luma_liveness.record(unit, False)
            luma_liveness.record(unit, False)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.s = System(walls=walls())
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")

    def signed_in(self, name="alex", **grants):
        self.s.user(name, **grants)
        return self.s.sign_in(name, USER_PASSWORD)

    def multi(self, client, source, displays=(), groups=(), walls_=None, **extra):
        body = {"source": source, "displays": list(displays), "groups": list(groups)}
        if walls_ is not None:
            body["walls"] = list(walls_)
        body.update(extra)
        return self.s.post(client, ROUTE, body)

    def state(self, client):
        return client.get("/api/user/state").get_json()

    def rev(self, client):
        return client.get("/api/user/revision").get_json()["rev"]

    def grant(self, user_id, **grants):
        perms = luma_users.Permissions(grants.get("sources", ()), grants.get("destinations", ()),
                                       grants.get("groups", ()), grants.get("walls", ()))
        self.assertTrue(self.s.store.set_permissions(user_id, perms)["ok"])


class PermissionModelTests(unittest.TestCase):
    def test_walls_are_their_own_category_and_survive_a_round_trip(self):
        perms = luma_users.Permissions([A], [D1], ["grp_x"], [WALL.upper()])
        again = luma_users.Permissions.from_json(json.loads(json.dumps(perms.to_json())))
        self.assertEqual(again.to_json()["walls"], [WALL])
        self.assertTrue(again.may_control_wall(WALL))
        self.assertTrue(again.may_control_wall(" " + WALL.upper() + " "))
        self.assertFalse(again.may_control_destination(WALL))
        self.assertFalse(again.may_control_group(WALL))

    def test_an_account_stored_before_walls_existed_has_none(self):
        old = {"sources": [A], "destinations": [D1], "groups": []}
        self.assertEqual(luma_users.Permissions.from_json(old).walls, frozenset())

    def test_malformed_wall_lists_grant_nothing(self):
        for bad in ("wall-aaaaaaaaaaaa", 7, {"x": 1}, [1, None, {}], None):
            self.assertEqual(luma_users.Permissions.from_json({"walls": bad}).walls, frozenset(), bad)

    def test_holding_a_wall_grants_none_of_its_decoders(self):
        perms = luma_users.Permissions([A], [], [], [WALL])
        self.assertFalse(perms.may_control_destination(D1))
        self.assertFalse(perms.may_control_destination(D2))


class ProjectionTests(Fixture):
    def test_a_user_sees_only_the_walls_it_holds_by_name_and_never_the_decoders(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        body = self.state(client)
        self.assertEqual([w["name"] for w in body["walls"]], ["Atrium Wall"])
        self.assertEqual(body["walls"][0]["id"], WALL)
        self.assertEqual((body["walls"][0]["members"], body["walls"][0]["online_members"]), (2, 2))
        self.assertEqual(body["destinations"], [], "no member decoder appears as an individual display")
        text = json.dumps(body)
        for unit in (DISPLAY_1, DISPLAY_2):
            self.assertNotIn(unit["ip"], text)
            self.assertNotIn(unit["mac"], text)

    def test_wall_rows_have_exactly_the_published_fields(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        row = self.state(client)["walls"][0]
        self.assertEqual(set(row), luma_projection.WALL_FIELDS)
        self.assertEqual(set(self.state(client)), luma_projection.STATE_FIELDS)

    def test_an_administrator_sees_every_saved_wall(self):
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        self.assertEqual(len(self.state(admin)["walls"]), 2)

    def test_a_wall_whose_decoder_is_offline_says_so_by_count(self):
        go_offline(self.s, DISPLAY_2["mac"])
        client = self.signed_in(sources=[A], walls=[WALL])
        row = self.state(client)["walls"][0]
        self.assertEqual((row["members"], row["online_members"], row["degraded"]), (2, 1, True))

    def test_a_decoder_missing_from_the_inventory_counts_as_a_member_that_is_not_answering(self):
        self.s.units = [u for u in self.s.units if u["mac"] != DISPLAY_2["mac"]]
        client = self.signed_in(sources=[A], walls=[WALL])
        row = self.state(client)["walls"][0]
        self.assertEqual((row["members"], row["online_members"]), (2, 1))

    def test_the_admin_lists_saved_walls_for_the_editor_and_a_user_cannot(self):
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        listed = admin.get("/api/admin/walls").get_json()["walls"]
        self.assertEqual([w["name"] for w in listed], ["Atrium Wall", "Gallery Wall"])
        self.assertEqual(listed[0]["granted_to"], [])
        user = self.signed_in(sources=[A], walls=[WALL])
        self.assertEqual(user.get("/api/admin/walls").status_code, 403)
        self.assertEqual(admin.get("/api/admin/walls").get_json()["walls"][0]["granted_to"], ["alex"])

    def test_the_admin_api_stores_wall_grants_through_the_same_parser(self):
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        made = self.s.user("blair")
        response = self.s.post(admin, "/api/admin/users/" + made["id"], {
            "action": "set_permissions",
            "permissions": {"sources": [A], "walls": [WALL, "not-a-list-member", 4]}})
        self.assertEqual(response.status_code, 200, response.get_json())
        stored = self.s.store.get(made["id"]).permissions
        self.assertTrue(stored.may_control_wall(WALL))
        self.assertFalse(stored.may_control_destination(D1))


class RoutingTests(Fixture):
    def test_routing_to_a_granted_wall_uses_the_wall_operation_and_no_display_route(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        response = self.multi(client, A, walls_=[WALL])
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(self.s.wall_router.calls, [(WALL, A)])
        self.assertEqual(self.s.router.calls, [], "a wall is not routed by routing its decoders one by one")
        self.assertTrue(body["ok"])
        row = body["results"][0]
        self.assertEqual((row["kind"], row["name"], row["routed"], row["total"]), ("wall", "Atrium Wall", 2, 2))
        self.assertNotIn("display", row)
        self.assertEqual(body["summary"], "1 of 1 destination routed")

    def test_an_ungranted_wall_is_refused_whole_before_any_write(self):
        client = self.signed_in(sources=[A], destinations=[D1], walls=[WALL])
        response = self.multi(client, A, [D1], walls_=[WALL, WALL2])
        self.assertEqual((response.status_code, response.get_json()["reason"]), (403, "wall_not_permitted"))
        self.assertEqual((self.s.wall_router.calls, self.s.router.calls), ([], []))

    def test_granting_a_wall_does_not_let_the_account_route_its_decoders_or_a_group(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        for kwargs, reason in (({"displays": [D1]}, "destination_not_permitted"),
                               ({"groups": ["grp_anything"]}, "group_not_permitted")):
            response = self.multi(client, A, **kwargs)
            self.assertEqual((response.status_code, response.get_json()["reason"]), (403, reason))
        self.assertEqual((self.s.wall_router.calls, self.s.router.calls), ([], []))

    def test_a_display_or_group_grant_never_stands_in_for_a_wall(self):
        group = self.s.remote.groups.create("Everything", [D1, D2])["group"]
        client = self.signed_in(sources=[A], destinations=[D1, D2], groups=[group["id"]])
        response = self.multi(client, A, walls_=[WALL])
        self.assertEqual((response.status_code, response.get_json()["reason"]), (403, "wall_not_permitted"))

    def test_the_wrong_kind_of_id_in_a_list_is_refused_not_reinterpreted(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], destinations=[D1], groups=[group["id"]], walls=[WALL])
        # A group id offered as a wall, and a wall id offered as a group.
        r1 = self.multi(client, A, walls_=[group["id"]])
        r2 = self.multi(client, A, groups=[WALL])
        self.assertEqual((r1.status_code, r1.get_json()["reason"]), (403, "wall_not_permitted"))
        self.assertEqual((r2.status_code, r2.get_json()["reason"]), (403, "group_not_permitted"))
        # A wall id offered as a display is not a hardware identity at all.
        self.assertIn(self.multi(client, A, [WALL]).status_code, (400, 403))
        self.assertEqual((self.s.wall_router.calls, self.s.router.calls), ([], []))

    def test_the_gate_reads_the_wall_lists_it_is_given(self):
        for key in ("wall", "wall_id", "walls"):
            self.assertIn(key, luma_gate.WALL_KEYS)
        self.assertIn("wall_not_permitted", luma_gate.DENIAL_SENTENCES)
        account = self.s.store.get(self.s.user("quinn", sources=[A])["id"])
        decision = luma_gate._check_resources({"source": A, "walls": [WALL]}, account)
        self.assertFalse(decision)
        self.assertEqual(decision.reason, "wall_not_permitted")

    def test_a_missing_wall_is_not_found(self):
        client = self.signed_in(sources=[A], walls=["wall-cccccccccccc"])
        response = self.multi(client, A, walls_=["wall-cccccccccccc"])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.s.wall_router.calls, [])

    def test_malformed_wall_lists_are_a_400_not_a_500(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        for bad in ("wall-aaaaaaaaaaaa", {"a": 1}, [1], [None], [""], ["x"] * 17):
            response = self.s.post(client, ROUTE, {"source": A, "displays": [], "groups": [], "walls": bad})
            self.assertIn(response.status_code, (400, 403, 404), (bad, response.status_code))
            self.assertLess(response.status_code, 500)
        self.assertEqual(self.s.wall_router.calls, [])

    def test_a_wall_named_twice_is_routed_once(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        self.multi(client, A, walls_=[WALL, WALL.upper()])
        self.assertEqual(self.s.wall_router.calls, [(WALL, A)])

    def test_a_partial_wall_is_reported_as_partial_and_the_whole_request_is_not_ok(self):
        self.s.wall_router.result = {"ok": False, "status": "partial", "routed": 1, "total": 2}
        client = self.signed_in(sources=[A], walls=[WALL])
        body = self.multi(client, A, walls_=[WALL]).get_json()
        self.assertFalse(body["ok"])
        self.assertEqual((body["routed"], body["total"]), (0, 1))
        row = body["results"][0]
        self.assertEqual((row["result"], row["routed"], row["total"]), ("partial", 1, 2))

    def test_walls_and_displays_route_together_in_the_order_given(self):
        client = self.signed_in(sources=[A], destinations=[D3], walls=[WALL])
        body = self.multi(client, A, [D3], walls_=[WALL]).get_json()
        self.assertEqual([r["name"] for r in body["results"]], ["Lobby Three", "Atrium Wall"])
        self.assertEqual(body["summary"], "2 of 2 destinations routed")
        self.assertEqual(self.s.router.calls[0][0], IP[D3])
        self.assertEqual(self.s.wall_router.calls, [(WALL, A)])

    def test_an_ungranted_source_is_refused_before_a_wall_is_touched(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        response = self.multi(client, B, walls_=[WALL])
        self.assertEqual((response.status_code, response.get_json()["reason"]), (403, "source_not_permitted"))
        self.assertEqual(self.s.wall_router.calls, [])

    def test_a_permission_withdrawn_while_a_wall_waits_refuses_the_next_one(self):
        client = self.signed_in(sources=[A], walls=[WALL, WALL2])
        user_id = self.s.store.find_by_username("alex").id

        def withdraw(_wall, _source):
            self.grant(user_id, sources=[A], walls=[WALL])          # WALL2 taken away mid-request

        self.s.wall_router.on_call = withdraw
        body = self.multi(client, A, walls_=[WALL, WALL2]).get_json()
        self.assertEqual([r["result"] for r in body["results"]], ["verified", "refused"])
        self.assertEqual(self.s.wall_router.calls, [(WALL, A)], "the second wall was never routed")

    def test_the_answer_names_no_address_and_no_decoder(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        text = json.dumps(self.multi(client, A, walls_=[WALL]).get_json())
        for unit in (DISPLAY_1, DISPLAY_2, SOURCE_A):
            self.assertNotIn(unit["ip"], text)
            self.assertNotIn(unit["mac"], text)

    def test_an_administrator_may_route_any_saved_wall(self):
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        response = self.multi(admin, B, walls_=[WALL2])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.s.wall_router.calls, [(WALL2, B)])


class LifecycleTests(Fixture):
    """Grant, route, take away, give back, rename, change, delete -- never a stale wall."""

    def test_the_whole_life_of_a_wall_grant(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        user_id = self.s.store.find_by_username("alex").id
        self.assertEqual(self.multi(client, A, walls_=[WALL]).status_code, 200)

        # Removed: the open session loses it, and the server refuses the old request.
        before = self.rev(client)
        self.grant(user_id, sources=[A])
        self.assertEqual(self.state(client)["walls"], [])
        self.assertNotEqual(self.rev(client), before)
        calls = len(self.s.wall_router.calls)
        refused = self.multi(client, A, walls_=[WALL])
        self.assertEqual((refused.status_code, refused.get_json()["reason"]), (403, "wall_not_permitted"))
        self.assertEqual(len(self.s.wall_router.calls), calls)

        # Given again: it is back, without signing in.
        self.grant(user_id, sources=[A], walls=[WALL])
        self.assertEqual([w["id"] for w in self.state(client)["walls"]], [WALL])
        self.assertEqual(self.multi(client, A, walls_=[WALL]).status_code, 200)

    def test_a_rename_reaches_the_user_and_changes_the_revision_but_not_the_permission(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        rev = self.rev(client)
        self.s.wall_defs[0]["name"] = "Renamed Atrium"
        self.assertEqual(self.state(client)["walls"][0]["name"], "Renamed Atrium")
        self.assertNotEqual(self.rev(client), rev)
        self.assertEqual(self.multi(client, A, walls_=[WALL]).status_code, 200,
                         "a name is presentation; the grant follows the id")

    def test_a_membership_change_is_what_the_user_sees_and_routes_to_next(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        rev = self.rev(client)
        self.s.wall_defs[0] = saved_wall(WALL, "Atrium Wall", [DISPLAY_1, DISPLAY_2, DISPLAY_3])
        self.assertEqual(self.state(client)["walls"][0]["members"], 3)
        self.assertNotEqual(self.rev(client), rev)
        body = self.multi(client, A, walls_=[WALL]).get_json()
        self.assertEqual(body["results"][0]["total"], 3, "the router was handed the wall as saved now")

    def test_swapping_a_member_changes_the_revision_even_when_the_count_does_not(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        rev = self.rev(client)
        self.s.wall_defs[0] = saved_wall(WALL, "Atrium Wall", [DISPLAY_1, DISPLAY_4])
        self.assertNotEqual(self.rev(client), rev)

    def test_swapping_two_members_cells_changes_the_revision_though_the_set_is_the_same(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        rev = self.rev(client)
        self.s.wall_defs[0] = saved_wall(WALL, "Atrium Wall", [DISPLAY_2, DISPLAY_1])
        self.assertNotEqual(self.rev(client), rev, "which display sits in which cell is part of the wall")

    def test_a_member_replaced_by_one_the_inventory_does_not_hold_changes_the_revision(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        rev = self.rev(client)
        ghost = dict(DISPLAY_2, mac="02:00:5e:20:00:99", ip="198.51.100.99")
        self.s.wall_defs[0] = saved_wall(WALL, "Atrium Wall", [DISPLAY_1, ghost])
        self.assertNotEqual(self.rev(client), rev)

    def test_a_deleted_wall_vanishes_at_once_and_a_route_to_it_is_not_found(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        self.s.wall_defs[:] = [w for w in self.s.wall_defs if w["id"] != WALL]
        self.assertEqual(self.state(client)["walls"], [])
        calls = len(self.s.wall_router.calls)
        self.assertEqual(self.multi(client, A, walls_=[WALL]).status_code, 404)
        self.assertEqual(len(self.s.wall_router.calls), calls)

    def test_a_stale_grant_for_a_deleted_wall_is_dropped_by_the_store(self):
        self.s.user("alex", sources=[A], walls=[WALL, WALL2])
        changed = self.s.store.forget_walls({WALL2})
        self.assertEqual(changed, 1)
        perms = self.s.store.find_by_username("alex").permissions
        self.assertEqual(perms.walls, frozenset({WALL2}))
        self.assertEqual(self.s.store.forget_walls({WALL2}), 0, "a second call changes nothing")

    def test_a_wall_grant_does_not_survive_a_cleared_inventory_as_a_decoder_grant(self):
        self.s.user("alex", sources=[A], destinations=[D1], walls=[WALL])
        result = self.s.store.forget_devices({D1, A})
        self.assertTrue(result["ok"])
        perms = self.s.store.find_by_username("alex").permissions
        self.assertEqual((perms.sources, perms.destinations), (frozenset(), frozenset()))
        self.assertEqual(perms.walls, frozenset({WALL}),
                         "the wall is saved configuration, not inventory: the grant stays and the wall reports offline")

    def test_a_wall_grant_persists_and_comes_back_after_a_restart(self):
        self.s.user("alex", sources=[A], walls=[WALL])
        reloaded = luma_users.UserStore(self.s.remote.users_file, self.s.remote.audit_file)
        self.assertTrue(reloaded.find_by_username("alex").permissions.may_control_wall(WALL))


class OfflineTests(Fixture):
    def setUp(self):
        super().setUp()
        self.group = self.s.remote.groups.create("Pair", [D1, D2])["group"]

    def test_the_two_missed_refresh_rule(self):
        unit = {"mac": "02:00:5e:20:00:09", "status": "idle"}
        self.assertFalse(luma_liveness.is_offline(unit), "never polled: not called offline")
        luma_liveness.record(unit, False)
        self.assertFalse(luma_liveness.is_offline(unit), "one missed read is a dropped frame")
        self.assertTrue(luma_projection.is_online(unit))
        luma_liveness.record(unit, False)
        self.assertTrue(luma_liveness.is_offline(unit))
        self.assertFalse(luma_projection.is_online(unit))
        luma_liveness.record(unit, True)
        self.assertFalse(luma_liveness.is_offline(unit), "any answer clears it at once")
        self.assertTrue(luma_projection.is_online(unit))

    def test_a_malformed_record_is_never_offline(self):
        for bad in (None, "x", 7, {"misses": "many"}, {"misses": None}):
            self.assertFalse(luma_liveness.is_offline({"reach": bad}), bad)

    def test_an_offline_source_refuses_every_route_with_a_409_and_no_write(self):
        go_offline(self.s, SOURCE_A["mac"])
        client = self.signed_in(sources=[A], destinations=[D1], groups=[self.group["id"]], walls=[WALL])
        for response in (self.multi(client, A, [D1]),
                         self.multi(client, A, [], [self.group["id"]]),
                         self.multi(client, A, walls_=[WALL]),
                         self.s.post(client, "/api/user/route", {"source": A, "destination": D1}),
                         self.s.post(client, "/api/user/route_group", {"source": A, "group": self.group["id"]})):
            body = response.get_json()
            self.assertEqual(response.status_code, 409, body)
            self.assertEqual(body["reason"], "device_offline")
            self.assertNotIn(SOURCE_A["ip"], json.dumps(body))
        self.assertEqual((self.s.router.calls, self.s.wall_router.calls), ([], []))

    def test_an_offline_display_is_never_written_to_and_is_reported_as_offline(self):
        go_offline(self.s, DISPLAY_2["mac"])
        client = self.signed_in(sources=[A], destinations=[D1, D2])
        body = self.multi(client, A, [D1, D2]).get_json()
        self.assertEqual([r["result"] for r in body["results"]], ["verified", "offline"])
        self.assertFalse(body["ok"])
        self.assertEqual((body["routed"], body["total"]), (1, 2))
        self.assertEqual([d for d, _ in self.s.router.calls], [IP[D1]])

    def test_a_single_route_to_an_offline_display_is_a_409(self):
        go_offline(self.s, DISPLAY_1["mac"])
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.s.post(client, "/api/user/route", {"source": A, "destination": D1})
        self.assertEqual((response.status_code, response.get_json()["reason"]), (409, "device_offline"))
        self.assertEqual(self.s.router.calls, [])

    def test_a_group_with_an_offline_member_routes_the_rest_and_reports_the_member(self):
        go_offline(self.s, DISPLAY_1["mac"])
        client = self.signed_in(sources=[A], groups=[self.group["id"]])
        body = self.s.post(client, "/api/user/route_group", {"source": A, "group": self.group["id"]}).get_json()
        self.assertEqual([r["result"] for r in body["results"]], ["offline", "verified"])
        self.assertEqual((body["routed"], body["total"], body["ok"]), (1, 2, False))
        self.assertEqual([d for d, _ in self.s.router.calls], [IP[D2]])

    def test_the_state_and_the_gate_agree_about_who_is_offline(self):
        go_offline(self.s, DISPLAY_1["mac"])
        client = self.signed_in(sources=[A], destinations=[D1, D2])
        online = {d["id"]: d["online"] for d in self.state(client)["destinations"]}
        self.assertEqual(online, {D1: False, D2: True})
        self.assertEqual(self.multi(client, A, [D1]).get_json()["results"][0]["result"], "offline")
        self.assertEqual(self.multi(client, A, [D2]).get_json()["results"][0]["result"], "verified")

    def test_a_unit_that_answers_again_is_routable_again_without_a_rescan(self):
        go_offline(self.s, DISPLAY_1["mac"])
        client = self.signed_in(sources=[A], destinations=[D1])
        self.assertEqual(self.multi(client, A, [D1]).get_json()["results"][0]["result"], "offline")
        for unit in self.s.units:
            if unit["mac"] == DISPLAY_1["mac"]:
                luma_liveness.record(unit, True)
        self.assertEqual(self.multi(client, A, [D1]).get_json()["results"][0]["result"], "verified")

    def test_an_unknown_source_is_not_called_offline(self):
        client = self.signed_in(sources=["02:00:5e:77:77:77"], destinations=[D1])
        response = self.multi(client, "02:00:5e:77:77:77", [D1])
        self.assertNotEqual(response.get_json().get("reason"), "device_offline")


class TheRealRoutesTests(unittest.TestCase):
    """The administrator's matrix, the wall operation and the poll, on the real application."""

    def setUp(self):
        self.app = RealApp(self)
        self.app.seed()
        self.m = self.app.m
        self.client = self.m.APP.test_client()          # the local operator
        self._saved = {}
        self.app.patch("_load_video_wall_state", lambda: copy.deepcopy(self._saved) if self._saved
                       else {"walls": [], "ignored_candidates": []})
        self.app.patch("_save_video_wall_state", lambda data: self._saved.update(copy.deepcopy(data)))

    def _offline(self, ip):
        with self.m.cache_lock:
            unit = self.m.cache_units[ip]
            luma_liveness.record(unit, False)
            luma_liveness.record(unit, False)

    def test_the_shared_refresh_records_whether_each_unit_answered_and_nothing_more(self):
        self.app.devices.unreachable.add(DISPLAY_2["ip"])
        self.m._refresh_matrix_state_core()
        self.assertTrue(luma_projection.is_online(self.app.unit(DISPLAY_2["ip"])), "one miss is not offline")
        self.m._refresh_matrix_state_core()
        self.assertFalse(luma_projection.is_online(self.app.unit(DISPLAY_2["ip"])))
        self.assertTrue(luma_projection.is_online(self.app.unit(DISPLAY_1["ip"])))
        self.assertTrue(luma_projection.is_online(self.app.unit(SOURCE_A["ip"])))
        self.app.devices.unreachable.clear()
        self.m._refresh_matrix_state_core()
        self.assertTrue(luma_projection.is_online(self.app.unit(DISPLAY_2["ip"])), "answered again: back at once")
        self.assertEqual(self.app.devices.writes(), [], "reading liveness writes nothing")

    def test_the_administrators_route_refuses_an_offline_decoder_or_encoder(self):
        self._offline(DISPLAY_1["ip"])
        response = self.client.post("/api/route_matrix", json={"decoder_ip": DISPLAY_1["ip"], "encoder_ip": SOURCE_A["ip"]})
        self.assertEqual((response.status_code, response.get_json()["reason"]), (409, "device_offline"))
        self._offline(SOURCE_B["ip"])
        response = self.client.post("/api/route_matrix", json={"decoder_ip": DISPLAY_2["ip"], "encoder_ip": SOURCE_B["ip"]})
        self.assertEqual(response.status_code, 409)
        response = self.client.post("/api/route_matrix", json={"decoder_ip": DISPLAY_2["ip"], "encoder_ip": SOURCE_B["ip"],
                                                               "mode": "video"})
        self.assertEqual(response.status_code, 409, "the breakaway path is gated too")
        self.assertEqual(self.app.devices.writes(), [])

    def test_the_administrators_route_still_works_for_online_units(self):
        response = self.client.post("/api/route_matrix", json={"decoder_ip": DISPLAY_1["ip"], "encoder_ip": SOURCE_A["ip"]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.app.devices.decoder_stream[DISPLAY_1["ip"]], "stream-alpha")

    def test_the_administrators_gates_refuse_only_a_determined_offline_unit(self):
        # No recorded status at all (an old cache): not called offline, so not refused.
        with self.m.cache_lock:
            del self.m.cache_units[DISPLAY_1["ip"]]["status"]
        response = self.client.post("/api/route_matrix", json={"decoder_ip": DISPLAY_1["ip"], "encoder_ip": SOURCE_A["ip"]})
        self.assertEqual(response.status_code, 200, response.get_json())
        # A status that says the unit is not idle is a determined answer.
        with self.m.cache_lock:
            self.m.cache_units[DISPLAY_1["ip"]]["status"] = "offline"
        refused = self.client.post("/api/route_matrix", json={"decoder_ip": DISPLAY_1["ip"], "encoder_ip": SOURCE_A["ip"]})
        self.assertEqual((refused.status_code, refused.get_json()["reason"]), (409, "device_offline"))

    def test_the_served_units_carry_the_one_online_derivation(self):
        self._offline(DISPLAY_3["ip"])
        units = {u["ip"]: u["online"] for u in self.client.get("/api/cache").get_json()["units"]}
        self.assertEqual(units[DISPLAY_3["ip"]], False)
        self.assertEqual(units[DISPLAY_1["ip"]], True)

    def test_the_video_wall_page_never_writes_to_an_offline_display(self):
        self._offline(DISPLAY_2["ip"])
        wall = saved_wall(WALL, "Atrium Wall", [DISPLAY_1, DISPLAY_2], source_id=wall_device_id(SOURCE_A))
        response = self.client.post("/api/video_wall/source", json={"wall": wall, "scope": "wall"})
        results = response.get_json()["results"]
        self.assertTrue(results[wall_device_id(DISPLAY_1)]["ok"])
        self.assertFalse(results[wall_device_id(DISPLAY_2)]["ok"])
        self.assertTrue(results[wall_device_id(DISPLAY_2)]["offline"])
        self.assertFalse([w for w in self.app.devices.writes() if w[0] == DISPLAY_2["ip"]])
        wall["cells"][1]["rotation"] = 90                  # a write apply would make if it got that far
        applied = self.client.post("/api/video_wall/apply", json={"wall": wall}).get_json()
        self.assertTrue([w for w in self.app.devices.writes() if w[0] == DISPLAY_1["ip"] and w[1].startswith("SelectVideo")],
                        "precondition: apply does write to the display that is answering")
        self.assertFalse([w for w in self.app.devices.writes() if w[0] == DISPLAY_2["ip"]],
                         "apply (bezel, rotation) does not reach an offline display either")
        self.assertFalse(applied["ok"])

    # -- the wall operation itself -----------------------------------------
    def _wall(self, members=(DISPLAY_1, DISPLAY_2), **extra):
        wall = saved_wall(WALL, "Atrium Wall", list(members))
        wall.update(extra)
        self._saved.update({"walls": [wall], "ignored_candidates": []})
        return wall

    def test_routing_a_wall_sends_the_source_to_every_decoder_and_reads_each_back(self):
        wall = self._wall()
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertEqual((outcome["ok"], outcome["status"], outcome["routed"], outcome["total"]),
                         (True, "verified", 2, 2))
        for display in (DISPLAY_1, DISPLAY_2):
            self.assertEqual(self.app.devices.decoder_stream[display["ip"]], "stream-alpha")
            self.assertEqual(self.app.devices.wall_info[display["ip"]]["vmenable"], True)
        self.assertEqual(self.app.devices.wall_info[DISPLAY_1["ip"]]["vwmatrix"], "1_2_1_1")
        self.assertEqual(self.app.devices.wall_info[DISPLAY_2["ip"]]["vwmatrix"], "1_2_1_2")
        reads = self.app.devices.methods(DISPLAY_1["ip"])
        self.assertGreater(len(reads), 2, "the decoder was read after the write")

    def test_the_saved_wall_agrees_with_the_decoders_once_the_route_is_verified(self):
        wall = self._wall()
        self.m._route_wall_for_users(wall, A)
        saved = self._saved["walls"][0]
        self.assertEqual(saved["source_id"], wall_device_id(SOURCE_A))
        self.assertEqual(saved["name"], "Atrium Wall")
        self.assertEqual(len(saved["cells"]), 2, "the wall's grid and members are not changed")

    def test_a_decoder_that_accepts_the_write_and_does_not_hold_it_is_not_a_success(self):
        wall = self._wall()
        self.app.devices.ignores_set.add(DISPLAY_2["ip"])
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertEqual((outcome["ok"], outcome["status"], outcome["routed"], outcome["total"]),
                         (False, "partial", 1, 2))
        self.assertNotIn("source_id", self._saved["walls"][0] if self._saved["walls"][0].get("source_id") else {},
                         "an unverified route does not rewrite the saved wall")
        self.assertEqual(self._saved["walls"][0].get("source_id", ""), "")

    def test_a_wall_matrix_the_decoder_does_not_hold_is_not_a_success_either(self):
        wall = self._wall()
        self.app.devices.wall_ignores.add(DISPLAY_1["ip"])
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertFalse(outcome["ok"])
        self.assertEqual((outcome["routed"], outcome["total"]), (1, 2))

    def test_an_offline_decoder_is_skipped_and_the_wall_says_partial(self):
        wall = self._wall()
        self._offline(DISPLAY_2["ip"])
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertEqual((outcome["ok"], outcome["status"], outcome["routed"], outcome["total"]),
                         (False, "partial", 1, 2))
        self.assertFalse([w for w in self.app.devices.writes() if w[0] == DISPLAY_2["ip"]])

    def test_a_decoder_that_is_no_longer_in_the_inventory_is_never_guessed_at(self):
        wall = self._wall()
        with self.m.cache_lock:
            del self.m.cache_units[DISPLAY_2["ip"]]
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertEqual((outcome["ok"], outcome["status"], outcome["routed"], outcome["total"]),
                         (False, "partial", 1, 2))

    def test_when_every_decoder_is_unavailable_nothing_is_written_and_the_status_says_why(self):
        wall = self._wall()
        self._offline(DISPLAY_1["ip"])
        self._offline(DISPLAY_2["ip"])
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertEqual((outcome["ok"], outcome["status"], outcome["routed"]), (False, "offline", 0))
        self.assertEqual(self.app.devices.writes(), [])

    def test_an_offline_or_unknown_source_writes_nothing(self):
        wall = self._wall()
        self._offline(SOURCE_A["ip"])
        self.assertEqual(self.m._route_wall_for_users(wall, A)["status"], "offline")
        self.assertEqual(self.m._route_wall_for_users(wall, "02:00:5e:77:77:77")["status"], "not_found")
        self.assertEqual(self.m._route_wall_for_users(wall, D1)["status"], "not_found",
                         "a display is not a source")
        self.assertEqual(self.app.devices.writes(), [])

    def test_an_unreachable_source_encoder_writes_nothing(self):
        wall = self._wall()
        self.app.devices.encoder_stream.pop(SOURCE_A["ip"])
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertEqual((outcome["ok"], outcome["status"]), (False, "unreachable"))
        self.assertEqual(self.app.devices.writes(), [])

    def test_a_source_that_does_not_answer_is_never_routed_from_the_stream_the_cache_remembers(self):
        wall = self._wall()
        cached = self.app.unit(SOURCE_A["ip"])["enc"]["video"]["streamname"]
        self.assertEqual(cached, "stream-alpha", "precondition: the cache does remember a stream")
        self.app.devices.unreachable.add(SOURCE_A["ip"])
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertEqual((outcome["ok"], outcome["status"], outcome["routed"]), (False, "unreachable", 0))
        self.assertEqual(self.app.devices.writes(), [], "no decoder was told to show a stream nobody confirmed")
        for display in (DISPLAY_1, DISPLAY_2):
            self.assertEqual(self.app.devices.decoder_stream[display["ip"]], "")

    def test_a_cell_with_its_own_source_is_left_alone_and_counted(self):
        wall = self._wall()
        wall["cells"][1]["source_mode"] = "override"
        wall["cells"][1]["override_source_id"] = wall_device_id(SOURCE_B)
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertEqual((outcome["total"], outcome["routed"], outcome["other_sources"]), (1, 1, 1))
        self.assertFalse([w for w in self.app.devices.writes() if w[0] == DISPLAY_2["ip"]])

    def test_a_wall_with_no_followed_cells_is_a_failure_not_a_success(self):
        wall = self._wall(members=())
        outcome = self.m._route_wall_for_users(wall, A)
        self.assertEqual((outcome["ok"], outcome["total"]), (False, 0))

    # -- the saved set and the grants -----------------------------------------
    def test_deleting_a_saved_wall_takes_every_grant_to_it_away(self):
        self._wall()
        remote = RealRemote(self, self.app)
        store = remote.store
        store.create("zed", USER_PASSWORD, luma_users.ROLE_USER,
                     luma_users.Permissions([A], [], [], [WALL]))
        response = self.client.post("/api/video_wall/delete", json={"id": WALL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(store.find_by_username("zed").permissions.walls, frozenset())

    def test_importing_a_set_without_a_wall_takes_its_grants_away_and_keeps_the_others(self):
        self._wall()
        remote = RealRemote(self, self.app)
        store = remote.store
        store.create("zed", USER_PASSWORD, luma_users.ROLE_USER,
                     luma_users.Permissions([A], [], [], [WALL, WALL2]))
        keep = saved_wall(WALL2, "Gallery Wall", [DISPLAY_3, DISPLAY_4])
        response = self.client.post("/api/video_wall/import",
                                    json={"kind": "lumasuite.video_walls", "version": 1, "walls": [keep]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(store.find_by_username("zed").permissions.walls, frozenset({WALL2}))


class SecondLineTests(Fixture):
    """The handler's own authorisation, reached by taking the gate's out of the way.

    The gate refuses first, so a handler that forgot to check would pass every request test.
    Opening the gate for the span of one test is what proves the second line exists.
    """

    def open_gate(self):
        allow = lambda payload, account: luma_gate.Decision(True, 200, "permitted", account)
        patcher = mock.patch.object(luma_gate, "_check_resources", allow)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_precondition_the_gate_really_is_open(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        self.open_gate()
        self.assertEqual(self.multi(client, A, walls_=[WALL]).status_code, 200,
                         "precondition: a granted request still works with the gate's step removed")
        self.assertEqual(luma_gate._check_resources({"walls": ["x"]}, None).allowed, True)

    def test_the_handler_refuses_an_ungranted_wall_display_group_and_source_on_its_own(self):
        client = self.signed_in(sources=[A], destinations=[D1], walls=[WALL])
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        self.open_gate()
        for body, reason in (({"source": A, "walls": [WALL2]}, "wall_not_permitted"),
                             ({"source": A, "displays": [D2]}, "destination_not_permitted"),
                             ({"source": A, "groups": [group["id"]]}, "group_not_permitted"),
                             ({"source": B, "displays": [D1]}, "source_not_permitted")):
            response = self.s.post(client, ROUTE, body)
            self.assertEqual((response.status_code, response.get_json().get("reason")), (403, reason), body)
        self.assertEqual((self.s.router.calls, self.s.wall_router.calls), ([], []))

    def test_a_grant_withdrawn_while_a_wall_waits_for_its_decoders_is_noticed_on_the_far_side(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        user_id = self.s.store.find_by_username("alex").id
        locks = self.s.remote.locks
        answer = {}

        def request():
            answer["response"] = self.multi(client, A, walls_=[WALL])

        with locks.hold(locks.decoder(D1)):
            worker = threading.Thread(target=request)
            waited = locks.stats()["waited"]
            worker.start()
            deadline = time.monotonic() + 5
            while locks.stats()["waited"] == waited and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(locks.stats()["waited"], waited,
                               "precondition: the request is queued behind the lock")
            self.grant(user_id, sources=[A])            # taken away while it waits
        worker.join(10)
        body = answer["response"].get_json()
        self.assertEqual(body["results"][0]["result"], "refused")
        self.assertEqual(self.s.wall_router.calls, [], "nothing was written after the grant went")


class DropSecurityTests(Fixture):
    """A drop is the Route button by another gesture: nothing it carries is trusted."""

    def test_a_crafted_request_gets_the_answer_the_button_would(self):
        client = self.signed_in(sources=[A], destinations=[D1], walls=[WALL])
        crafted = (
            ({"source": B, "displays": [D1]}, "source_not_permitted"),
            ({"source": A, "displays": [D2]}, "destination_not_permitted"),
            ({"source": A, "walls": [WALL2]}, "wall_not_permitted"),
            ({"source": A, "walls": [WALL], "displays": [D1, D3]}, "destination_not_permitted"),
            ({"source": A, "groups": ["grp_nonexistent"]}, "group_not_permitted"),
        )
        for body, reason in crafted:
            response = self.s.post(client, ROUTE, body)
            self.assertEqual((response.status_code, response.get_json()["reason"]), (403, reason), body)
        self.assertEqual((self.s.router.calls, self.s.wall_router.calls), ([], []))

    def test_there_is_no_client_supplied_revision_or_permission_field_that_is_believed(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.s.post(client, ROUTE, {
            "source": A, "displays": [D2], "rev": self.rev(client), "permissions": {"destinations": [D2]},
            "role": "admin", "walls_granted": [WALL]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.s.router.calls, [])

    def test_a_removed_grant_binds_the_very_next_request_on_the_open_session(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        user_id = self.s.store.find_by_username("alex").id
        self.assertEqual(self.multi(client, A, [D1]).status_code, 200)
        self.grant(user_id, sources=[A])
        self.assertEqual(self.multi(client, A, [D1]).status_code, 403)
        self.assertEqual(len(self.s.router.calls), 1)

    def test_a_disabled_account_and_an_unauthenticated_caller_route_nothing(self):
        client = self.signed_in(sources=[A], walls=[WALL])
        user_id = self.s.store.find_by_username("alex").id
        self.s.store.set_enabled(user_id, False)
        self.assertIn(self.multi(client, A, walls_=[WALL]).status_code, (401, 403))
        anonymous = self.s.client()
        self.assertEqual(anonymous.post(ROUTE, json={"source": A, "walls": [WALL]}).status_code, 401)
        self.assertEqual(self.s.wall_router.calls, [])

    def test_a_direct_access_session_is_held_to_the_same_wall_grant(self):
        made = self.s.user("tablet", sources=[A], walls=[WALL])
        self.assertTrue(self.s.store.enable_direct_access(made["id"])["ok"])
        token = self.s.store.direct_access_token(made["id"])
        client = self.s.client()
        self.assertEqual(client.get("/access/" + token).status_code, 200)
        body = client.get("/api/user/state").get_json()
        self.assertEqual([w["id"] for w in body["walls"]], [WALL])


if __name__ == "__main__":
    unittest.main()
