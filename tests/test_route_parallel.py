"""Multi-display routing is one bounded parallel batch, and a wall and a toggle ride on it.

Three defects, one engine (`luma_remote_api._run_batch`):

  MA-005  a source routed to several displays (Standard multi-destination, Display Group, saved
          Video Wall) used to switch them visibly one after another. The destinations are now
          resolved and authorised first, then written in a small bounded parallel batch.
  MA-007  a saved wall row in the User Matrix supports A/V, Video and Audio exactly as the selected
          mode says, member by member, resolved on the server, authorised by the wall grant.
  MA-008  "Audio follows video" for Standard Routing (default on): a Standard route sends both
          planes; with it off a Standard route is video only. The Matrix is never affected.

Everything runs against `_devicesim.DeviceSim`, which holds state, and the hardware fence is on.
The start spread -- how far apart the first and last write BEGIN -- is measured from the model's
own write log, with a realistic per-write latency, not from a recorder inside the code under test.

Run `python tests/test_route_parallel.py --measure` to print the spread for the current tree.
"""
import copy
import json
import re
import sys
import threading
import time
import unittest

import _support                      # installs the fence and isolates the data dir first
from _support import DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4

from test_routing_torture import (A, B, C, D1, D2, D3, D4, VIDEO, AUDIO, Base)

import luma_groups
import luma_locks
import lumaserver

WRITE_LATENCY = 0.25            # seconds per device write: the middle of the 150-400 ms measured


def spread(sim):
    """First/last write start and completion, in ms from the first write start."""
    log = sorted(sim.write_log, key=lambda row: row[2])
    first = log[0][2]
    starts = [(row[2] - first) * 1000 for row in log]
    ends = [(row[3] - first) * 1000 for row in log]
    return {"writes": len(log), "first_start_ms": round(min(starts)), "last_start_ms": round(max(starts)),
            "first_end_ms": round(min(ends)), "last_end_ms": round(max(ends))}


def most_in_flight(sim):
    """The most device writes that were being waited on at the same moment."""
    edges = []
    for _ip, _m, began, ended in sim.write_log:
        edges.append((began, 1))
        edges.append((ended, -1))
    now = peak = 0
    for _t, step in sorted(edges, key=lambda e: (e[0], e[1])):
        now += step
        peak = max(peak, now)
    return peak


class Fixture(Base):
    def setUp(self):
        super().setUp()
        self.sim.write_pause = WRITE_LATENCY
        self.addCleanup(lambda: setattr(self.sim, "write_pause", 0.0))

    def ok_rows(self, response):
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        return body["results"]

    def standard(self, client, source, displays=(), groups=(), walls=(), **extra):
        body = {"source": source, "displays": list(displays), "groups": list(groups),
                "walls": list(walls), "interface": "standard"}
        body.update(extra)
        return self.remote.post(client, "/api/user/route_multi", body)

    def two_four(self, d, video, audio, follow):
        d.video, d.audio, d.follow = VIDEO[video] if video else "", AUDIO[audio] if audio else "", follow


