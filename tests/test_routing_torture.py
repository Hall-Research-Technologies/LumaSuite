"""Routing against a model that holds state: the final state of the devices is the assertion.

Every other routing test here answers "what did the HTTP response say" or "what did a recorder see".
These drive the real routes -- the verified A/V route, the video-only and audio-only engine, the wall
operation, the shared refresh and the User matrix -- against `_devicesim.DeviceSim`, which behaves as
the measured decoders do, and then read the *model*: what each decoder holds, what it was told, and
whether the matrix a person is looking at says the same.

Two layers. Most scenarios replace the transport call (`_ws_call_auth`) with the model. The transport
failures -- a unit that hangs, goes quiet, replies with garbage, replies to the wrong request, or
closes the connection -- need the real `_ws_call`, so those run against a loopback WebSocket stand-in
for a unit. Nothing leaves loopback and the hardware fence stays installed; a test asserts at the end
that no mutating call reached an address the model does not own.
"""
import copy
import os
import random
import threading
import time
import unittest
from unittest import mock

import _support                      # installs the fence and isolates the data dir first
import _fence
from _devicesim import DeviceSim, WsFixture
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, SOURCE_C, RealApp, RealRemote, identity,
                      saved_wall, wall_device_id)

import lumaserver
import luma_liveness
import luma_users

REAL_AUTH = lumaserver._ws_call_auth     # the real transport entry, kept for the socket-level tests

A, B, C = identity(SOURCE_A), identity(SOURCE_B), identity(SOURCE_C)
D1, D2, D3, D4 = (identity(DISPLAY_1), identity(DISPLAY_2), identity(DISPLAY_3), identity(DISPLAY_4))
ROUTE = "/api/user/route_multi"
SOURCES = (SOURCE_A, SOURCE_B, SOURCE_C)
DISPLAYS = (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4)
VIDEO = {A: "stream-alpha", B: "stream-bravo", C: "stream-charlie"}
AUDIO = {A: "audio-alpha", B: "audio-bravo", C: "audio-charlie"}
FIXTURE_PREFIXES = ("192.0.2.", "198.51.100.")


def fence_attempts():
    """What the fence has refused so far for the fixture addresses."""
    return {k: v for k, v in _fence.attempts.items() if any(p in str(k) for p in FIXTURE_PREFIXES)}


