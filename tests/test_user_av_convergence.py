"""A restricted User's A/V route: one implementation, both planes, and breakaway preserved.

Decision (NC-2): A/V means *both* Video and Audio go to the chosen source explicitly. Video and
Audio modes change only their own plane and keep the other. Standard Routing, a drop and the User
Matrix all reach the same backend function, so they cannot disagree about what a route does.

Every scenario drives the real routes against the state-holding device model
(`_devicesim.DeviceSim`) and asserts what the *decoder holds* afterwards and which writes it was
sent, not what the HTTP answer said.
"""
import copy
import inspect
import re
import unittest

import _support                      # installs the fence and isolates the data dir first
from _support import DISPLAY_1, DISPLAY_2, DISPLAY_3

from test_routing_torture import (A, B, C, D1, D2, D3, VIDEO, AUDIO, Base)

import lumaserver
import luma_breakaway

SINGLE = "/api/user/route"


def strip_comments(source):
    return "\n".join(re.sub(r"\s#.*$", "", line) for line in source.splitlines()
                     if not line.lstrip().startswith("#"))


class Surfaces(Base):
    """The three ways a person can ask for the same thing."""

    def standard_single(self, client, source, display):
        return self.remote.post(client, SINGLE, {"source": source, "destination": display})

    def standard_multi(self, client, source, display):
        return self.route(client, source, displays=[display], interface="standard")

    def matrix_av(self, client, source, display):
        return self.route(client, source, displays=[display], interface="matrix", mode="av")

    SURFACES = ("standard_single", "standard_multi", "matrix_av")

    def left_by_a_breakaway(self, display, video, audio):
        """The decoder as a breakaway leaves it: video on one source, audio on another, not following."""
        self.scanned_as(display, video, audio)
        d = self.sim.decoder(display["ip"])
        d.video, d.audio, d.follow = VIDEO[video], AUDIO[audio], False

    def ok(self, response):
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body["ok"], body)
        return body


class ConvergedAVTests(Surfaces):
    def test_every_surface_leaves_both_planes_on_the_source_with_the_same_writes(self):
        client = self.make_user()
        seen = {}
        for name in self.SURFACES:
            self.left_by_a_breakaway(DISPLAY_1, B, C)
            self.sim.calls.clear()
            self.ok(getattr(self, name)(client, A, D1))
            self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], False), name)
            seen[name] = self.sim.writes(DISPLAY_1["ip"])
        self.assertEqual(seen["standard_single"], seen["standard_multi"])
        self.assertEqual(seen["standard_multi"], seen["matrix_av"])
        self.assertEqual([m for _i, m in seen["matrix_av"]],
                         ["SelectAudioStream.Set", "SelectVideoStream.Set"],
                         "audio first, so a failure leaves the video where it was; two writes, no more")

    def test_the_three_surfaces_are_one_function(self):
        # The single route and the one batch engine (which the group route and the multi route both
        # use) each call the one `route_av` they were built with, and the application builds it from
        # `_route_av_for_users` and nothing else.
        with open("luma_remote_api.py", encoding="utf-8") as handle:
            api = strip_comments(handle.read())
        self.assertEqual(len(re.findall(r"outcome = route_av\(decoder_ip, encoder_ip\)", api)), 2)
        self.assertNotIn("_route_av(", api, "a surface calls the plain route directly")
        server = strip_comments(inspect.getsource(lumaserver))
        self.assertEqual(len(re.findall(r"route_av=_route_av_for_users", server)), 1)

    def test_a_display_that_follows_video_gets_one_video_write_and_keeps_following(self):
        client = self.make_user()
        for name in self.SURFACES:
            self.scanned_as(DISPLAY_1, B, B)
            self.sim.calls.clear()
            body = self.ok(getattr(self, name)(client, A, D1))
            self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], True), name)
            self.assertEqual([m for _i, m in self.sim.writes(DISPLAY_1["ip"])],
                             ["SelectVideoStream.Set"], name)
            if "results" in body:
                self.assertEqual(body["results"][0]["audio"], "follows")

    def test_a_route_that_is_already_true_writes_nothing(self):
        client = self.make_user()
        self.scanned_as(DISPLAY_1, A, A)
        self.sim.calls.clear()
        self.ok(self.standard_single(client, A, D1))
        self.assertEqual(self.sim.writes(), [])
        self.left_by_a_breakaway(DISPLAY_2, A, A)
        self.sim.decoder(DISPLAY_2["ip"]).follow = False        # explicit, and already both on A
        self.sim.calls.clear()
        self.ok(self.standard_multi(client, A, D2))
        self.assertEqual(self.sim.writes(), [], "both planes are already on the source")

    def test_when_only_the_audio_is_elsewhere_only_the_audio_is_written(self):
        client = self.make_user()
        self.left_by_a_breakaway(DISPLAY_1, A, C)               # right video, wrong audio
        self.sim.calls.clear()
        self.ok(self.matrix_av(client, A, D1))
        self.assertEqual([m for _i, m in self.sim.writes(DISPLAY_1["ip"])], ["SelectAudioStream.Set"])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], False))

    def test_the_matrix_shows_a_route_with_nothing_of_the_breakaway_left(self):
        client = self.make_user()
        self.left_by_a_breakaway(DISPLAY_1, B, C)
        self.ok(self.standard_single(client, A, D1))
        row = self.row(self.matrix(client), D1)
        self.assertEqual((row["cells"][A]["video"], row["cells"][A]["audio"]), ("all", "all"))
        for other in (B, C):
            self.assertEqual((row["cells"][other]["video"], row["cells"][other]["audio"]),
                             ("none", "none"), other)


