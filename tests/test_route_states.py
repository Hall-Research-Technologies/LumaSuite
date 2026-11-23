"""A route is verified, failed, or not yet confirmed -- and the server says which, per destination.

The page used to draw "the display did not confirm it yet" as a failure. A display can take longer than
the route's own read-back window to report a stream it has already taken, so the route is answered as
`not_verified` and the page must look again before it calls it anything. These hold:

  * `/api/user/route_multi` names each thing chosen -- by its position in the list the page sent, never
    by an identity -- as verified, unconfirmed or failed, and a group is the worst of its members;
  * `/api/user/route_check` is the later look: read-only (no write, no lock), authorised exactly as the
    route is, and it carries no identity or address;
  * a display that settles is confirmed by it, one that is still disagreeing stays unconfirmed;
  * multi-destination routing still deduplicates, still counts truthfully, still never writes to a display
    the account was not given, and a failed destination neither stops the rest nor undoes them.

Everything runs against `_devicesim.DeviceSim` with the hardware fence on.
"""
import re
import time
import unittest
from unittest import mock

import _support                      # installs the fence and isolates the data dir first
import luma_gate
from _support import DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4

from test_routing_torture import (A, B, C, D1, D2, D3, D4, VIDEO, Base)


IP = {D1: DISPLAY_1["ip"], D2: DISPLAY_2["ip"], D3: DISPLAY_3["ip"], D4: DISPLAY_4["ip"]}


class Fixture(Base):
    def multi(self, client, source, displays=(), groups=(), mode=None, interface="standard"):
        body = {"source": source, "displays": list(displays), "groups": list(groups),
                "walls": [], "interface": interface}
        if mode:
            body["mode"] = mode
        return self.remote.post(client, "/api/user/route_multi", body)

    def check(self, client, source, displays=(), groups=(), interface="standard"):
        return self.remote.post(client, "/api/user/route_check", {
            "source": source, "displays": list(displays), "groups": list(groups),
            "interface": interface})

    def held(self, display):
        return self.sim.decoders[IP[display]].video


