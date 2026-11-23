"""Unroute for a restricted User: destinations only, one or many, displays and Display Groups.

It must be authorised in full before anything is written, resolve groups to members now, put each
physical display into the unrouted state once, never touch a display that was not chosen, carry on
past a failure, and report what really became of each one.
"""
import unittest
from unittest import mock
from types import SimpleNamespace

import _support
from _support import USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4, System, identity

import luma_access
import luma_gate

D1, D2, D3, D4 = (identity(u) for u in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4))
IP = {D1: DISPLAY_1["ip"], D2: DISPLAY_2["ip"], D3: DISPLAY_3["ip"], D4: DISPLAY_4["ip"]}
UNROUTE = "/api/user/unroute"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        self.g1 = self.s.remote.groups.create("Group One", [D1, D2])["group"]
        self.g2 = self.s.remote.groups.create("Group Two", [D2, D3])["group"]

    def signed_in(self, name="alex", **grants):
        self.s.user(name, **grants)
        return self.s.sign_in(name, USER_PASSWORD)

    def unroute(self, client, displays=(), groups=(), **extra):
        body = {"displays": list(displays), "groups": list(groups)}
        body.update(extra)
        return self.s.post(client, UNROUTE, body)

    def written(self):
        return list(self.s.unrouter.calls)


class SelectionTests(Fixture):
    def test_one_display_is_unrouted_and_needs_no_source(self):
        client = self.signed_in(destinations=[D1])
        response = self.unroute(client, [D1])
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.written(), [IP[D1]])
        body = response.get_json()
        self.assertEqual((body["ok"], body["unrouted"], body["total"]), (True, 1, 1))
        self.assertEqual(self.s.router.calls, [], "unroute never routes")

    def test_several_displays_and_a_group_are_one_request_each_physical_display_once(self):
        client = self.signed_in(destinations=[D3, D4], groups=[self.g1["id"]])
        body = self.unroute(client, [D3, D4, D3], [self.g1["id"]]).get_json()
        self.assertEqual(sorted(self.written()), sorted([IP[D1], IP[D2], IP[D3], IP[D4]]))
        self.assertEqual((body["unrouted"], body["total"]), (4, 4))

    def test_two_groups_sharing_a_member_unroute_that_member_once(self):
        client = self.signed_in(groups=[self.g1["id"], self.g2["id"]])
        body = self.unroute(client, [], [self.g1["id"], self.g2["id"]]).get_json()
        self.assertEqual(sorted(self.written()), sorted([IP[D1], IP[D2], IP[D3]]))
        self.assertEqual(body["total"], 3)

    def test_a_display_chosen_and_also_in_a_chosen_group_is_written_once(self):
        client = self.signed_in(destinations=[D1], groups=[self.g1["id"]])
        self.unroute(client, [D1], [self.g1["id"]])
        self.assertEqual(sorted(self.written()), sorted([IP[D1], IP[D2]]))

    def test_nothing_chosen_is_refused_and_nothing_written(self):
        client = self.signed_in(destinations=[D1])
        self.assertEqual(self.unroute(client).status_code, 400)
        self.assertEqual(self.written(), [])

    def test_a_video_wall_is_refused(self):
        client = self.signed_in(destinations=[D1])
        response = self.unroute(client, [D1], walls=["wall-x"])
        self.assertIn(response.status_code, (400, 403))
        self.assertEqual(self.written(), [])