class TheCacheAgreesWithTheDeviceTests(Surfaces):
    """After a route the cache holds what the display reported, never what it held before.

    A User's A/V is carried by the breakaway engine, which writes the cache from its own
    verified read-back; a source with no audio falls back to the plain verified route,
    which records the audio the display reports. Each path is reached here with a cache
    that is deliberately WRONG first, so a path that stopped recording leaves the wrong
    value in place and the test says so (a cache that already agreed would prove nothing).
    """

    def cached_audio(self, display):
        with self.m.cache_lock:
            return dict(self.m.cache_units[display["ip"]]["dec"]["audio"])

    def make_cache_wrong(self, display, name, follows):
        with self.m.cache_lock:
            held = self.m.cache_units[display["ip"]]["dec"]["audio"]
            held["streamname"], held["followvideoenable"] = name, follows

    def test_the_engine_records_the_audio_the_display_now_holds(self):
        client = self.make_user()
        self.left_by_a_breakaway(DISPLAY_1, B, C)                # device: audio on C, not following
        self.make_cache_wrong(DISPLAY_1, AUDIO[B], True)
        self.ok(self.standard_single(client, A, D1))
        held = self.cached_audio(DISPLAY_1)
        self.assertEqual(held["streamname"], AUDIO[A], "the cache kept the audio from before the route")

    def test_the_engine_records_the_follow_flag_the_display_reports(self):
        client = self.make_user()
        self.scanned_as(DISPLAY_1, B, B)                         # device: following
        self.make_cache_wrong(DISPLAY_1, AUDIO[B], False)        # the cache believes it is not
        self.ok(self.standard_single(client, A, D1))
        self.assertIs(self.cached_audio(DISPLAY_1)["followvideoenable"], True)

    def test_the_fallback_records_the_audio_name_the_display_reports(self):
        client = self.make_user()
        self.sim.encoders[_support.SOURCE_A["ip"]].audio = ""    # no audio to bring: plain video route
        self.left_by_a_breakaway(DISPLAY_1, B, C)
        self.make_cache_wrong(DISPLAY_1, AUDIO[B], True)
        self.sim.calls.clear()
        self.ok(self.standard_single(client, A, D1))
        self.assertEqual([m for _i, m in self.sim.writes(DISPLAY_1["ip"])], ["SelectVideoStream.Set"],
                         "precondition: this is the fallback, not the engine")
        held = self.cached_audio(DISPLAY_1)
        self.assertEqual(held["streamname"], AUDIO[C], "the cache kept the audio a scan once saw")

    def test_the_fallback_records_the_follow_flag_the_display_reports(self):
        client = self.make_user()
        self.sim.encoders[_support.SOURCE_A["ip"]].audio = ""
        self.left_by_a_breakaway(DISPLAY_1, B, C)                # device: not following
        self.make_cache_wrong(DISPLAY_1, AUDIO[C], True)         # the cache believes it follows
        self.sim.calls.clear()
        self.ok(self.standard_single(client, A, D1))
        self.assertEqual([m for _i, m in self.sim.writes(DISPLAY_1["ip"])], ["SelectVideoStream.Set"],
                         "precondition: this is the fallback, not the engine")
        self.assertIs(self.cached_audio(DISPLAY_1)["followvideoenable"], False)


