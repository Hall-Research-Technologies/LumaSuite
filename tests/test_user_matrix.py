"""The restricted User's Matrix and the routing-interface choice, through the HTTP surface.

Two things are proved here. First, the matrix is built from one account's grants and
nothing else: a source, display, group or wall it holds no grant for is not in the body at
all, and holding a group or a wall grants neither its members nor any other row. Second,
the interface a person may use is the server's decision, made again on every request from
the account as it is at that moment -- a hidden tab, a remembered choice and a
manipulated request body prove nothing.

Nothing here reaches a device: the A/V engine, the video/audio engine and the wall
operation are recorders.
"""
import copy
import json
import unittest

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, SOURCE_C, System, identity, saved_wall)

import luma_liveness
import luma_user_matrix
import luma_users

A, B, C = (identity(u) for u in (SOURCE_A, SOURCE_B, SOURCE_C))
D1, D2, D3, D4 = (identity(u) for u in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4))
ROUTE = "/api/user/route_multi"
WALL = "wall-aaaaaaaaaaaa"


def with_audio(unit, audio, video=None):
    """A copy of a unit carrying an audio stream (and optionally a different video one)."""
    unit = copy.deepcopy(unit)
    side = "enc" if unit["type"] == "encoder" else "dec"
    unit.setdefault(side, {})["audio"] = {"streamname": audio}
    if video is not None:
        unit[side]["video"] = {"streamname": video}
    return unit


def lab():
    """Three sources with audio streams of their own, four displays."""
    return [with_audio(SOURCE_A, "audio-alpha"), with_audio(SOURCE_B, "audio-bravo"),
            with_audio(SOURCE_C, "audio-charlie"),
            with_audio(DISPLAY_1, ""), with_audio(DISPLAY_2, ""),
            with_audio(DISPLAY_3, ""), with_audio(DISPLAY_4, "")]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.s = System(units=lab(), walls=[saved_wall(WALL, "Atrium Wall", [DISPLAY_1, DISPLAY_2])])
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        self.local = self.s.client(remote=False)

    def user_id(self, name="alex"):
        return next(u["id"] for u in self.s.store.list_accounts() if u["username"] == name)

    def signed_in(self, name="alex", **grants):
        self.s.user(name, **grants)
        return self.s.sign_in(name, USER_PASSWORD)

    def set_permissions(self, permissions, name="alex"):
        return self.local.post("/api/admin/users/" + self.user_id(name),
                               json={"action": "set_permissions", "permissions": permissions})

    def matrix(self, client):
        return client.get("/api/user/matrix")

    def route(self, client, source, mode="av", interface="matrix", displays=(), groups=(),
              walls=None, **extra):
        body = {"source": source, "displays": list(displays), "groups": list(groups),
                "interface": interface, "mode": mode}
        if walls is not None:
            body["walls"] = list(walls)
        body.update(extra)
        return self.s.post(client, ROUTE, body)

    def unit(self, mac):
        return next(u for u in self.s.units if u["mac"] == mac)

    def show(self, display, video=None, audio=None):
        unit = self.unit(display["mac"])
        if video is not None:
            unit["dec"]["video"] = {"streamname": video}
        if audio is not None:
            unit["dec"]["audio"] = {"streamname": audio}

    def go_offline(self, unit_source):
        unit = self.unit(unit_source["mac"])
        luma_liveness.record(unit, False)
        luma_liveness.record(unit, False)


class InterfacePermissionTests(unittest.TestCase):
    def test_an_account_stored_before_the_setting_keeps_standard_and_gains_no_matrix(self):
        old = luma_users.Permissions.from_json({"sources": [A], "destinations": [D1], "groups": []})
        self.assertTrue(old.standard_routing)
        self.assertFalse(old.matrix_routing)

    def test_the_choice_round_trips_and_survives_replace(self):
        perms = luma_users.Permissions([A], [D1], [], [], False, True)
        again = luma_users.Permissions.from_json(json.loads(json.dumps(perms.to_json())))
        self.assertEqual((again.standard_routing, again.matrix_routing), (False, True))
        self.assertEqual(perms.replace(sources=[B]).interfaces(), {"standard": False, "matrix": True})

    def test_a_record_with_neither_interface_is_read_as_standard_not_as_no_access(self):
        both_off = luma_users.Permissions.from_json({"standard_routing": False, "matrix_routing": False})
        self.assertTrue(both_off.standard_routing)

    def test_a_flag_that_is_not_a_boolean_is_ignored(self):
        junk = luma_users.Permissions.from_json({"standard_routing": "no", "matrix_routing": 1})
        self.assertEqual(junk.interfaces(), {"standard": True, "matrix": False})

    def test_an_unknown_interface_name_is_never_permitted(self):
        perms = luma_users.Permissions([A], [D1], [], [], True, True)
        self.assertFalse(perms.may_use_interface("admin"))
        self.assertFalse(perms.may_use_interface(None))