class StartSpreadTests(Fixture):
    """MA-005: the displays change together."""

    def test_four_displays_start_their_writes_together_not_in_a_cascade(self):
        client = self.make_user()
        response = self.standard(client, A, displays=[D1, D2, D3, D4])
        self.assertEqual([r["result"] for r in self.ok_rows(response)], ["verified"] * 4)
        s = spread(self.sim)
        self.assertEqual(s["writes"], 4)
        # Sequential would put the last start at three write latencies (750 ms) or more.
        self.assertLess(s["last_start_ms"], WRITE_LATENCY * 1000 * 0.8, s)
        self.assertEqual(most_in_flight(self.sim), 4, "all four writes were in flight together")

    def test_a_group_starts_its_members_together(self):
        gid = self.group([D1, D2, D3, D4])
        client = self.make_user(destinations=(), groups=(gid,))
        response = self.standard(client, A, groups=[gid])
        self.assertEqual([r["result"] for r in self.ok_rows(response)], ["verified"] * 4)
        self.assertLess(spread(self.sim)["last_start_ms"], WRITE_LATENCY * 1000 * 0.8)

    def test_the_group_endpoint_and_the_multi_endpoint_are_one_engine(self):
        gid = self.group([D1, D2, D3, D4])
        client = self.make_user(destinations=(), groups=(gid,))
        response = self.remote.post(client, "/api/user/route_group", {"source": A, "group": gid})
        body = response.get_json()
        self.assertEqual((response.status_code, body["routed"], body["total"]), (200, 4, 4), body)
        self.assertLess(spread(self.sim)["last_start_ms"], WRITE_LATENCY * 1000 * 0.8)
        self.assertEqual(most_in_flight(self.sim), 4)

    def test_a_saved_wall_starts_every_member_together(self):
        self.wall([DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4])
        client = self.make_user(destinations=(), walls=("w1",))
        response = self.standard(client, A, walls=["w1"])
        self.assertEqual([r["result"] for r in self.ok_rows(response)], ["verified"])
        decoder_writes = [row for row in self.sim.write_log if row[1] == "SelectVideoStream.Set"]
        starts = [row[2] for row in decoder_writes]
        self.assertEqual(len(starts), 4)
        self.assertLess((max(starts) - min(starts)) * 1000, WRITE_LATENCY * 1000 * 0.8)

    def test_a_display_a_group_and_a_wall_together_share_one_batch(self):
        gid = self.group([D1, D2])
        self.wall([DISPLAY_3, DISPLAY_4])
        client = self.make_user(destinations=(), groups=(gid,), walls=("w1",))
        response = self.standard(client, A, groups=[gid], walls=["w1"])
        self.assertEqual([r["result"] for r in self.ok_rows(response)], ["verified"] * 3)
        video = [row[2] for row in self.sim.write_log if row[1] == "SelectVideoStream.Set"]
        self.assertEqual(len(video), 4)
        self.assertLess((max(video) - min(video)) * 1000, WRITE_LATENCY * 1000 * 0.8)

    def test_concurrency_is_bounded(self):
        luma_groups_bound = 2
        import os
        os.environ["LUMA_ROUTE_WORKERS"] = str(luma_groups_bound)
        self.addCleanup(os.environ.pop, "LUMA_ROUTE_WORKERS", None)
        client = self.make_user()
        response = self.standard(client, A, displays=[D1, D2, D3, D4])
        self.assertEqual([r["result"] for r in self.ok_rows(response)], ["verified"] * 4)
        self.assertEqual(most_in_flight(self.sim), luma_groups_bound)
        self.assertEqual(response.get_json()["timing"]["workers"], luma_groups_bound)

    def test_the_bound_is_clamped_and_never_unbounded(self):
        import os
        for wanted, expect in (("1000", luma_groups.MAX_DISPATCH_WORKERS), ("0", 1), ("-3", 1),
                               ("many", luma_groups.DISPATCH_WORKERS)):
            os.environ["LUMA_ROUTE_WORKERS"] = wanted
            self.assertEqual(luma_groups.dispatch_workers(), expect, wanted)
        os.environ.pop("LUMA_ROUTE_WORKERS", None)
        self.assertLessEqual(luma_groups.DISPATCH_WORKERS, 8)

    def test_run_parallel_never_has_more_calls_in_flight_than_its_bound(self):
        live, peak, lock = [0], [0], threading.Lock()

        def one(index, entry):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.01)
            with lock:
                live[0] -= 1
            return {"ok": True, "n": entry}

        summary = luma_groups.run_parallel(list(range(40)), one, workers=5)
        self.assertEqual(peak[0], 5)
        self.assertEqual([r["n"] for r in summary["results"]], list(range(40)), "entry order")
        self.assertEqual((summary["routed"], summary["total"]), (40, 40))

    def test_results_stay_in_the_order_chosen_whatever_order_they_finish(self):
        self.sim.slow[DISPLAY_1["ip"]] = 0.4        # the first display is the slowest to answer
        client = self.make_user()
        response = self.standard(client, A, displays=[D1, D2, D3])
        names = [r["name"] for r in self.ok_rows(response)]
        self.assertEqual(names, ["Lobby One", "Lobby Two", "Lobby Three"])

    def test_the_answer_carries_timings_and_nothing_secret(self):
        client = self.make_user()
        response = self.standard(client, A, displays=[D1, D2])
        body = response.get_json()
        timing = body["timing"]
        for key in ("first_start_ms", "last_start_ms", "first_end_ms", "last_end_ms", "start_spread_ms"):
            self.assertIsInstance(timing[key], (int, float), key)
        text = json.dumps(body)
        for forbidden in (DISPLAY_1["ip"], DISPLAY_1["mac"], "password", "stream-alpha"):
            self.assertNotIn(forbidden, text)

    def test_one_engine_serves_every_multi_destination_route(self):
        with open("luma_remote_api.py", encoding="utf-8") as handle:
            source = handle.read()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("run_sequential", code, "a handler still sequences destinations itself")
        self.assertEqual(len(re.findall(r"_run_batch\(\"/api/user/", code)), 2)
        self.assertEqual(len(re.findall(r"luma_groups\.run_parallel\(", code)), 1)