class BreakawayIsPreservedTests(Surfaces):
    """Video and Audio modes change one plane and keep the other, in both directions."""

    def test_video_only_keeps_whatever_audio_the_display_had_in_both_follow_states(self):
        client = self.make_user()
        for following in (True, False):
            self.scanned_as(DISPLAY_1, A, A)
            self.sim.decoder(DISPLAY_1["ip"]).follow = following
            self.ok(self.route(client, B, displays=[D1], mode="video"))
            self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[A], False), following)

    def test_audio_only_keeps_whatever_video_the_display_had(self):
        client = self.make_user()
        for following in (True, False):
            self.scanned_as(DISPLAY_1, A, A)
            self.sim.decoder(DISPLAY_1["ip"]).follow = following
            self.ok(self.route(client, C, displays=[D1], mode="audio"))
            self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[C], False), following)

    def test_a_breakaway_made_of_two_modes_then_av_then_each_plane_alone(self):
        client = self.make_user()
        self.scanned_as(DISPLAY_1, A, A)
        self.ok(self.route(client, B, displays=[D1], mode="video"))
        self.ok(self.route(client, C, displays=[D1], mode="audio"))
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[C], False))
        self.ok(self.standard_single(client, A, D1))                  # A/V: both planes to A
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], False))
        self.ok(self.route(client, C, displays=[D1], mode="audio"))   # audio alone: video stays on A
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[C], False))
        self.ok(self.route(client, B, displays=[D1], mode="video"))   # video alone: audio stays on C
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[C], False))

    def test_the_administrator_can_not_ask_for_the_users_mode(self):
        # `user_av` is not an administrator mode: the endpoint validates against the closed list.
        self.assertNotIn(luma_breakaway.MODE_USER_AV, luma_breakaway.MODES)
        response = self.local.post("/api/route_matrix", json={
            "decoder_ip": DISPLAY_1["ip"], "encoder_ip": _support.SOURCE_A["ip"],
            "mode": luma_breakaway.MODE_USER_AV})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.sim.writes(), [])


class AuthorizationUnchangedTests(Surfaces):
    def test_an_ungranted_source_or_display_is_refused_on_every_surface_and_nothing_is_written(self):
        client = self.make_user(sources=(A,), destinations=(D1,))
        for name, source, display in (("standard_single", B, D1), ("standard_single", A, D2),
                                      ("standard_multi", B, D1), ("matrix_av", A, D2)):
            response = getattr(self, name)(client, source, display)
            self.assertEqual(response.status_code, 403, (name, source, display))
        self.assertEqual(self.sim.writes(), [])

    def test_the_interface_grants_still_decide_which_surface_may_ask(self):
        matrix_only = self.make_user("matrixonly", standard=False, matrix=True)
        self.assertEqual(self.standard_single(matrix_only, A, D1).status_code, 403)
        self.assertEqual(self.standard_multi(matrix_only, A, D1).status_code, 403)
        self.ok(self.matrix_av(matrix_only, A, D1))
        standard_only = self.make_user("standardonly", standard=True, matrix=False)
        self.assertEqual(self.matrix_av(standard_only, A, D2).status_code, 403)
        self.ok(self.standard_single(standard_only, A, D2))

    def test_a_grant_withdrawn_before_the_route_means_no_write(self):
        client = self.make_user()
        self.set_permissions(destinations=[D2], sources=[A, B, C])
        self.assertEqual(self.standard_single(client, A, D1).status_code, 403)
        self.assertEqual(self.sim.writes(DISPLAY_1["ip"]), [])