class Base(unittest.TestCase):
    """The real application, the transport replaced by one stateful model."""

    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.sim = DeviceSim.lab(SOURCES, DISPLAYS)
        self.app.patch("_ws_call_auth", self.sim.call)
        self.app.seed()
        self._fenced_before = fence_attempts()
        self.remote = RealRemote(self, self.app)
        self.system = self.remote.remote
        self.store = self.remote.store
        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.local = self.m.APP.test_client()
        self.addCleanup(self._remove_walls)
        self.addCleanup(self._nothing_escaped)

    # ---- guards -------------------------------------------------------------
    def _remove_walls(self):
        self.m._save_video_wall_state({"walls": [], "ignored_candidates": []})

    def _nothing_escaped(self):
        """Asserted in cleanup as well as in the tests: service code turns errors into results."""
        self.assertEqual(self.sim.violations, [], "the model saw a protocol rule broken")
        self.assertEqual([c for c in self.sim.unknown if c[1].endswith(".Set")], [],
                         "a write was addressed to a unit the model does not own")
        self.assertEqual(fence_attempts(), self._fenced_before,
                         "something tried the real transport for a fixture address")

    # ---- the lab ------------------------------------------------------------
    def wall(self, members, wid="w1", name="Atrium Wall", source=None):
        entry = saved_wall(wid, name, members, source_id=wall_device_id(source) if source else "")
        state = self.m._load_video_wall_state()
        state["walls"] = [w for w in state["walls"] if w.get("id") != wid] + [entry]
        self.m._save_video_wall_state(state)

    def group(self, members, name="Lobby"):
        return self.system.groups.create(name, list(members))["group"]["id"]

    def make_user(self, name="alex", sources=(A, B, C), destinations=(D1, D2, D3, D4), groups=(),
                  walls=(), standard=True, matrix=True):
        result = self.store.create(name, USER_PASSWORD, luma_users.ROLE_USER,
                                   luma_users.Permissions(sources, destinations, groups, walls,
                                                          standard, matrix))
        self.assertTrue(result["ok"], result)
        return self.remote.sign_in(name, USER_PASSWORD)

    def user_id(self, name="alex"):
        return next(u["id"] for u in self.store.list_accounts() if u["username"] == name)

    def set_permissions(self, name="alex", **perms):
        body = {"sources": [A, B, C], "destinations": [D1, D2, D3, D4], "groups": [], "walls": [],
                "standard_routing": True, "matrix_routing": True}
        body.update(perms)
        return self.local.post("/api/admin/users/" + self.user_id(name),
                               json={"action": "set_permissions", "permissions": body})

    def route(self, client, source, displays=(), groups=(), walls=(), mode="av", interface="matrix"):
        body = {"source": source, "displays": list(displays), "groups": list(groups),
                "walls": list(walls), "interface": interface, "mode": mode}
        return self.remote.post(client, ROUTE, body)

    def unit(self, mac):
        with self.m.cache_lock:
            return next(u for u in self.m.cache_units.values() if u["mac"] == mac)

    def go_offline(self, unit):
        """A unit that has stopped answering: silent on the wire and past the miss limit."""
        self.sim.down.add(unit["ip"])
        with self.m.cache_lock:
            cached = self.m.cache_units[unit["ip"]]
            luma_liveness.record(cached, False)
            luma_liveness.record(cached, False)

    def come_back(self, unit):
        self.sim.down.discard(unit["ip"])
        with self.m.cache_lock:
            luma_liveness.record(self.m.cache_units[unit["ip"]], True)

    def scanned_as(self, display, video, audio):
        """What a scan leaves in the cache for a decoder, and the same state on the device."""
        with self.m.cache_lock:
            cached = self.m.cache_units[display["ip"]]
            cached.setdefault("dec", {})["video"] = {"streamname": VIDEO[video]}
            cached["dec"]["audio"] = {"streamname": AUDIO[audio]}
        d = self.sim.decoder(display["ip"])
        d.video, d.audio, d.follow = VIDEO[video], AUDIO[audio], True

    def matrix(self, client):
        self.m._refresh_matrix_state_core()
        response = client.get("/api/user/matrix")
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    @staticmethod
    def row(matrix, rid):
        return next(r for r in matrix["rows"] if r["id"] == rid)

    def results(self, response):
        self.assertLess(response.status_code, 500, response.get_json())
        return [r["result"] for r in response.get_json().get("results", [])]