class BatchSemanticsTests(Fixture):
    """Authorisation, offline, failure and busy are per destination, and unchanged."""

    def test_one_ungranted_display_refuses_the_whole_request_before_any_write(self):
        client = self.make_user(destinations=(D1, D2))
        response = self.standard(client, A, displays=[D1, D2, D3])
        self.assertEqual((response.status_code, response.get_json()["reason"]),
                         (403, "destination_not_permitted"))
        self.assertEqual(self.sim.writes(), [])

    def test_an_offline_display_is_reported_and_never_written_and_the_rest_route(self):
        client = self.make_user()
        self.go_offline(DISPLAY_2)
        response = self.standard(client, A, displays=[D1, D2, D3])
        self.assertEqual([r["result"] for r in self.ok_rows(response)], ["verified", "offline", "verified"])
        self.assertEqual(self.sim.writes(DISPLAY_2["ip"]), [])
        self.assertFalse(response.get_json()["ok"])

    def test_a_display_that_does_not_hold_the_write_fails_alone_and_nothing_is_undone(self):
        client = self.make_user()
        self.sim.decoder(DISPLAY_2["ip"]).ignore_video = True
        response = self.standard(client, A, displays=[D1, D2, D3])
        results = [r["result"] for r in self.ok_rows(response)]
        self.assertEqual((results[0], results[2]), ("verified", "verified"))
        self.assertNotEqual(results[1], "verified")
        self.assertEqual(self.sim.held(DISPLAY_1["ip"])[0], VIDEO[A], "a success was not rolled back")
        self.assertEqual(self.sim.held(DISPLAY_3["ip"])[0], VIDEO[A])

    def test_a_busy_display_is_named_busy_and_the_others_are_not_held_up(self):
        client = self.make_user()
        self.system.locks._timeout = 0.3
        self.addCleanup(setattr, self.system.locks, "_timeout", luma_locks.DEFAULT_TIMEOUT_SECONDS)
        with self.system.locks.hold(luma_locks.ResourceLocks.decoder(D2)):
            response = self.standard(client, A, displays=[D1, D2, D3])
        results = [r["result"] for r in self.ok_rows(response)]
        self.assertEqual(results, ["verified", "busy", "verified"])
        self.assertEqual(self.sim.writes(DISPLAY_2["ip"]), [])

    def test_permission_withdrawn_during_the_lock_wait_stops_that_write(self):
        """Authorisation is checked again on the far side of the lock, inside the parallel batch."""
        client = self.make_user()
        self.system.locks._timeout = 5
        self.addCleanup(setattr, self.system.locks, "_timeout", luma_locks.DEFAULT_TIMEOUT_SECONDS)
        answer = {}
        hold = self.system.locks.hold(luma_locks.ResourceLocks.decoder(D2))
        hold.__enter__()
        try:
            def go():
                answer["r"] = self.standard(client, A, displays=[D1, D2, D3])
            thread = threading.Thread(target=go)
            thread.start()
            deadline = time.time() + 5
            while time.time() < deadline and not self.sim.write_log:
                time.sleep(0.01)                     # the other displays are being written; D2 is waiting
            self.assertTrue(self.sim.write_log, "precondition: the batch started")
            revoked = self.set_permissions(destinations=[D1, D3])
            self.assertEqual(revoked.status_code, 200, revoked.get_json())
        finally:
            hold.__exit__(None, None, None)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        results = [r["result"] for r in answer["r"].get_json()["results"]]
        self.assertEqual(results[1], "refused", results)
        self.assertEqual(self.sim.writes(DISPLAY_2["ip"]), [], "nothing was written once permission went")

    def test_locks_are_taken_one_display_at_a_time_so_overlapping_requests_cannot_deadlock(self):
        client = self.make_user()
        answers = []

        def go(order):
            answers.append(self.standard(client, A, displays=order).status_code)
        threads = [threading.Thread(target=go, args=(o,)) for o in
                   ([D1, D2, D3, D4], [D4, D3, D2, D1], [D2, D4, D1, D3], [D3, D1, D4, D2])]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertFalse([t for t in threads if t.is_alive()], "a request deadlocked")
        self.assertEqual(answers, [200] * 4)