class GroupsOfflineAndPartialFailureTests(Surfaces):
    def test_a_group_brings_both_planes_to_every_member_whatever_each_was_left_with(self):
        gid = self.group([D1, D2, D3])
        client = self.make_user(destinations=(), groups=(gid,))
        self.left_by_a_breakaway(DISPLAY_1, B, C)               # explicit, elsewhere
        self.scanned_as(DISPLAY_2, B, B)                        # following
        self.left_by_a_breakaway(DISPLAY_3, A, C)               # video already right
        body = self.ok(self.route(client, A, groups=[gid], interface="standard"))
        self.assertEqual([r["result"] for r in body["results"]], ["verified"] * 3)
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], False))
        self.assertEqual(self.sim.held(DISPLAY_2["ip"]), (VIDEO[A], AUDIO[A], True))
        self.assertEqual(self.sim.held(DISPLAY_3["ip"]), (VIDEO[A], AUDIO[A], False))

    def test_an_offline_member_is_never_written_and_the_rest_are_routed(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        self.left_by_a_breakaway(DISPLAY_1, B, C)
        self.left_by_a_breakaway(DISPLAY_2, B, C)
        self.go_offline(DISPLAY_2)
        response = self.route(client, A, groups=[gid], interface="standard")
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(sorted(r["result"] for r in response.get_json()["results"]),
                         ["offline", "verified"])
        self.assertEqual(self.sim.writes(DISPLAY_2["ip"]), [], "an offline member is not written to")
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], False))
        self.assertEqual(self.sim.held(DISPLAY_2["ip"]), (VIDEO[B], AUDIO[C], False), "left as it was")

    def test_a_member_that_does_not_hold_the_audio_is_a_failure_and_is_put_back(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        self.left_by_a_breakaway(DISPLAY_1, B, C)
        self.left_by_a_breakaway(DISPLAY_2, B, C)
        self.sim.decoder(DISPLAY_2["ip"]).ignore_audio = True    # accepts the write, holds nothing
        response = self.route(client, A, groups=[gid], interface="standard")
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(sorted(r["result"] for r in body["results"]), ["not_verified", "verified"])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[A], AUDIO[A], False))
        self.assertEqual(self.sim.held(DISPLAY_2["ip"]), (VIDEO[B], AUDIO[C], False),
                         "the member that could not take it is where it was: never half-routed")
        self.assertEqual(body["routed"], 1)

    def test_a_silent_display_is_found_out_before_any_write(self):
        client = self.make_user()
        self.left_by_a_breakaway(DISPLAY_1, B, C)
        self.sim.down.add(DISPLAY_1["ip"])
        response = self.standard_single(client, A, D1)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.sim.writes(DISPLAY_1["ip"]), [])

    def test_a_saved_wall_is_still_routed_by_the_wall_operation_and_verified_per_member(self):
        self.wall([DISPLAY_2, DISPLAY_3])
        client = self.make_user(destinations=(), walls=("w1",))
        body = self.ok(self.route(client, C, walls=["w1"], interface="standard"))
        self.assertEqual(body["results"][0]["result"], "verified")
        for display in (DISPLAY_2, DISPLAY_3):
            self.assertTrue(self.sim.decoder(display["ip"]).wall["vmenable"])
            self.assertEqual(self.sim.decoder(display["ip"]).video, VIDEO[C])