class FinalStateTests(Base):
    """The model is read after the route, not the response."""

    def test_rapid_alternation_ends_on_the_last_source_and_every_step_held(self):
        client = self.make_user()
        for i in range(24):
            source = (A, B)[i % 2]
            response = self.route(client, source, displays=[D1], interface="standard")
            self.assertEqual(self.results(response), ["verified"], i)
            self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[source], AUDIO[source], True), i)
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[B], True))
        self.assertTrue(self.sim.consistent(DISPLAY_1["ip"]))
        for other in (DISPLAY_2, DISPLAY_3, DISPLAY_4):
            self.assertEqual(self.sim.methods(other["ip"]), [], "a display nobody named was never asked")

    def test_several_displays_each_hold_the_source_and_the_others_are_untouched(self):
        client = self.make_user()
        response = self.route(client, A, displays=[D1, D2, D3], interface="standard")
        self.assertEqual(self.results(response), ["verified"] * 3)
        for display in (DISPLAY_1, DISPLAY_2, DISPLAY_3):
            self.assertEqual(self.sim.held(display["ip"]), (VIDEO[A], AUDIO[A], True))
        self.assertEqual(self.sim.held(DISPLAY_4["ip"]), ("", "", True))
        self.assertEqual(self.sim.writes(DISPLAY_4["ip"]), [])

    def test_a_group_routes_every_member_and_nothing_else(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        response = self.route(client, B, groups=[gid])
        self.assertEqual(self.results(response), ["verified", "verified"])
        for display in (DISPLAY_1, DISPLAY_2):
            self.assertEqual(self.sim.held(display["ip"]), (VIDEO[B], AUDIO[B], True))
        for display in (DISPLAY_3, DISPLAY_4):
            self.assertEqual(self.sim.methods(display["ip"]), [])

    def test_a_saved_wall_holds_the_source_and_its_matrix_on_every_member(self):
        self.wall([DISPLAY_3, DISPLAY_4])
        client = self.make_user(destinations=(), walls=("w1",))
        response = self.route(client, C, walls=["w1"], interface="standard")
        self.assertEqual(self.results(response), ["verified"])
        for display in (DISPLAY_3, DISPLAY_4):
            d = self.sim.decoder(display["ip"])
            self.assertEqual(d.video, VIDEO[C])
            self.assertTrue(d.wall["vmenable"])
        self.assertEqual(self.sim.decoder(DISPLAY_3["ip"]).wall["vwmatrix"], "1_2_1_1")
        self.assertEqual(self.sim.decoder(DISPLAY_4["ip"]).wall["vwmatrix"], "1_2_1_2")
        self.assertEqual(self.sim.writes(DISPLAY_1["ip"]) + self.sim.writes(DISPLAY_2["ip"]), [])

    def test_av_then_video_then_audio_is_a_breakaway_and_av_again_puts_it_back(self):
        client = self.make_user()
        self.scanned_as(DISPLAY_1, A, A)
        self.assertEqual(self.results(self.route(client, A, displays=[D1])), ["verified"])
        self.assertEqual(self.results(self.route(client, B, displays=[D1], mode="video")), ["verified"])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[A], False),
                         "video moved, the audio was pinned where it was")
        self.assertEqual(self.results(self.route(client, C, displays=[D1], mode="audio")), ["verified"])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[C], False), "a breakaway")
        row = self.row(self.matrix(client), D1)
        self.assertEqual((row["cells"][B]["video"], row["cells"][C]["audio"]), ("all", "all"))
        self.assertEqual((row["cells"][B]["audio"], row["cells"][C]["video"]), ("none", "none"))
        # A/V means both planes to the chosen source. After a breakaway the audio is brought along
        # explicitly, and the display is left not following -- the setting the breakaway chose is
        # not rewritten, because the audio no longer depends on it.
        again = self.route(client, A, displays=[D1])
        self.assertEqual(self.results(again), ["verified"])
        self.assertEqual(again.get_json()["results"][0]["audio"], "routed")
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], False))
        row = self.row(self.matrix(client), D1)
        self.assertEqual((row["cells"][A]["video"], row["cells"][A]["audio"]), ("all", "all"))
        self.assertEqual((row["cells"][C]["video"], row["cells"][C]["audio"]), ("none", "none"),
                         "nothing of the breakaway is left on the matrix")

    def test_audio_only_never_moves_the_video_and_video_only_never_moves_the_audio(self):
        client = self.make_user()
        self.scanned_as(DISPLAY_2, A, A)
        self.route(client, C, displays=[D2], mode="audio")
        self.assertEqual(self.sim.held(DISPLAY_2["ip"])[0], VIDEO[A])
        self.route(client, B, displays=[D2], mode="video")
        self.assertEqual(self.sim.held(DISPLAY_2["ip"]), (VIDEO[B], AUDIO[C], False))

    def test_the_matrix_agrees_with_the_decoder_after_a_plain_av_route(self):
        # The cache holds what a scan found; the route then changes the decoder. Audio follows the
        # video on the device, so the matrix must say the audio is the new source's, not the old one's.
        client = self.make_user()
        self.scanned_as(DISPLAY_1, A, A)
        self.assertEqual(self.results(self.route(client, B, displays=[D1], interface="standard")), ["verified"])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[B], True))
        row = self.row(self.matrix(client), D1)
        self.assertEqual((row["cells"][B]["video"], row["cells"][B]["audio"]), ("all", "all"))
        self.assertEqual((row["cells"][A]["video"], row["cells"][A]["audio"]), ("none", "none"))

    def test_the_cache_records_what_the_decoder_reported_about_its_audio_and_whether_it_follows(self):
        client = self.make_user()
        self.scanned_as(DISPLAY_1, A, A)
        self.route(client, B, displays=[D1], interface="standard")
        audio = self.app.unit(DISPLAY_1["ip"])["dec"]["audio"]
        self.assertEqual((audio["streamname"], audio["followvideoenable"]), (AUDIO[B], True))
        self.route(client, C, displays=[D1], mode="audio")
        audio = self.app.unit(DISPLAY_1["ip"])["dec"]["audio"]
        self.assertEqual((audio["streamname"], audio["followvideoenable"]), (AUDIO[C], False))
        self.route(client, A, displays=[D1], interface="standard")       # A/V: both planes move
        audio = self.app.unit(DISPLAY_1["ip"])["dec"]["audio"]
        self.assertEqual((audio["streamname"], audio["followvideoenable"]), (AUDIO[A], False))
        self.route(client, B, displays=[D1], mode="video")                # video only: audio stays
        audio = self.app.unit(DISPLAY_1["ip"])["dec"]["audio"]
        self.assertEqual((audio["streamname"], audio["followvideoenable"]), (AUDIO[A], False),
                         "a route that did not move the audio does not claim it did")

    def test_a_route_that_reads_no_audio_leaves_the_cache_as_it_was(self):
        client = self.make_user()
        self.scanned_as(DISPLAY_1, A, A)
        self.sim.lose_replies.add((DISPLAY_1["ip"], "SelectAudioStream.Get"))
        self.route(client, B, displays=[D1], interface="standard")
        audio = self.app.unit(DISPLAY_1["ip"])["dec"]["audio"]
        self.assertEqual(audio["streamname"], AUDIO[A], "an unanswered read is not an answer")

    def test_the_matrix_agrees_with_the_decoder_after_a_group_route(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        self.scanned_as(DISPLAY_1, A, A)
        self.scanned_as(DISPLAY_2, A, A)
        self.route(client, C, groups=[gid], interface="standard")
        row = self.row(self.matrix(client), gid)
        self.assertEqual(row["state"], "all")
        self.assertEqual((row["cells"][C]["video"], row["cells"][C]["audio"]), ("all", "all"))
        self.assertEqual(row["cells"][A]["audio"], "none")


class OfflineAndReturnTests(Base):
    def test_an_offline_member_is_never_written_and_the_rest_are(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        self.go_offline(DISPLAY_2)
        response = self.route(client, A, groups=[gid], interface="standard")
        self.assertEqual(self.results(response), ["verified", "offline"])
        self.assertFalse(response.get_json()["ok"], "a half-routed group is never a full success")
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], True))
        self.assertEqual(self.sim.writes(DISPLAY_2["ip"]), [])
        self.assertEqual(self.sim.held(DISPLAY_2["ip"]), ("", "", True))

    def test_a_member_that_comes_back_is_routed_the_next_time_without_a_rescan(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        self.go_offline(DISPLAY_2)
        self.route(client, A, groups=[gid], interface="standard")
        self.come_back(DISPLAY_2)
        response = self.route(client, B, groups=[gid], interface="standard")
        self.assertEqual(self.results(response), ["verified", "verified"])
        for display in (DISPLAY_1, DISPLAY_2):
            self.assertEqual(self.sim.held(display["ip"]), (VIDEO[B], AUDIO[B], True))

    def test_an_offline_source_writes_nothing_anywhere(self):
        client = self.make_user()
        self.go_offline(SOURCE_A)
        response = self.route(client, A, displays=[D1, D2], interface="standard")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.sim.writes(), [])

    def test_a_wall_with_an_offline_member_is_partial_and_never_writes_to_it(self):
        self.wall([DISPLAY_3, DISPLAY_4])
        client = self.make_user(destinations=(), walls=("w1",))
        self.go_offline(DISPLAY_4)
        response = self.route(client, C, walls=["w1"], interface="standard")
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["results"][0]["result"], "partial")
        self.assertEqual((body["results"][0]["routed"], body["results"][0]["total"]), (1, 2))
        self.assertEqual(self.sim.writes(DISPLAY_4["ip"]), [])
        self.assertEqual(self.sim.decoder(DISPLAY_3["ip"]).video, VIDEO[C])


    def test_a_wall_whose_every_member_is_offline_says_offline_and_writes_nothing(self):
        # The wall operation has two guards: its own, which names the reason, and the one inside the
        # per-cell write. With only some members offline either one gives the same "partial"; only
        # when every member is down do they differ, and the sentence the person reads is the first's.
        self.wall([DISPLAY_3, DISPLAY_4])
        client = self.make_user(destinations=(), walls=("w1",))
        self.go_offline(DISPLAY_3)
        self.go_offline(DISPLAY_4)
        body = self.route(client, C, walls=["w1"], interface="standard").get_json()
        self.assertFalse(body["ok"])
        self.assertEqual((body["results"][0]["result"], body["results"][0]["routed"]), ("offline", 0))
        self.assertEqual(self.sim.writes(DISPLAY_3["ip"]) + self.sim.writes(DISPLAY_4["ip"]), [])