class WallBreakawayTests(Fixture):
    """MA-007: a saved wall row routes A/V, Video or Audio exactly as the mode says."""

    def setUp(self):
        super().setUp()
        self.sim.write_pause = 0.0
        self.wall([DISPLAY_1, DISPLAY_2])
        self.client = self.make_user(destinations=(), walls=("w1",))

    def put(self, display, video, audio, follow):
        self.scanned_as(display, video, audio)
        d = self.sim.decoder(display["ip"])
        d.follow = follow

    def wall_route(self, source, mode):
        return self.route(self.client, source, walls=["w1"], mode=mode)

    def test_av_routes_both_planes_to_every_member_from_a_mixed_start(self):
        self.put(DISPLAY_1, B, B, True)               # following
        self.put(DISPLAY_2, B, C, False)              # a breakaway
        body = self.wall_route(A, "av").get_json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["results"][0]["result"], "verified")
        for display in (DISPLAY_1, DISPLAY_2):
            video, audio, _follow = self.sim.held(display["ip"])
            self.assertEqual((video, audio), (VIDEO[A], AUDIO[A]), display["hostname"])
            self.assertTrue(self.sim.decoder(display["ip"]).wall["vmenable"])

    def test_video_changes_the_video_only_and_keeps_each_members_audio(self):
        self.put(DISPLAY_1, B, B, True)
        self.put(DISPLAY_2, B, C, False)
        body = self.wall_route(A, "video").get_json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.sim.held(DISPLAY_1["ip"])[:2], (VIDEO[A], AUDIO[B]))
        self.assertEqual(self.sim.held(DISPLAY_2["ip"])[:2], (VIDEO[A], AUDIO[C]))
        for display in (DISPLAY_1, DISPLAY_2):
            self.assertTrue(self.sim.decoder(display["ip"]).wall["vmenable"], "still a wall member")

    def test_audio_changes_the_audio_only_and_keeps_each_members_video(self):
        self.put(DISPLAY_1, B, B, True)
        self.put(DISPLAY_2, C, C, False)
        before = {d["ip"]: dict(self.sim.decoder(d["ip"]).wall) for d in (DISPLAY_1, DISPLAY_2)}
        body = self.wall_route(A, "audio").get_json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.sim.held(DISPLAY_1["ip"])[:2], (VIDEO[B], AUDIO[A]))
        self.assertEqual(self.sim.held(DISPLAY_2["ip"])[:2], (VIDEO[C], AUDIO[A]))
        methods = [m for _i, m in self.sim.writes()]
        self.assertNotIn("SelectVideoStream.Set", methods)
        self.assertNotIn("StandardVideoWallInfo.Set", methods, "an audio route leaves the wall as it is")
        for display in (DISPLAY_1, DISPLAY_2):
            self.assertEqual(self.sim.decoder(display["ip"]).wall, before[display["ip"]])

    def test_an_offline_member_is_skipped_and_the_wall_says_partial(self):
        self.go_offline(DISPLAY_2)
        for mode in ("av", "video", "audio"):
            self.sim.calls.clear()
            self.sim.write_log.clear()
            body = self.wall_route(A, mode).get_json()
            row = body["results"][0]
            self.assertEqual((row["result"], row["routed"], row["total"]), ("partial", 1, 2), mode)
            self.assertFalse(body["ok"])
            self.assertEqual(self.sim.writes(DISPLAY_2["ip"]), [], mode)

    def test_a_member_that_fails_does_not_stop_the_others(self):
        self.put(DISPLAY_1, B, B, True)
        self.put(DISPLAY_2, B, B, True)
        self.sim.decoder(DISPLAY_1["ip"]).ignore_video = True
        body = self.wall_route(A, "video").get_json()
        row = body["results"][0]
        self.assertEqual((row["result"], row["routed"], row["total"]), ("partial", 1, 2))
        self.assertEqual(self.sim.held(DISPLAY_2["ip"])[0], VIDEO[A])

    def test_every_member_failing_is_not_a_success_and_names_why(self):
        self.go_offline(DISPLAY_1)
        self.go_offline(DISPLAY_2)
        row = self.wall_route(A, "audio").get_json()["results"][0]
        self.assertEqual((row["ok"], row["result"], row["routed"]), (False, "offline", 0))

    def test_the_wall_grant_is_the_authority_members_need_no_grant_of_their_own(self):
        client = self.make_user("wallonly", destinations=(), walls=("w1",))
        for mode in ("av", "video", "audio"):
            response = self.route(client, A, walls=["w1"], mode=mode)
            self.assertEqual(response.status_code, 200, (mode, response.get_json()))
        direct = self.route(client, A, displays=[D1], mode="av")
        self.assertEqual((direct.status_code, direct.get_json()["reason"]),
                         (403, "destination_not_permitted"), "routing the wall granted no member")

    def test_a_user_without_the_wall_grant_is_refused_in_every_mode_and_nothing_is_written(self):
        client = self.make_user("nowall", destinations=(D1, D2), walls=())
        for mode in ("av", "video", "audio"):
            response = self.route(client, A, walls=["w1"], mode=mode)
            self.assertEqual((response.status_code, response.get_json()["reason"]),
                             (403, "wall_not_permitted"), mode)
        self.assertEqual(self.sim.writes(), [])

    def test_membership_is_resolved_on_the_server_a_request_cannot_name_members(self):
        client = self.make_user("wallonly", destinations=(), walls=("w1",))
        body = {"source": A, "displays": [], "walls": ["w1"], "members": [D3], "interface": "matrix",
                "mode": "video"}
        response = self.remote.post(client, "/api/user/route_multi", body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sim.writes(DISPLAY_3["ip"]), [], "a decoder outside the wall was never reached")

    def test_a_busy_wall_member_is_not_written_and_the_others_still_route(self):
        self.put(DISPLAY_1, B, B, True)
        self.put(DISPLAY_2, B, B, True)
        self.system.locks._timeout = 0.3
        self.addCleanup(setattr, self.system.locks, "_timeout", luma_locks.DEFAULT_TIMEOUT_SECONDS)
        with self.system.locks.hold(luma_locks.ResourceLocks.decoder(D2)):
            body = self.wall_route(A, "video").get_json()
        row = body["results"][0]
        self.assertEqual((row["result"], row["routed"], row["total"]), ("partial", 1, 2))
        self.assertEqual(self.sim.writes(DISPLAY_2["ip"]), [], "a member held by another operator is not written")
        self.assertEqual(self.sim.held(DISPLAY_1["ip"])[0], VIDEO[A])

    def test_a_wall_row_offers_all_three_modes(self):
        row = self.row(self.matrix(self.client), "w1")
        self.assertEqual(row["modes"], ["av", "video", "audio"])

    def test_audio_only_does_not_rewrite_the_saved_wall_source(self):
        self.wall_route(A, "audio")
        saved = lumaserver._load_video_wall_state()["walls"][0]
        self.assertEqual(saved.get("source_id", ""), "")
        self.wall_route(A, "video")
        saved = lumaserver._load_video_wall_state()["walls"][0]
        self.assertEqual(saved["source_id"], lumaserver._vw_device_id(self.unit("02:00:5e:10:00:0a")))