class WhatThePlanesCanDoTests(Surfaces):
    def test_a_source_with_no_audio_stream_is_routed_as_video_and_the_audio_is_reported(self):
        client = self.make_user()
        self.sim.encoders[_support.SOURCE_A["ip"]].audio = ""
        self.left_by_a_breakaway(DISPLAY_1, B, C)
        self.sim.calls.clear()
        body = self.ok(self.standard_multi(client, A, D1))
        self.assertEqual(self.sim.held(DISPLAY_1["ip"])[0], VIDEO[A])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"])[1], AUDIO[C], "there was no audio to bring")
        self.assertEqual(body["results"][0]["audio"], "not_following", "said aloud, not assumed")
        self.assertEqual([m for _i, m in self.sim.writes(DISPLAY_1["ip"])], ["SelectVideoStream.Set"])

    def test_audio_the_display_cannot_receive_is_refused_before_anything_is_written(self):
        client = self.make_user()
        self.left_by_a_breakaway(DISPLAY_1, B, C)
        self.sim.decoder(DISPLAY_1["ip"]).known_audio = [AUDIO[C]]    # does not know A's audio
        self.sim.calls.clear()
        response = self.standard_single(client, A, D1)
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "audio_not_available")
        self.assertIn("audio is not available", body["error"])
        self.assertEqual(self.sim.writes(), [])
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[C], False))

    def test_a_display_that_follows_is_not_asked_which_audio_streams_it_knows(self):
        client = self.make_user()
        self.scanned_as(DISPLAY_1, B, B)
        self.sim.decoder(DISPLAY_1["ip"]).known_audio = []             # irrelevant to a following display
        self.sim.calls.clear()
        self.ok(self.standard_single(client, A, D1))
        self.assertNotIn("AudioStreamNameList.Get", self.sim.methods(DISPLAY_1["ip"]))

    def test_a_video_write_that_is_not_held_restores_the_audio_it_moved(self):
        client = self.make_user()
        self.left_by_a_breakaway(DISPLAY_1, B, C)
        self.sim.decoder(DISPLAY_1["ip"]).ignore_video = True
        response = self.standard_single(client, A, D1)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[C], False),
                         "audio was written first and is put back when the video does not hold")

    def test_a_switcher_keeps_the_video_only_route(self):
        client = self.make_user()
        with self.m.cache_lock:
            self.m.cache_units[DISPLAY_1["ip"]]["type"] = "switcher"
        self.sim.calls.clear()
        self.standard_single(client, A, D1)
        self.assertNotIn("SelectAudioStream.Set", self.sim.methods(DISPLAY_1["ip"]))


SENTENCE = "That source's audio is not available to this display."