class AdminControlTests(Fixture):
    def create(self, permissions, role="user", name="pat"):
        return self.local.post("/api/admin/users", json={
            "username": name, "password": USER_PASSWORD, "role": role, "permissions": permissions})

    def test_creating_an_account_with_neither_interface_is_refused(self):
        response = self.create({"sources": [A], "standard_routing": False, "matrix_routing": False})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason"], "no_routing_interface")
        self.assertIsNone(self.s.store.find_by_username("pat"))

    def test_editing_an_account_to_neither_interface_is_refused_and_changes_nothing(self):
        self.s.user("alex", sources=[A], destinations=[D1])
        before = self.s.store.get(self.user_id()).permissions.to_json()
        response = self.set_permissions({"sources": [A], "destinations": [D1],
                                         "standard_routing": False, "matrix_routing": False})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.s.store.get(self.user_id()).permissions.to_json(), before)

    def test_matrix_only_is_a_valid_choice(self):
        self.assertEqual(self.create({"sources": [A], "standard_routing": False,
                                      "matrix_routing": True}).status_code, 200)
        perms = self.s.store.find_by_username("pat").permissions
        self.assertEqual(perms.interfaces(), {"standard": False, "matrix": True})

    def test_an_account_created_without_the_fields_is_standard_only(self):
        self.assertEqual(self.create({"sources": [A]}).status_code, 200)
        self.assertEqual(self.s.store.find_by_username("pat").permissions.interfaces(),
                         {"standard": True, "matrix": False})

    def test_changing_an_interface_is_audited_by_name_and_without_secrets(self):
        self.s.user("alex", sources=[A], destinations=[D1])
        self.set_permissions({"sources": [A], "destinations": [D1],
                              "standard_routing": True, "matrix_routing": True})
        self.set_permissions({"sources": [A], "destinations": [D1],
                              "standard_routing": False, "matrix_routing": True})
        events = [e["event"] for e in self.s.store.audit_entries(limit=50)]
        self.assertIn("user.matrix_routing_enabled", events)
        self.assertIn("user.standard_routing_disabled", events)
        self.assertNotIn(USER_PASSWORD, json.dumps(self.s.store.audit_entries(limit=50)))