class AudioFollowsVideoTests(Fixture):
    """MA-008: a Standard preference, never a permission, never a Matrix change."""

    def setUp(self):
        super().setUp()
        self.sim.write_pause = 0.0

    def left(self, display, video, audio, follow):
        self.scanned_as(display, video, audio)
        self.sim.decoder(display["ip"]).follow = follow

    def test_the_default_is_on_and_sends_both_planes(self):
        client = self.make_user()
        self.left(DISPLAY_1, B, C, False)
        self.assertEqual(self.standard(client, A, displays=[D1]).status_code, 200)
        self.assertEqual(self.sim.held(DISPLAY_1["ip"])[:2], (VIDEO[A], AUDIO[A]))

    def test_on_sends_both_planes_to_every_kind_of_destination(self):
        gid = self.group([D1, D2])
        self.wall([DISPLAY_3, DISPLAY_4])
        client = self.make_user(destinations=(D1, D2), groups=(gid,), walls=("w1",))
        for display in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4):
            self.left(display, B, C, False)
        response = self.standard(client, A, displays=[D1], groups=[gid], walls=["w1"],
                                 audio_follows_video=True)
        self.assertEqual(response.status_code, 200, response.get_json())
        for display in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4):
            self.assertEqual(self.sim.held(display["ip"])[:2], (VIDEO[A], AUDIO[A]), display["hostname"])

    def test_off_changes_the_video_only_everywhere(self):
        gid = self.group([D1, D2])
        self.wall([DISPLAY_3, DISPLAY_4])
        client = self.make_user(destinations=(D1, D2), groups=(gid,), walls=("w1",))
        for display in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4):
            self.left(display, B, C, False)
        response = self.standard(client, A, displays=[D1], groups=[gid], walls=["w1"],
                                 audio_follows_video=False)
        self.assertEqual(response.status_code, 200, response.get_json())
        for display in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4):
            self.assertEqual(self.sim.held(display["ip"])[:2], (VIDEO[A], AUDIO[C]), display["hostname"])

    def test_off_keeps_the_audio_of_a_display_that_was_following(self):
        client = self.make_user()
        self.left(DISPLAY_1, B, B, True)
        self.standard(client, A, displays=[D1], audio_follows_video=False)
        self.assertEqual(self.sim.held(DISPLAY_1["ip"])[:2], (VIDEO[A], AUDIO[B]))

    def test_the_single_and_group_endpoints_honour_it_too(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(D1, D2), groups=(gid,))
        self.left(DISPLAY_1, B, C, False)
        self.left(DISPLAY_2, B, C, False)
        single = self.remote.post(client, "/api/user/route", {"source": A, "destination": D1,
                                                              "audio_follows_video": False})
        self.assertEqual(single.status_code, 200, single.get_json())
        self.assertEqual(self.sim.held(DISPLAY_1["ip"])[:2], (VIDEO[A], AUDIO[C]))
        group = self.remote.post(client, "/api/user/route_group",
                                 {"source": A, "group": gid, "audio_follows_video": False})
        self.assertEqual(group.status_code, 200, group.get_json())
        self.assertEqual(self.sim.held(DISPLAY_2["ip"])[:2], (VIDEO[A], AUDIO[C]))
        self.assertFalse(group.get_json()["results"][0].get("reason"))

    def test_a_value_that_is_not_a_boolean_is_refused_and_writes_nothing(self):
        client = self.make_user()
        for bad in ("false", 0, None, [], "off"):
            response = self.standard(client, A, displays=[D1], audio_follows_video=bad)
            self.assertEqual((response.status_code, response.get_json()["reason"]), (400, "bad_preference"), bad)
        self.assertEqual(self.sim.writes(), [])

    def test_the_matrix_ignores_it_so_matrix_modes_are_unaffected(self):
        client = self.make_user()
        for flag in (True, False):
            self.left(DISPLAY_1, B, C, False)
            self.route(client, A, displays=[D1], mode="video", audio_follows_video=flag) \
                if False else self.remote.post(client, "/api/user/route_multi", {
                    "source": A, "displays": [D1], "interface": "matrix", "mode": "video",
                    "audio_follows_video": flag})
            self.assertEqual(self.sim.held(DISPLAY_1["ip"])[:2], (VIDEO[A], AUDIO[C]),
                             "Matrix Video is video only whatever the toggle says (%s)" % flag)
            self.left(DISPLAY_1, B, C, False)
            self.remote.post(client, "/api/user/route_multi", {
                "source": A, "displays": [D1], "interface": "matrix", "mode": "av",
                "audio_follows_video": flag})
            self.assertEqual(self.sim.held(DISPLAY_1["ip"])[:2], (VIDEO[A], AUDIO[A]),
                             "Matrix A/V is both planes whatever the toggle says (%s)" % flag)
            self.left(DISPLAY_1, B, B, True)
            self.remote.post(client, "/api/user/route_multi", {
                "source": A, "displays": [D1], "interface": "matrix", "mode": "audio",
                "audio_follows_video": flag})
            self.assertEqual(self.sim.held(DISPLAY_1["ip"])[:2], (VIDEO[B], AUDIO[A]))

    def test_it_is_a_preference_not_a_permission(self):
        import luma_users
        with open("luma_users.py", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("audio_follows", source, "no grant, field or interface for it")
        client = self.make_user("standard-only", matrix=False)
        self.assertEqual(self.standard(client, A, displays=[D1], audio_follows_video=False).status_code, 200)
        self.assertEqual(self.standard(client, A, displays=[D1], audio_follows_video=True).status_code, 200)
        self.assertNotIn("audio_follows", json.dumps(luma_users.Permissions((A,), (D1,)).to_json()))

    def test_both_planes_are_prepared_per_display_not_all_video_then_all_audio(self):
        self.sim.write_pause = WRITE_LATENCY
        client = self.make_user()
        for display in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4):
            self.left(display, B, C, False)       # a breakaway: audio and video both need a write
        self.standard(client, A, displays=[D1, D2, D3, D4])
        log = self.sim.write_log
        self.assertEqual(len(log), 8)
        for display in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4):
            mine = [row for row in log if row[0] == display["ip"]]
            self.assertEqual([row[1] for row in mine], ["SelectAudioStream.Set", "SelectVideoStream.Set"])
        first_video_start = min(row[2] for row in log if row[1] == "SelectVideoStream.Set")
        last_audio_start = max(row[2] for row in log if row[1] == "SelectAudioStream.Set")
        self.assertLess(last_audio_start, first_video_start + WRITE_LATENCY * 0.5,
                        "every display's audio began before any display's video finished: one pass each")
        # Parallel starts all land within about one write latency; a serial route puts the last
        # start at three latencies or more (750 ms here), so two latencies still separates the two
        # cleanly. 1.5 latencies (375 ms) failed on the two-core macOS Intel runner at 376 ms and
        # 389 ms, a scheduling margin rather than serialisation; the ordering assertion above is
        # unchanged and is what proves audio and video are prepared per display.
        self.assertLess(spread(self.sim)["last_start_ms"], WRITE_LATENCY * 1000 * 2.0)


def measure():
    """Print the start spread for a four-display route on this tree. Not a test."""
    class Measure(Fixture):
        def runTest(self):
            client = self.make_user()
            self.sim.write_pause = WRITE_LATENCY
            began = time.monotonic()
            response = self.standard(client, A, displays=[D1, D2, D3, D4])
            took = round((time.monotonic() - began) * 1000)
            result = spread(self.sim)
            result["request_ms"] = took
            result["status"] = response.status_code
            print(json.dumps(result))
    unittest.TextTestRunner(verbosity=0).run(Measure())


if __name__ == "__main__":
    if "--measure" in sys.argv:
        sys.argv.remove("--measure")
        measure()
    else:
        unittest.main()