class TheSentenceReachesEverySurfaceTests(Surfaces):
    """Audio a display cannot receive is refused before any write, and the person is told why.

    The sentence used to exist on the single route only. A drop, a multi-select and the Matrix all
    go through `/api/user/route_multi`, which dropped the reason, so the page said "Did not work".
    """

    def setUp(self):
        super().setUp()
        self.client = self.make_user()
        self.left_by_a_breakaway(DISPLAY_1, B, C)
        self.sim.decoder(DISPLAY_1["ip"]).known_audio = [AUDIO[C]]       # does not know A's audio
        self.sim.calls.clear()

    def row_of(self, response):
        body = response.get_json()
        self.assertIn("results", body, body)
        self.assertEqual(len(body["results"]), 1, body)
        self.assertFalse(body["ok"], body)
        return body["results"][0]

    def assert_nothing_written(self):
        self.assertEqual(self.sim.writes(), [], "the engine declined before writing")
        self.assertEqual(self.sim.held(DISPLAY_1["ip"]), (VIDEO[B], AUDIO[C], False))

    def test_the_single_route_says_it(self):
        body = self.standard_single(self.client, A, D1).get_json()
        self.assertEqual(body["reason"], "audio_not_available")
        self.assertEqual(body["error"], SENTENCE)
        self.assert_nothing_written()

    def test_a_drop_or_multi_select_says_it_in_the_row(self):
        row = self.row_of(self.standard_multi(self.client, A, D1))
        self.assertEqual(row["reason"], "audio_not_available")
        self.assertEqual(row["message"], SENTENCE)
        self.assertEqual((row["ok"], row["result"]), (False, "failed"))
        self.assert_nothing_written()

    def test_the_matrix_says_it_in_the_row(self):
        row = self.row_of(self.matrix_av(self.client, A, D1))
        self.assertEqual(row["reason"], "audio_not_available")
        self.assertEqual(row["message"], SENTENCE)
        self.assert_nothing_written()

    def test_a_group_chosen_in_a_multi_select_says_it_for_the_member_that_declined(self):
        gid = self.group([D1])
        client = self.make_user("groupie", destinations=(), groups=(gid,))
        body = self.route(client, A, groups=[gid], interface="standard").get_json()
        row = body["results"][0]
        self.assertFalse(row["ok"])
        self.assertEqual((row["reason"], row["message"]), ("audio_not_available", SENTENCE))
        self.assert_nothing_written()

    def test_the_group_route_itself_says_it_for_the_member_that_declined(self):
        gid = self.group([D1])
        client = self.make_user("groupie", destinations=(), groups=(gid,))
        response = self.remote.post(client, "/api/user/route_group", {"source": A, "group": gid})
        row = response.get_json()["results"][0]
        self.assertFalse(row["ok"])
        self.assertEqual((row["reason"], row["message"]), ("audio_not_available", SENTENCE))
        self.assert_nothing_written()

    def test_all_surfaces_use_the_same_sentence(self):
        said = {
            "single": self.standard_single(self.client, A, D1).get_json()["error"],
            "multi": self.row_of(self.standard_multi(self.client, A, D1))["message"],
            "matrix": self.row_of(self.matrix_av(self.client, A, D1))["message"],
        }
        self.assertEqual(set(said.values()), {SENTENCE}, said)

    def test_only_the_one_that_declined_carries_it_when_several_displays_are_asked(self):
        self.scanned_as(DISPLAY_2, B, B)                                  # follows: nothing to decline
        body = self.route(self.client, A, displays=[D1, D2], interface="standard").get_json()
        by_name = {r["name"]: r for r in body["results"]}
        declined = [r for r in body["results"] if not r["ok"]]
        self.assertEqual(len(declined), 1, body)
        self.assertEqual(declined[0]["message"], SENTENCE)
        for r in body["results"]:
            if r["ok"]:
                self.assertNotIn("message", r)
                self.assertNotIn("reason", r)
        self.assertEqual(len(by_name), 2)

    def test_a_row_for_any_other_failure_carries_no_message(self):
        self.sim.decoder(DISPLAY_1["ip"]).known_audio = None
        self.sim.decoder(DISPLAY_1["ip"]).ignore_video = True              # accepted and not held
        row = self.row_of(self.standard_multi(self.client, A, D1))
        self.assertNotIn("message", row)
        self.assertNotIn("reason", row)

    def test_the_response_never_carries_an_address_a_stream_name_or_the_engines_own_words(self):
        text = self.standard_multi(self.client, A, D1).get_data(as_text=True)
        for marker in (DISPLAY_1["ip"], "198.51.100", "192.0.2", VIDEO[A], AUDIO[A], AUDIO[C], A, D1):
            self.assertNotIn(marker, text, marker)

    def test_a_row_that_succeeded_keeps_the_original_key_set(self):
        self.scanned_as(DISPLAY_1, B, B)
        self.sim.decoder(DISPLAY_1["ip"]).known_audio = None
        body = self.ok(self.standard_multi(self.client, A, D1))
        self.assertEqual(set(body["results"][0]), {"index", "name", "ok", "result", "audio"})


if __name__ == "__main__":
    unittest.main()