class InterfaceEnforcementTests(Fixture):
    def test_state_says_which_interfaces_the_account_holds(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.assertEqual(client.get("/api/user/state").get_json()["routing"],
                         {"standard": True, "matrix": True})

    def test_standard_only_cannot_read_the_matrix(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.matrix(client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "interface_not_permitted")

    def test_standard_only_cannot_route_by_claiming_the_matrix(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.route(client, A, displays=[D1])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.s.router.calls, [])

    def test_standard_only_cannot_route_video_only_even_by_naming_standard(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.route(client, A, mode="video", interface="standard", displays=[D1])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.s.mode_router.calls, [])

    def test_matrix_only_cannot_use_the_standard_routes(self):
        group = self.s.remote.groups.create("Pair", [D3, D4])["group"]
        client = self.signed_in(sources=[A], destinations=[D1], groups=[group["id"]],
                                standard=True, matrix=True)
        self.set_permissions({"sources": [A], "destinations": [D1], "groups": [group["id"]],
                              "standard_routing": False, "matrix_routing": True})
        for path, body in (("/api/user/route", {"source": A, "destination": D1}),
                           ("/api/user/route_group", {"source": A, "group": group["id"]}),
                           (ROUTE, {"source": A, "displays": [D1], "groups": []})):
            response = self.s.post(client, path, body)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.get_json()["reason"], "interface_not_permitted", path)
        self.assertEqual(self.s.router.calls, [])
        self.assertEqual(self.route(client, A, displays=[D1]).status_code, 200)

    def test_both_interfaces_may_route(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.assertEqual(self.s.post(client, ROUTE, {"source": A, "displays": [D1],
                                                     "groups": []}).status_code, 200)
        self.assertEqual(self.route(client, A, displays=[D1]).status_code, 200)

    def test_removing_matrix_binds_the_very_next_request(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.assertEqual(self.matrix(client).status_code, 200)
        rev = client.get("/api/user/revision").get_json()["rev"]
        self.set_permissions({"sources": [A], "destinations": [D1],
                              "standard_routing": True, "matrix_routing": False})
        self.assertEqual(self.matrix(client).status_code, 403)
        self.assertEqual(self.route(client, A, displays=[D1]).status_code, 403)
        self.assertNotEqual(client.get("/api/user/revision").get_json()["rev"], rev)
        self.assertEqual(client.get("/api/user/state").get_json()["routing"],
                         {"standard": True, "matrix": False})

    def test_granting_matrix_changes_the_revision_and_opens_it_without_signing_in_again(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        rev = client.get("/api/user/revision").get_json()["rev"]
        self.set_permissions({"sources": [A], "destinations": [D1],
                              "standard_routing": True, "matrix_routing": True})
        self.assertNotEqual(client.get("/api/user/revision").get_json()["rev"], rev)
        self.assertEqual(self.matrix(client).status_code, 200)

    def test_standard_routing_removed_live_refuses_standard_and_keeps_the_matrix(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.set_permissions({"sources": [A], "destinations": [D1],
                              "standard_routing": False, "matrix_routing": True})
        self.assertEqual(self.s.post(client, "/api/user/route",
                                     {"source": A, "destination": D1}).status_code, 403)
        self.assertEqual(self.route(client, A, displays=[D1]).status_code, 200)

    def test_an_unauthenticated_request_for_the_matrix_is_refused_at_the_gate(self):
        self.assertIn(self.s.client(remote=True).get("/api/user/matrix").status_code, (401, 403))

    def test_a_demoted_or_deleted_account_loses_the_matrix_at_once(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.assertEqual(self.matrix(client).status_code, 200)
        self.assertTrue(self.s.store.delete(self.user_id())["ok"])
        self.assertEqual(self.matrix(client).status_code, 401)
        self.assertEqual(self.route(client, A, displays=[D1]).status_code, 401)

    def test_a_disabled_account_loses_the_matrix_at_once(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.s.store.set_enabled(self.user_id(), False)
        self.assertEqual(self.matrix(client).status_code, 401)


class ProjectionTests(Fixture):
    def test_only_the_permitted_sources_and_destinations_exist_in_the_body(self):
        client = self.signed_in(sources=[A], destinations=[D1, D2], matrix=True)
        body = self.matrix(client).get_json()
        self.assertEqual([s["id"] for s in body["sources"]], [A])
        self.assertEqual(sorted(r["id"] for r in body["rows"]), sorted([D1, D2]))
        text = json.dumps(body)
        for hidden in (B, C, D3, D4):
            self.assertNotIn(hidden, text)
        for unit in (SOURCE_B, SOURCE_C, DISPLAY_3, DISPLAY_4, SOURCE_A, DISPLAY_1):
            self.assertNotIn(unit["ip"], text)

    def test_the_matrix_is_not_the_admin_matrix_with_things_hidden(self):
        client = self.signed_in(sources=[A], destinations=[], matrix=True)
        body = self.matrix(client).get_json()
        self.assertEqual(body["rows"], [])
        for row_cells in [r["cells"] for r in body["rows"]]:
            self.assertEqual(set(row_cells), {A})

    def test_a_group_is_one_row_and_its_members_are_not_expanded(self):
        group = self.s.remote.groups.create("Pair", [D3, D4])["group"]
        client = self.signed_in(sources=[A], destinations=[D1], groups=[group["id"]], matrix=True)
        rows = self.matrix(client).get_json()["rows"]
        self.assertEqual([(r["kind"], r["id"]) for r in rows],
                         [("group", group["id"]), ("display", D1)])
        self.assertEqual(rows[0]["members"], 2)

    def test_a_group_the_account_was_not_given_is_not_in_the_body_at_all(self):
        other = self.s.remote.groups.create("Backstage", [D3, D4])["group"]
        mine = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[mine["id"]], matrix=True)
        body = self.matrix(client).get_json()
        self.assertEqual([r["id"] for r in body["rows"]], [mine["id"]])
        self.assertNotIn(other["id"], json.dumps(body))
        self.assertNotIn("Backstage", json.dumps(body))

    def test_a_wall_the_account_was_not_given_is_not_in_the_body_either(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        body = self.matrix(client).get_data(as_text=True)
        self.assertNotIn(WALL, body)
        self.assertNotIn("Atrium Wall", body)

    def test_a_wall_is_its_own_row_and_grants_none_of_its_decoders(self):
        client = self.signed_in(sources=[A], walls=[WALL], matrix=True)
        rows = self.matrix(client).get_json()["rows"]
        self.assertEqual([(r["kind"], r["id"], r["modes"]) for r in rows],
                         [("wall", WALL, ["av", "video", "audio"])])
        self.assertEqual(self.route(client, A, displays=[D1]).status_code, 403)

    def test_rows_are_ordered_walls_then_groups_then_displays_deterministically(self):
        group = self.s.remote.groups.create("Pair", [D3, D4])["group"]
        client = self.signed_in(sources=[A], destinations=[D2, D1], groups=[group["id"]],
                                walls=[WALL], matrix=True)
        rows = self.matrix(client).get_json()["rows"]
        self.assertEqual([r["kind"] for r in rows], ["wall", "group", "display", "display"])
        self.assertEqual([r["name"] for r in rows if r["kind"] == "display"], ["Lobby One", "Lobby Two"])

    def test_the_body_never_carries_an_address_or_a_mac_of_an_unpermitted_unit(self):
        client = self.signed_in(sources=[A, B], destinations=[D1], matrix=True)
        text = self.matrix(client).get_data(as_text=True)
        for unit in (SOURCE_C, DISPLAY_2, DISPLAY_3):
            self.assertNotIn(unit["mac"], text)
            self.assertNotIn(unit["ip"], text)


class CellStateTests(Fixture):
    def cells(self, client, row_id, kind="display"):
        rows = self.matrix(client).get_json()["rows"]
        return next(r for r in rows if r["id"] == row_id and r["kind"] == kind)

    def test_av_together_lights_the_ring_and_the_dot_in_one_column(self):
        client = self.signed_in(sources=[A, B], destinations=[D1], matrix=True)
        self.show(DISPLAY_1, video="stream-alpha", audio="audio-alpha")
        row = self.cells(client, D1)
        self.assertEqual((row["cells"][A]["video"], row["cells"][A]["audio"]), ("all", "all"))
        self.assertEqual((row["cells"][B]["video"], row["cells"][B]["audio"]), ("none", "none"))

    def test_video_only(self):
        client = self.signed_in(sources=[A, B], destinations=[D1], matrix=True)
        self.show(DISPLAY_1, video="stream-alpha", audio="")
        row = self.cells(client, D1)
        self.assertEqual((row["cells"][A]["video"], row["cells"][A]["audio"]), ("all", "none"))

    def test_audio_only(self):
        client = self.signed_in(sources=[A, B], destinations=[D1], matrix=True)
        self.show(DISPLAY_1, video="", audio="audio-bravo")
        row = self.cells(client, D1)
        self.assertEqual((row["cells"][B]["video"], row["cells"][B]["audio"]), ("none", "all"))

    def test_breakaway_shows_video_and_audio_from_different_sources(self):
        client = self.signed_in(sources=[A, B], destinations=[D1], matrix=True)
        self.show(DISPLAY_1, video="stream-alpha", audio="audio-bravo")
        row = self.cells(client, D1)
        self.assertEqual((row["cells"][A]["video"], row["cells"][A]["audio"]), ("all", "none"))
        self.assertEqual((row["cells"][B]["video"], row["cells"][B]["audio"]), ("none", "all"))

    def test_a_source_the_account_may_not_use_is_never_named_in_a_cell(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.show(DISPLAY_1, video="stream-bravo", audio="audio-bravo")
        row = self.cells(client, D1)
        self.assertEqual(set(row["cells"]), {A})
        self.assertEqual((row["cells"][A]["video"], row["cells"][A]["audio"]), ("none", "none"))

    def test_a_stream_two_encoders_claim_is_ambiguous_and_lights_nothing(self):
        self.unit(SOURCE_B["mac"])["enc"]["video"] = {"streamname": "stream-alpha"}
        client = self.signed_in(sources=[A, B], destinations=[D1], matrix=True)
        self.show(DISPLAY_1, video="stream-alpha")
        row = self.cells(client, D1)
        self.assertEqual((row["cells"][A]["video"], row["cells"][B]["video"]), ("none", "none"))

    def test_displays_on_different_unpermitted_sources_are_the_same_answer_to_this_account(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        self.show(DISPLAY_1, video="stream-bravo", audio="audio-bravo")
        self.show(DISPLAY_2, video="stream-charlie", audio="audio-charlie")
        row = self.cells(client, group["id"], "group")
        self.assertEqual(row["state"], "all", "the account is not told that two strangers differ")
        self.assertEqual((row["cells"][A]["video"], row["cells"][A]["audio"]), ("none", "none"))

    def test_a_display_on_an_unpermitted_source_is_not_the_same_as_an_unrouted_one(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        self.show(DISPLAY_1, video="stream-bravo")
        self.show(DISPLAY_2, video="")
        self.assertEqual(self.cells(client, group["id"], "group")["state"], "mixed")

    def test_a_group_is_all_only_when_every_member_holds_the_source(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        self.show(DISPLAY_1, video="stream-alpha", audio="audio-alpha")
        self.show(DISPLAY_2, video="stream-alpha", audio="audio-alpha")
        row = self.cells(client, group["id"], "group")
        self.assertEqual((row["state"], row["cells"][A]["video"], row["cells"][A]["audio"]),
                         ("all", "all", "all"))
        self.assertEqual(row["cells"][A]["video_n"], 2)

    def test_a_group_whose_members_differ_is_mixed_never_one_active_state(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A, B], groups=[group["id"]], matrix=True)
        self.show(DISPLAY_1, video="stream-alpha", audio="audio-alpha")
        self.show(DISPLAY_2, video="stream-bravo", audio="audio-bravo")
        row = self.cells(client, group["id"], "group")
        self.assertEqual(row["state"], "mixed")
        self.assertEqual(row["cells"][A]["video"], "partial")
        self.assertEqual(row["cells"][B]["video"], "partial")

    def test_a_group_with_one_member_off_the_source_is_partial_in_the_cell(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        self.show(DISPLAY_1, video="stream-alpha")
        self.show(DISPLAY_2, video="")
        row = self.cells(client, group["id"], "group")
        self.assertEqual(row["cells"][A]["video"], "partial")

    def test_an_offline_member_can_never_make_a_group_cell_all(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        self.show(DISPLAY_1, video="stream-alpha")
        self.show(DISPLAY_2, video="stream-alpha")
        self.go_offline(DISPLAY_2)
        row = self.cells(client, group["id"], "group")
        self.assertEqual(row["state"], "partial")
        self.assertTrue(row["degraded"])
        self.assertEqual(row["cells"][A]["video"], "partial")

    def test_a_group_with_nothing_answering_is_unavailable(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        self.go_offline(DISPLAY_1)
        self.go_offline(DISPLAY_2)
        self.assertEqual(self.cells(client, group["id"], "group")["state"], "unavailable")

    def test_a_wall_row_reports_its_members_like_a_group(self):
        client = self.signed_in(sources=[A], walls=[WALL], matrix=True)
        self.show(DISPLAY_1, video="stream-alpha")
        self.show(DISPLAY_2, video="stream-alpha")
        row = self.cells(client, WALL, "wall")
        self.assertEqual((row["state"], row["cells"][A]["video"]), ("all", "all"))

    def test_an_offline_display_is_marked_unavailable_and_an_offline_source_is_offline(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.go_offline(DISPLAY_1)
        self.go_offline(SOURCE_A)
        body = self.matrix(client).get_json()
        self.assertEqual(body["rows"][0]["state"], "unavailable")
        self.assertFalse(body["sources"][0]["online"])


class RoutingTests(Fixture):
    def test_av_from_the_matrix_uses_the_one_av_engine(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        response = self.route(client, A, "av", displays=[D1])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(self.s.router.calls, [(DISPLAY_1["ip"], SOURCE_A["ip"])])
        self.assertEqual(self.s.mode_router.calls, [])

    def test_video_only_and_audio_only_use_the_breakaway_engine_with_the_named_source(self):
        client = self.signed_in(sources=[A, B], destinations=[D1], matrix=True)
        self.assertEqual(self.route(client, A, "video", displays=[D1]).status_code, 200)
        self.assertEqual(self.route(client, B, "audio", displays=[D1]).status_code, 200)
        self.assertEqual(self.s.mode_router.calls, [(DISPLAY_1["ip"], "video", SOURCE_A["ip"]),
                                                    (DISPLAY_1["ip"], "audio", SOURCE_B["ip"])])
        self.assertEqual(self.s.router.calls, [])

    def test_a_breakaway_is_video_from_one_source_then_audio_from_another(self):
        client = self.signed_in(sources=[A, B], destinations=[D1], matrix=True)
        self.route(client, A, "video", displays=[D1])
        self.route(client, B, "audio", displays=[D1])
        self.assertEqual([c[1] for c in self.s.mode_router.calls], ["video", "audio"])
        self.assertEqual([c[2] for c in self.s.mode_router.calls], [SOURCE_A["ip"], SOURCE_B["ip"]])

    def test_a_group_routes_every_member_in_order_with_the_same_mode(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        response = self.route(client, A, "video", groups=[group["id"]])
        body = response.get_json()
        self.assertEqual((body["routed"], body["total"]), (2, 2))
        self.assertEqual(self.s.mode_router.calls, [(DISPLAY_1["ip"], "video", SOURCE_A["ip"]),
                                                    (DISPLAY_2["ip"], "video", SOURCE_A["ip"])])

    def test_a_group_with_an_offline_member_reports_partial_and_never_writes_to_it(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        self.go_offline(DISPLAY_2)
        response = self.route(client, A, "av", groups=[group["id"]])
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual((body["routed"], body["total"]), (1, 2))
        self.assertEqual(self.s.router.calls, [(DISPLAY_1["ip"], SOURCE_A["ip"])])
        self.assertIn("offline", [r["result"] for r in body["results"]])

    def test_a_wall_routes_through_the_wall_operation_and_reports_only_n_of_m(self):
        client = self.signed_in(sources=[A], walls=[WALL], matrix=True)
        self.s.wall_router.result = {"ok": False, "status": "partial", "routed": 1, "total": 2}
        response = self.route(client, A, "av", walls=[WALL])
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["results"][0]["result"], "partial")
        self.assertEqual((body["results"][0]["routed"], body["results"][0]["total"]), (1, 2))
        self.assertEqual(self.s.wall_router.calls, [(WALL, A)])
        self.assertEqual(self.s.router.calls, [])

    def test_a_wall_is_routed_in_the_mode_chosen(self):
        client = self.signed_in(sources=[A], walls=[WALL], matrix=True)
        for mode in ("av", "video", "audio"):
            response = self.route(client, A, mode, walls=[WALL])
            self.assertEqual(response.status_code, 200, (mode, response.get_json()))
        self.assertEqual(self.s.wall_router.planes, ["av", "video", "audio"])

    def test_an_offline_source_is_refused_before_any_write(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.go_offline(SOURCE_A)
        response = self.route(client, A, "video", displays=[D1])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.s.mode_router.calls, [])

    def test_an_offline_display_is_reported_offline_and_never_written_to(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.go_offline(DISPLAY_1)
        body = self.route(client, A, "audio", displays=[D1]).get_json()
        self.assertEqual(body["results"][0]["result"], "offline")
        self.assertEqual(self.s.mode_router.calls, [])

    def test_an_engine_refusal_is_not_reported_as_a_permission_refusal(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.s.mode_router.fail[DISPLAY_1["ip"]] = "refused"
        body = self.route(client, A, "video", displays=[D1]).get_json()
        self.assertEqual(body["results"][0]["result"], "failed")

    def test_a_display_that_does_not_confirm_is_a_failure_not_a_success(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.s.mode_router.fail[DISPLAY_1["ip"]] = "not_verified"
        body = self.route(client, A, "video", displays=[D1]).get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["results"][0]["result"], "not_verified")

    def test_matrix_routes_are_audited_as_matrix_routes_with_the_mode(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.route(client, A, "video", displays=[D1])
        entry = next(e for e in self.s.store.audit_entries(limit=50) if e["event"] == "route_matrix")
        self.assertEqual(entry["detail"]["mode"], "video")
        self.assertNotIn(USER_PASSWORD, json.dumps(entry))

    def test_a_standard_route_is_still_audited_as_before(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.route(client, A, "av", interface="standard", displays=[D1])
        events = [e["event"] for e in self.s.store.audit_entries(limit=50)]
        self.assertIn("route_multi", events)
        self.assertNotIn("route_matrix", events)


class AuthorisationTests(Fixture):
    def test_every_manipulated_identifier_is_refused_and_nothing_is_written(self):
        group = self.s.remote.groups.create("Other", [D3, D4])["group"]
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        for body in ({"source": B, "displays": [D1]},                 # source not granted
                     {"source": A, "displays": [D2]},                 # display not granted
                     {"source": A, "groups": [group["id"]]},          # group not granted
                     {"source": A, "walls": [WALL]},                  # wall not granted
                     {"source": A, "displays": [D1, D3]}):            # one bad apple spoils the batch
            response = self.route(client, body["source"], "video",
                                  displays=body.get("displays", ()), groups=body.get("groups", ()),
                                  walls=body.get("walls"))
            self.assertEqual(response.status_code, 403, body)
        self.assertEqual(self.s.mode_router.calls, [])
        self.assertEqual(self.s.router.calls, [])
        self.assertEqual(self.s.wall_router.calls, [])

    def test_a_group_grant_does_not_let_a_person_route_its_members_individually(self):
        group = self.s.remote.groups.create("Pair", [D1, D2])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        self.assertEqual(self.route(client, A, "video", displays=[D1]).status_code, 403)

    def test_a_display_grant_does_not_grant_a_wall_containing_it(self):
        client = self.signed_in(sources=[A], destinations=[D1, D2], matrix=True)
        self.assertEqual(self.route(client, A, "av", walls=[WALL]).status_code, 403)

    def test_malformed_requests_are_400_and_never_500(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        bad = [
            {"source": A, "displays": [D1], "interface": "matrix", "mode": "breakaway"},
            {"source": A, "displays": [D1], "interface": "matrix", "mode": ["video"]},
            {"source": A, "displays": [D1], "interface": "matrix", "mode": 7},
            {"source": A, "displays": [D1], "interface": ["matrix"], "mode": "av"},
            {"source": A, "displays": [D1], "interface": "admin", "mode": "av"},
            {"source": A, "displays": [D1], "interface": None, "mode": "av"},
            {"source": A, "displays": "x", "interface": "matrix"},
            {"source": 5, "displays": [D1], "interface": "matrix"},
            {"source": A, "displays": [None], "interface": "matrix"},
            {"source": A, "displays": [], "groups": [], "interface": "matrix"},
            {"displays": [D1], "interface": "matrix"},
            {"source": A, "displays": [D1], "interface": "matrix", "mode": {"x": 1}},
        ]
        for body in bad:
            response = self.s.post(client, ROUTE, body)
            self.assertIn(response.status_code, (400, 403), body)
        for raw in ("[]", "null", "7", '"x"'):
            response = client.post(ROUTE, data=raw, content_type="application/json",
                                   headers={"X-LumaSuite-CSRF": client.csrf})
            self.assertLess(response.status_code, 500, raw)
        self.assertEqual(self.s.mode_router.calls, [])
        self.assertEqual(self.s.router.calls, [])

    def test_unknown_ids_in_the_matrix_request_are_refused_not_searched_for(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        for ident in ("02:00:5e:99:99:99", "../../etc", "x" * 500):
            self.assertEqual(self.route(client, ident, "av", displays=[D1]).status_code, 403)
            self.assertEqual(self.route(client, A, "av", displays=[ident]).status_code, 403)

    def test_a_csrf_less_matrix_route_is_refused(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        response = client.post(ROUTE, json={"source": A, "displays": [D1], "groups": [],
                                            "interface": "matrix", "mode": "av"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.s.router.calls, [])


class LiveChangeTests(Fixture):
    def test_a_source_added_live_appears_and_one_removed_disappears(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.set_permissions({"sources": [A, B], "destinations": [D1],
                              "standard_routing": True, "matrix_routing": True})
        self.assertEqual([s["id"] for s in self.matrix(client).get_json()["sources"]], [A, B])
        self.set_permissions({"sources": [B], "destinations": [D1],
                              "standard_routing": True, "matrix_routing": True})
        self.assertEqual([s["id"] for s in self.matrix(client).get_json()["sources"]], [B])
        self.assertEqual(self.route(client, A, "av", displays=[D1]).status_code, 403)

    def test_a_display_a_group_and_a_wall_added_live_appear_in_the_next_read(self):
        group = self.s.remote.groups.create("Pair", [D3, D4])["group"]
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.set_permissions({"sources": [A], "destinations": [D1], "groups": [group["id"]],
                              "walls": [WALL], "standard_routing": True, "matrix_routing": True})
        kinds = [r["kind"] for r in self.matrix(client).get_json()["rows"]]
        self.assertEqual(kinds, ["wall", "group", "display"])

    def test_a_group_membership_change_reaches_the_matrix_and_the_revision(self):
        group = self.s.remote.groups.create("Pair", [D3, D4])["group"]
        client = self.signed_in(sources=[A], groups=[group["id"]], matrix=True)
        rev = client.get("/api/user/revision").get_json()["rev"]
        self.s.remote.groups.update(group["id"], members=[D3])
        self.assertNotEqual(client.get("/api/user/revision").get_json()["rev"], rev)
        self.assertEqual(self.matrix(client).get_json()["rows"][0]["members"], 1)

    def test_a_wall_removed_from_the_saved_definitions_leaves_the_matrix(self):
        client = self.signed_in(sources=[A], walls=[WALL], matrix=True)
        self.assertEqual(len(self.matrix(client).get_json()["rows"]), 1)
        self.s.wall_defs.clear()
        self.assertEqual(self.matrix(client).get_json()["rows"], [])
        self.assertEqual(self.route(client, A, "av", walls=[WALL]).status_code, 404)


class DirectAccessTests(Fixture):
    def link_client(self, **grants):
        self.s.user("alex", **grants)
        link = self.local.post("/api/admin/users/%s/direct_access" % self.user_id(),
                               json={"action": "enable"}).get_json()["link"]
        client = self.s.client(remote=True)
        self.assertEqual(client.get("/access/" + link.rsplit("/", 1)[1]).status_code, 200)
        return client, link

    def test_a_direct_access_session_follows_the_accounts_interfaces(self):
        client, _link = self.link_client(sources=[A], destinations=[D1], matrix=True)
        self.assertEqual(client.get("/api/user/state").get_json()["routing"],
                         {"standard": True, "matrix": True})
        self.assertEqual(self.matrix(client).status_code, 200)

    def test_standard_only_direct_access_cannot_read_the_matrix(self):
        client, _link = self.link_client(sources=[A], destinations=[D1])
        self.assertEqual(self.matrix(client).status_code, 403)

    def test_changing_the_interface_converges_an_open_direct_access_client(self):
        client, _link = self.link_client(sources=[A], destinations=[D1], matrix=True)
        rev = client.get("/api/user/revision").get_json()["rev"]
        self.set_permissions({"sources": [A], "destinations": [D1],
                              "standard_routing": False, "matrix_routing": False})   # refused
        self.set_permissions({"sources": [A], "destinations": [D1],
                              "standard_routing": True, "matrix_routing": False})
        self.assertNotEqual(client.get("/api/user/revision").get_json()["rev"], rev)
        self.assertEqual(self.matrix(client).status_code, 403)

    def test_a_regenerated_link_kills_the_old_session_and_the_new_one_has_the_matrix(self):
        client, old = self.link_client(sources=[A], destinations=[D1], matrix=True)
        new = self.local.post("/api/admin/users/%s/direct_access" % self.user_id(),
                              json={"action": "regenerate"}).get_json()["link"]
        self.assertEqual(self.matrix(client).status_code, 401)
        fresh = self.s.client(remote=True)
        fresh.get("/access/" + new.rsplit("/", 1)[1])
        self.assertEqual(self.matrix(fresh).status_code, 200)
        self.assertEqual(self.s.client(remote=True).get("/access/" + old.rsplit("/", 1)[1]).status_code, 404)


class PreviewFenceTests(Fixture):
    def test_reading_the_matrix_requests_no_picture(self):
        client = self.signed_in(sources=[A, B], destinations=[D1, D2], matrix=True)
        for _ in range(3):
            self.assertEqual(self.matrix(client).status_code, 200)
        self.assertEqual(self.s.previews, [])

    def test_a_preview_for_an_unpermitted_source_is_refused_from_the_matrix_too(self):
        client = self.signed_in(sources=[A], destinations=[D1], matrix=True)
        self.assertEqual(client.get("/api/user/preview?source=" + B).status_code, 403)
        self.assertEqual(self.s.previews, [])


class ModuleTests(unittest.TestCase):
    def test_the_modes_a_row_may_be_asked_for(self):
        self.assertEqual(luma_user_matrix.MODES_FOR_ROW["wall"], ("av", "video", "audio"))
        self.assertEqual(luma_user_matrix.MODES_FOR_ROW["display"], ("av", "video", "audio"))
        self.assertEqual(luma_user_matrix.MODES_FOR_ROW["group"], ("av", "video", "audio"))


if __name__ == "__main__":
    unittest.main()