class TargetStateTests(Fixture):
    def test_each_choice_is_named_by_position_with_its_own_state(self):
        gid = self.group([D3, D4])
        client = self.make_user(destinations=(D1, D2, D3, D4), groups=(gid,))
        body = self.multi(client, A, displays=[D2, D1], groups=[gid]).get_json()
        self.assertEqual(body["summary"], "4 of 4 displays routed")
        self.assertEqual(body["targets"], [
            {"kind": "display", "index": 0, "state": "verified"},
            {"kind": "display", "index": 1, "state": "verified"},
            {"kind": "group", "index": 0, "state": "verified"}])

    def test_a_display_chosen_twice_is_one_destination(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(D1, D2), groups=(gid,))
        body = self.multi(client, A, displays=[D1], groups=[gid]).get_json()
        self.assertEqual((body["routed"], body["total"]), (2, 2), "D1 is routed once, not twice")
        self.assertEqual(len([w for w in self.sim.write_log if w[1] == "SelectVideoStream.Set"
                              and w[0] == IP[D1]]) <= 1, True)

    def test_a_failed_destination_neither_stops_the_rest_nor_undoes_them(self):
        client = self.make_user()
        self.sim.down.add(IP[D2])
        response = self.multi(client, A, displays=[D1, D2, D3])
        body = response.get_json()
        self.assertEqual((body["routed"], body["total"]), (2, 3))
        self.assertEqual(body["summary"], "2 of 3 displays routed")
        self.assertEqual([t["state"] for t in body["targets"]], ["verified", "failed", "verified"])
        self.assertEqual(self.held(D1), VIDEO[A])
        self.assertEqual(self.held(D3), VIDEO[A])
        self.assertNotEqual(self.held(D2), VIDEO[A])

    def test_a_group_is_the_worst_of_its_members(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        self.sim.down.add(IP[D2])
        body = self.multi(client, A, groups=[gid]).get_json()
        self.assertEqual(body["targets"], [{"kind": "group", "index": 0, "state": "failed"}])
        self.assertEqual((body["routed"], body["total"]), (1, 2))

    def test_the_answer_carries_no_identity_and_no_address(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(D3,), groups=(gid,))
        text = self.multi(client, A, displays=[D3], groups=[gid]).get_data(as_text=True)
        self.assertIsNone(re.search(r"\d+\.\d+\.\d+\.\d+", text))
        self.assertNotIn("02:00:5e", text)
        self.assertNotIn(gid, text)

    def test_a_display_that_was_not_granted_is_never_written_to(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        mark = len(self.sim.write_log)
        refused = self.multi(client, A, displays=[D3], groups=[gid])
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(len(self.sim.write_log), mark, "a half-authorised request writes nothing")
        ok = self.multi(client, A, groups=[gid])
        self.assertEqual(ok.status_code, 200)
        written = {w[0] for w in self.sim.write_log[mark:] if w[1] == "SelectVideoStream.Set"}
        self.assertEqual(written, {IP[D1], IP[D2]}, "only the group's members were written to")
        self.assertNotEqual(self.held(D3), VIDEO[A])
        self.assertNotEqual(self.held(D4), VIDEO[A])


class SlowDisplayTests(Fixture):
    """A display that takes longer than the route's read-back window to report what it has taken."""

    SETTLE = 4.0

    def slow(self, display):
        self.sim.decoders[IP[display]].settle = self.SETTLE

    def test_not_confirmed_yet_is_unconfirmed_not_failed(self):
        self.slow(D1)
        client = self.make_user()
        began = time.monotonic()
        body = self.multi(client, A, displays=[D1, D2]).get_json()
        self.assertEqual([t["state"] for t in body["targets"]], ["unconfirmed", "verified"])
        self.assertEqual(body["results"][0]["result"], "not_verified")
        self.assertEqual((body["routed"], body["total"]), (1, 2), "counted as not routed until it is confirmed")
        self.assertLess(time.monotonic() - began, self.SETTLE, "the route did not wait out the display")

    def test_a_group_with_a_slow_member_is_unconfirmed(self):
        gid = self.group([D1, D2])
        self.slow(D2)
        client = self.make_user(destinations=(), groups=(gid,))
        body = self.multi(client, A, groups=[gid]).get_json()
        self.assertEqual(body["targets"], [{"kind": "group", "index": 0, "state": "unconfirmed"}])

    def test_the_look_is_read_only_and_confirms_once_the_display_settles(self):
        self.slow(D1)
        client = self.make_user()
        self.multi(client, A, displays=[D1])
        writes = len(self.sim.write_log)
        early = self.check(client, A, displays=[D1]).get_json()
        self.assertEqual(early["targets"], [{"kind": "display", "index": 0, "state": "unconfirmed"}])
        self.assertFalse(early["ok"])
        time.sleep(self.SETTLE)
        late = self.check(client, A, displays=[D1]).get_json()
        self.assertEqual(late["targets"], [{"kind": "display", "index": 0, "state": "verified"}])
        self.assertTrue(late["ok"])
        self.assertEqual(len(self.sim.write_log), writes, "looking again wrote nothing")
        self.assertEqual(self.sim.violations, [])

    def test_the_look_confirms_a_group_by_its_members(self):
        gid = self.group([D1, D2])
        self.slow(D2)
        client = self.make_user(destinations=(), groups=(gid,))
        self.multi(client, A, groups=[gid])
        time.sleep(self.SETTLE)
        late = self.check(client, A, groups=[gid]).get_json()
        self.assertEqual(late["targets"], [{"kind": "group", "index": 0, "state": "verified"}])

    def test_a_display_that_never_took_the_route_is_not_confirmed_by_looking(self):
        client = self.make_user()
        self.sim.decoders[IP[D1]].ignore_video = True            # accepts the write and holds nothing
        body = self.multi(client, A, displays=[D1]).get_json()
        self.assertIn(body["targets"][0]["state"], ("unconfirmed", "failed"))
        later = self.check(client, A, displays=[D1]).get_json()
        self.assertNotEqual(later["targets"][0]["state"], "verified")

    def test_an_audio_only_route_has_nothing_to_look_at_again(self):
        self.slow(D1)
        client = self.make_user()
        body = self.multi(client, A, displays=[D1], mode="audio", interface="matrix").get_json()
        for target in body["targets"]:
            self.assertNotEqual(target["state"], "unconfirmed")


class RouteCheckAuthorisationTests(Fixture):
    def test_it_needs_exactly_the_grants_a_route_needs(self):
        gid = self.group([D1, D2])
        client = self.make_user(sources=(A,), destinations=(D1,), groups=())
        self.assertEqual(self.check(client, A, displays=[D1]).status_code, 200)
        self.assertEqual(self.check(client, B, displays=[D1]).status_code, 403, "a source it may not use")
        self.assertEqual(self.check(client, A, displays=[D2]).status_code, 403, "a display it may not control")
        self.assertEqual(self.check(client, A, groups=[gid]).status_code, 403, "a group it was not given")

    def test_holding_a_group_does_not_allow_looking_at_its_members_individually(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(), groups=(gid,))
        self.assertEqual(self.check(client, A, displays=[D1]).status_code, 403)
        self.assertEqual(self.check(client, A, groups=[gid]).status_code, 200)

    def test_an_interface_the_account_lacks_is_refused(self):
        client = self.make_user(standard=True, matrix=False)
        self.assertEqual(self.check(client, A, displays=[D1], interface="matrix").status_code, 403)
        self.assertEqual(self.check(client, A, displays=[D1], interface="standard").status_code, 200)

    def test_a_malformed_request_is_a_400_and_reads_nothing(self):
        client = self.make_user()
        mark = len(self.sim.call_log)
        for body in ({}, {"source": A}, {"source": A, "displays": "x"}, {"source": A, "displays": [D1], "interface": "admin"},
                     {"source": A, "displays": [], "groups": []}):
            self.assertEqual(self.remote.post(client, "/api/user/route_check", body).status_code, 400, body)
        self.assertEqual(len(self.sim.call_log), mark)

    def test_it_never_writes_and_never_names_a_device(self):
        gid = self.group([D1, D2])
        client = self.make_user(destinations=(D3,), groups=(gid,))
        mark = len(self.sim.write_log)
        text = self.check(client, A, displays=[D3], groups=[gid]).get_data(as_text=True)
        self.assertEqual(len(self.sim.write_log), mark)
        self.assertIsNone(re.search(r"\d+\.\d+\.\d+\.\d+", text))
        self.assertNotIn("02:00:5e", text)

    def test_an_offline_display_is_reported_not_read(self):
        client = self.make_user()
        self.go_offline(self.unit(DISPLAY_1["mac"]))
        body = self.check(client, A, displays=[D1]).get_json()
        self.assertEqual(body["targets"][0]["state"], "failed")




class RouteCheckSecondLineTests(Fixture):
    """The handler re-checks every grant the gate checks. The gate refuses first, so reaching the
    handler's own check means taking the gate out of the way for the span of the test (and saying
    first that the gate would have refused, so the test measures the handler and not the gate)."""

    def around_the_gate(self, client_name="alex"):
        account = next(a for a in self.store._accounts.values() if a.username == client_name)
        patcher = mock.patch.object(luma_gate, "evaluate",
                                    lambda *a, **k: luma_gate.Decision(True, 200, "permitted", account))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_handler_refuses_what_the_account_was_not_given(self):
        gid = self.group([D1, D2])
        other = self.group([D3])
        client = self.make_user(sources=(A,), destinations=(D1,), groups=(gid,))
        self.around_the_gate()
        cases = (({"source": B, "displays": [D1], "groups": []}, "source_not_permitted"),
                 ({"source": A, "displays": [D3], "groups": []}, "destination_not_permitted"),
                 ({"source": A, "displays": [], "groups": [other]}, "group_not_permitted"),
                 ({"source": A, "displays": [D2], "groups": [gid]}, "destination_not_permitted"))
        for payload, reason in cases:
            payload = dict(payload, interface="standard")
            response = self.remote.post(client, "/api/user/route_check", payload)
            self.assertEqual((response.status_code, response.get_json().get("reason")), (403, reason), payload)

    def test_the_handler_refuses_an_interface_the_account_lacks(self):
        client = self.make_user(standard=True, matrix=False)
        self.around_the_gate()
        response = self.remote.post(client, "/api/user/route_check", {
            "source": A, "displays": [D1], "groups": [], "interface": "matrix"})
        self.assertEqual((response.status_code, response.get_json().get("reason")),
                         (403, "interface_not_permitted"))


if __name__ == "__main__":
    unittest.main()