class PermissionTests(Fixture):
    def test_a_non_member_display_is_never_touched(self):
        client = self.signed_in(groups=[self.g1["id"]])
        self.unroute(client, [], [self.g1["id"]])
        self.assertNotIn(IP[D3], self.written())
        self.assertNotIn(IP[D4], self.written())

    def test_one_ungranted_display_refuses_the_whole_request_before_any_write(self):
        client = self.signed_in(destinations=[D1, D2])
        response = self.unroute(client, [D1, D2, D3])
        self.assertEqual((response.status_code, response.get_json()["reason"]),
                         (403, "destination_not_permitted"))
        self.assertEqual(self.written(), [])

    def test_a_group_member_is_not_a_display_the_account_may_pick_alone(self):
        client = self.signed_in(groups=[self.g1["id"]])
        self.assertEqual(self.unroute(client, [D1]).status_code, 403)
        self.assertEqual(self.written(), [])

    def test_an_ungranted_group_is_refused(self):
        client = self.signed_in(destinations=[D1, D2])
        response = self.unroute(client, [], [self.g1["id"]])
        self.assertEqual((response.status_code, response.get_json()["reason"]),
                         (403, "group_not_permitted"))
        self.assertEqual(self.written(), [])

    def test_the_route_is_classified_for_users_and_exists_in_the_route_table(self):
        self.assertEqual(luma_access.ROUTES[UNROUTE], luma_access.Access.USER)
        self.assertIn(UNROUTE, {rule.rule for rule in self.s.app.url_map.iter_rules()})


class IndependenceTests(Fixture):
    def test_a_failed_display_does_not_stop_the_others_and_is_reported_truthfully(self):
        self.s.unrouter.fail[IP[D2]] = "not_verified"
        client = self.signed_in(destinations=[D1, D2, D3])
        body = self.unroute(client, [D1, D2, D3]).get_json()
        self.assertEqual(sorted(self.written()), sorted([IP[D1], IP[D2], IP[D3]]))
        self.assertFalse(body["ok"])
        self.assertEqual((body["unrouted"], body["total"]), (2, 3))
        states = {t["index"]: t["state"] for t in body["targets"] if t["kind"] == "display"}
        self.assertEqual(states, {0: "verified", 1: "failed", 2: "verified"},
                         "an unconfirmed unroute is a failure here, never 'unconfirmed'")

    def test_the_response_carries_no_address_or_identity(self):
        client = self.signed_in(destinations=[D1])
        text = self.unroute(client, [D1]).get_data(as_text=True)
        self.assertNotIn(DISPLAY_1["ip"], text)
        self.assertNotIn(D1, text)


class HandlerSecondLineTests(Fixture):
    """The handler re-checks what the gate checks, reached by taking the gate out of the way."""

    def test_the_handler_refuses_what_the_account_was_not_given(self):
        client = self.signed_in(destinations=[D1], groups=[self.g1["id"]])
        account = next(a for a in self.s.store._accounts.values() if a.username == "alex")
        original = luma_gate.evaluate
        patcher = mock.patch.object(luma_gate, "evaluate",
                                    lambda *a, **k: luma_gate.Decision(True, 200, "permitted", account))
        patcher.start()
        self.addCleanup(patcher.stop)
        cases = (({"displays": [D3], "groups": []}, "destination_not_permitted"),
                 ({"displays": [], "groups": [self.g2["id"]]}, "group_not_permitted"),
                 ({"displays": [D2], "groups": [self.g1["id"]]}, "destination_not_permitted"))
        store = SimpleNamespace(resolve_session=lambda token: (account, SimpleNamespace(
            csrf_token="c", from_direct_link=False)))
        environ = {"luma.listener": "remote", "HTTP_COOKIE": "lumasuite_session=x", "HTTP_X_LUMASUITE_CSRF": "c"}
        for payload, reason in cases:
            decision = original(UNROUTE, "POST", environ, payload, store)
            self.assertFalse(decision.allowed, "the gate would have admitted this: the test measures nothing")
            response = self.s.post(client, UNROUTE, payload)
            self.assertEqual((response.status_code, response.get_json()["reason"]), (403, reason), payload)
        self.assertEqual(self.written(), [])


class OfflineTests(Fixture):
    def test_an_offline_display_is_never_written_to_and_is_reported_as_offline(self):
        from test_user_walls import go_offline
        go_offline(self.s, DISPLAY_2["mac"])
        client = self.signed_in(destinations=[D1, D2])
        body = self.unroute(client, [D1, D2]).get_json()
        self.assertEqual(self.written(), [IP[D1]], "an offline display is never written to")
        self.assertFalse(body["ok"])
        self.assertEqual((body["unrouted"], body["total"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