class PermissionBetweenRoutesTests(Base):
    def test_a_grant_removed_between_two_routes_refuses_the_second_and_changes_nothing(self):
        client = self.make_user(destinations=(D1,))
        self.assertEqual(self.results(self.route(client, A, displays=[D1], interface="standard")), ["verified"])
        self.set_permissions(destinations=[])
        response = self.route(client, B, displays=[D1], interface="standard")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], True),
                         "the refused route left the display on what it was showing")

    def test_a_source_removed_between_routes_refuses_that_source_only(self):
        client = self.make_user(sources=(A, B), destinations=(D1,))
        self.route(client, B, displays=[D1], interface="standard")
        self.set_permissions(sources=[A], destinations=[D1])
        self.assertEqual(self.route(client, B, displays=[D1], interface="standard").status_code, 403)
        self.assertEqual(self.results(self.route(client, A, displays=[D1], interface="standard")), ["verified"])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], True))

    def test_a_grant_withdrawn_while_the_first_display_is_being_written_stops_the_second(self):
        client = self.make_user(destinations=(D1, D2))
        fired = []
        # The test is about order: the second display's authorization is judged after the
        # withdrawal that happened while the first was being written. A route dispatches its
        # displays in parallel, so with several workers the two begin together, and which one
        # reaches its write first is a matter of thread scheduling (about 1 run in 40 had the
        # second display already past its last check when the grant was withdrawn, on every
        # platform). One worker puts the two in sequence, which is the property under test;
        # the parallel dispatch itself is covered by test_route_parallel.
        with_one_worker = mock.patch.dict(os.environ, {"LUMA_ROUTE_WORKERS": "1"})
        with_one_worker.start()
        self.addCleanup(with_one_worker.stop)

        def withdraw(ip, method, params):
            if ip == DISPLAY_1["ip"] and method == "SelectVideoStream.Set" and not fired:
                fired.append(True)
                # The administrator's request arrives on its own thread, as it does in life. Issued
                # from this one it would share the route request's `g` and turn it into the local
                # operator's -- a harness artefact that once made this test pass a withdrawn grant.
                admin = threading.Thread(target=lambda: self.set_permissions(destinations=[D1]))
                admin.start()
                admin.join(30)

        self.sim.on_call = withdraw
        response = self.route(client, A, displays=[D1, D2], interface="standard")
        self.assertTrue(fired, "precondition: the grant really was withdrawn mid-route")
        self.assertEqual(self.results(response), ["verified", "refused"])
        self.assertEqual(self.sim.writes(DISPLAY_2["ip"]), [], "the second display was never written")
        self.assertEqual(self.sim.held(DISPLAY_2["ip"]), ("", "", True))

    def test_matrix_removed_while_open_refuses_the_next_matrix_route_and_keeps_standard(self):
        client = self.make_user(matrix=True)
        self.assertEqual(self.results(self.route(client, A, displays=[D1])), ["verified"])
        self.set_permissions(matrix_routing=False)
        refused = self.route(client, B, displays=[D1], interface="matrix")
        self.assertEqual((refused.status_code, refused.get_json()["reason"]), (403, "interface_not_permitted"))
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], True))
        self.assertEqual(self.results(self.route(client, B, displays=[D1], interface="standard")), ["verified"])

    def test_a_deleted_account_cannot_route_and_the_device_is_left_alone(self):
        client = self.make_user()
        self.route(client, A, displays=[D1], interface="standard")
        deleted = self.local.post("/api/admin/users/" + self.user_id(), json={"action": "delete"})
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        response = self.route(client, B, displays=[D1], interface="standard")
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], True))


class ConcurrencyTests(Base):
    def test_two_routes_to_one_decoder_never_overlap_and_never_leave_a_mixed_state(self):
        self.sim.write_pause = 0.03
        clients = [self.make_user(name="u%d" % i) for i in range(2)]
        statuses = []

        def go(client, source):
            for _ in range(6):
                statuses.append(self.route(client, source, displays=[D1], interface="standard").status_code)

        threads = [threading.Thread(target=go, args=(clients[0], A)),
                   threading.Thread(target=go, args=(clients[1], B))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual([s for s in statuses if s >= 500], [])
        self.assertLessEqual(self.sim.max_concurrent_writes.get(DISPLAY_1["ip"], 0), 1,
                             "two writes were in flight at one decoder at once")
        video, audio, follow = self.sim.held(DISPLAY_1["ip"])
        self.assertIn((video, audio), ((VIDEO[A], AUDIO[A]), (VIDEO[B], AUDIO[B])))
        self.assertTrue(self.sim.consistent(DISPLAY_1["ip"]))

    def test_a_hammer_of_routes_permission_edits_and_outages_leaves_a_consistent_lab(self):
        gid = self.group([D1, D2])
        self.wall([DISPLAY_3, DISPLAY_4])
        self.sim.write_pause = 0.004
        names = ["h%d" % i for i in range(4)]
        for name in names:
            self.make_user(name=name, groups=(gid,), walls=("w1",))
        statuses, errors = [], []
        rng = random.Random(20260514)
        plans = [[(rng.choice((A, B, C)), rng.choice(("av", "av", "video", "audio")),
                   rng.choice((([D1],), ([D2, D3],), ([D1, D2, D3, D4],)))[0],
                   rng.random() < 0.2) for _ in range(30)] for _ in names]

        def worker(name, plan):
            try:
                client = self.remote.sign_in(name, USER_PASSWORD)
                local = self.m.APP.test_client()
                for index, (source, mode, displays, flip) in enumerate(plan):
                    if flip:
                        target = rng_choice[index % 2]
                        local.post("/api/admin/users/" + self.user_id(name),
                                   json={"action": "set_permissions", "permissions": {
                                       "sources": [A, B, C], "groups": [gid], "walls": ["w1"],
                                       "destinations": [D1, D2, D3, D4] if target else [D1, D3, D4],
                                       "standard_routing": True, "matrix_routing": True}})
                    statuses.append(self.route(client, source, displays=displays, mode=mode).status_code)
                    if index % 7 == 3:
                        statuses.append(self.route(client, source, groups=[gid]).status_code)
                    if index % 11 == 5 and mode == "av":
                        statuses.append(self.route(client, source, walls=["w1"], interface="standard").status_code)
            except Exception as exc:                    # a worker that died is a failure, not silence
                errors.append(repr(exc))

        rng_choice = (True, False)
        threads = [threading.Thread(target=worker, args=(n, p)) for n, p in zip(names, plans)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
        self.assertEqual(errors, [])
        self.assertEqual([s for s in statuses if s >= 500], [], "a route answered with a server error")
        for display in DISPLAYS:
            self.assertLessEqual(self.sim.max_concurrent_writes.get(display["ip"], 0), 1, display["ip"])
            self.assertTrue(self.sim.consistent(display["ip"]), display["ip"])


class FailureInjectionTests(Base):
    def test_a_decoder_that_does_not_answer_is_a_controlled_failure_and_the_write_is_not_repeated(self):
        client = self.make_user()
        self.sim.down.add(DISPLAY_1["ip"])
        response = self.route(client, A, displays=[D1], interface="standard")
        # The display is read before it is written, so one that does not answer is found out
        # without a write ever being sent to it.
        self.assertEqual(self.results(response), ["unreachable"])
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.sim.writes(DISPLAY_1["ip"]), [], "nothing is written to a display that is silent")
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), ("", "", True))

    def test_a_source_that_does_not_answer_writes_nothing(self):
        client = self.make_user()
        self.sim.down.add(SOURCE_A["ip"])
        response = self.route(client, A, displays=[D1, D2], interface="standard")
        self.assertEqual(self.results(response), ["unreachable", "unreachable"])
        self.assertEqual(self.sim.writes(), [])

    def test_a_reply_that_is_not_a_reply_is_never_called_a_route(self):
        client = self.make_user()
        self.sim.garbage.add(DISPLAY_1["ip"])
        response = self.route(client, A, displays=[D1], interface="standard")
        self.assertFalse(response.get_json()["ok"])
        self.assertNotEqual(self.results(response), ["verified"])

    def test_an_error_reply_is_a_failure_with_a_message_not_a_500(self):
        client = self.make_user()
        self.sim.error.add(DISPLAY_1["ip"])
        response = self.route(client, A, displays=[D1], interface="standard")
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), ("", "", True))

    def test_a_slow_decoder_still_routes_and_is_verified(self):
        client = self.make_user()
        self.sim.slow[DISPLAY_1["ip"]] = 0.2
        started = time.monotonic()
        response = self.route(client, A, displays=[D1], interface="standard")
        self.assertEqual(self.results(response), ["verified"])
        self.assertLess(time.monotonic() - started, 10)

    def test_a_lost_reply_is_a_failure_and_the_next_refresh_makes_the_cache_agree_with_the_device(self):
        client = self.make_user()
        self.sim.lose_replies.add((DISPLAY_1["ip"], "SelectVideoStream.Set"))
        response = self.route(client, A, displays=[D1], interface="standard")
        self.assertEqual(self.results(response), ["failed"])
        self.assertEqual(len(self.sim.writes(DISPLAY_1["ip"])), 1, "the write is never retried blind")
        self.assertEqual(self.sim.decoder(DISPLAY_1["ip"]).video, VIDEO[A], "the write did arrive")
        self.m._refresh_matrix_state_core()
        self.assertEqual(self.app.unit(DISPLAY_1["ip"])["dec"]["video"]["streamname"], VIDEO[A],
                         "the cache follows the device, not the failed answer")

    def test_a_decoder_that_goes_quiet_part_way_through_a_group_is_one_failure_in_two(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        self.sim.fail_after[DISPLAY_2["ip"]] = 1
        response = self.route(client, A, groups=[gid], interface="standard")
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual((body["routed"], body["total"]), (1, 2))
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], True))

    def test_a_write_the_decoder_accepts_and_does_not_hold_is_never_reported_as_success(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        self.sim.decoder(DISPLAY_2["ip"]).ignore_video = True
        response = self.route(client, A, groups=[gid], interface="standard")
        self.assertEqual(self.results(response), ["verified", "not_verified"])
        self.assertFalse(response.get_json()["ok"])

    def test_a_wall_member_that_does_not_hold_its_matrix_makes_the_wall_partial(self):
        self.wall([DISPLAY_3, DISPLAY_4])
        client = self.make_user(destinations=(), walls=("w1",))
        self.sim.decoder(DISPLAY_4["ip"]).wall_ignore = True
        body = self.route(client, A, walls=["w1"], interface="standard").get_json()
        self.assertEqual(body["results"][0]["result"], "partial")
        self.assertFalse(body["ok"])

    def test_a_decoder_that_cannot_hear_the_requested_audio_is_refused_before_any_write(self):
        client = self.make_user()
        self.scanned_as(DISPLAY_1, A, A)
        self.sim.decoder(DISPLAY_1["ip"]).known_audio = [AUDIO[A]]
        response = self.route(client, B, displays=[D1], mode="audio")
        self.assertEqual(self.results(response), ["failed"])
        self.assertEqual(self.sim.writes(DISPLAY_1["ip"]), [])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], True))


class TransportFailureTests(unittest.TestCase):
    """The real `_ws_call` against a loopback unit that misbehaves in each way a unit can."""

    TIMEOUT = 0.4

    def setUp(self):
        import websocket._core as core
        self.app = RealApp(self)
        self.m = self.app.m
        self.sim = DeviceSim.lab(SOURCES, DISPLAYS)
        self.sim.decoder(DISPLAY_1["ip"]).video = ""
        self.app.seed()
        self._fenced_before = fence_attempts()
        self.guarded = []
        real = core.create_connection

        def loopback_only(url, *args, **kwargs):
            if not str(url).startswith("ws://127.0.0.1:"):
                self.guarded.append(url)
                raise OSError("the test refused a non-loopback WebSocket: %s" % url)
            return real(url, *args, **kwargs)

        self.units = {}
        self.app.patch("_ws_call_auth", REAL_AUTH)          # RealApp swaps in a model; these need the wire
        self.app.patch("create_connection", loopback_only)
        self.app.patch("_ws_url", lambda ip, path: "ws://127.0.0.1:%d%s" % (self.units[ip].port, path))
        self.m.CONFIG["TIMEOUT"] = self.TIMEOUT
        self.m.ROUTE_READBACK_ATTEMPTS = 1
        self.addCleanup(setattr, self.m, "ROUTE_READBACK_ATTEMPTS", 3)
        self.addCleanup(self._close)
        self.addCleanup(self._nothing_escaped)

    def _close(self):
        for fixture in self.units.values():
            fixture.close()

    def _nothing_escaped(self):
        self.assertEqual(self.guarded, [], "a connection to a non-loopback address was attempted")
        self.assertEqual(fence_attempts(), self._fenced_before)

    def lab(self, encoder_mode="ok", decoder_mode="ok", delay=0.0):
        self.units[SOURCE_A["ip"]] = WsFixture(self.sim, SOURCE_A["ip"], encoder_mode, delay)
        self.units[DISPLAY_1["ip"]] = WsFixture(self.sim, DISPLAY_1["ip"], decoder_mode, delay)

    def route(self):
        started = time.monotonic()
        result = self.m._route_av(DISPLAY_1["ip"], SOURCE_A["ip"])
        return result, time.monotonic() - started

    def test_precondition_a_healthy_unit_routes_through_the_real_transport(self):
        self.lab()
        result, _elapsed = self.route()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.sim.decoder(DISPLAY_1["ip"]).video, VIDEO[A])

    def test_every_way_a_source_can_misbehave_is_a_bounded_failure_that_writes_nothing(self):
        for mode in ("hang", "silent", "garbage", "list", "wrong_id", "error", "close", "slow"):
            with self.subTest(source=mode):
                self.sim.calls.clear()
                self.lab(encoder_mode=mode, delay=self.TIMEOUT * 3)
                result, elapsed = self.route()
                self.assertFalse(result["ok"], mode)
                self.assertIn(result["status"], ("unreachable", "failed"), mode)
                self.assertLess(elapsed, 6.0, "%s: the call was not bounded (%.1f s)" % (mode, elapsed))
                if mode in ("hang", "silent", "slow"):
                    self.assertEqual(self.units[SOURCE_A["ip"]].connections, 1,
                                     "%s: a unit that does not answer is asked once, not once per path" % mode)
                self.assertEqual(self.sim.writes(), [], mode)
                self._close()
                self.units.clear()

    def test_every_way_a_decoder_can_misbehave_is_a_bounded_failure_and_is_never_called_a_route(self):
        for mode in ("hang", "silent", "garbage", "list", "wrong_id", "error", "close", "slow"):
            with self.subTest(decoder=mode):
                self.sim.calls.clear()
                self.sim.decoder(DISPLAY_1["ip"]).video = ""
                self.units[SOURCE_A["ip"]] = WsFixture(self.sim, SOURCE_A["ip"], "ok")
                self.units[DISPLAY_1["ip"]] = WsFixture(self.sim, DISPLAY_1["ip"], mode, self.TIMEOUT * 3)
                result, elapsed = self.route()
                self.assertFalse(result["ok"], mode)
                self.assertLess(elapsed, 6.0, "%s: the call was not bounded (%.1f s)" % (mode, elapsed))
                if mode in ("hang", "silent", "slow"):
                    self.assertEqual(self.units[DISPLAY_1["ip"]].connections, 1,
                                     "%s: a unit that does not answer is asked once, not once per path" % mode)
                if mode == "slow":
                    # The unit received the write and answered after the deadline: the write is held,
                    # and the route is still not a success because nobody confirmed it. It was sent once.
                    self.assertLessEqual(len(self.sim.writes(DISPLAY_1["ip"])), 1, "a write is never repeated blind")
                else:
                    self.assertEqual(self.sim.decoder(DISPLAY_1["ip"]).video, "",
                                     "%s: nothing was written" % mode)
                self._close()
                self.units.clear()


if __name__ == "__main__":
    unittest.main()
