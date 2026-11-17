"""One source routed to several displays and groups, and the preview counters.

The route is a restricted User's: it must be authorised in full before anything is written,
resolve destinations by hardware identity, route each physical display once, one after
another, carry on past a failure, and never undo what already worked.
"""
import os
import threading
import time
import unittest
from unittest import mock

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, System, identity)

import luma_access
import luma_gate
import luma_previewstats
import luma_users

A, B = identity(SOURCE_A), identity(SOURCE_B)
D1, D2, D3, D4 = (identity(u) for u in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4))
IP = {D1: DISPLAY_1["ip"], D2: DISPLAY_2["ip"], D3: DISPLAY_3["ip"], D4: DISPLAY_4["ip"]}
ROUTE = "/api/user/route_multi"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        # Group One holds D1 and D2; Group Two holds D2 and D3.
        self.g1 = self.s.remote.groups.create("Group One", [D1, D2])["group"]
        self.g2 = self.s.remote.groups.create("Group Two", [D2, D3])["group"]

    def signed_in(self, name="alex", **grants):
        self.s.user(name, **grants)
        return self.s.sign_in(name, USER_PASSWORD)

    def multi(self, client, source, displays=(), groups=(), **extra):
        body = {"source": source, "displays": list(displays), "groups": list(groups)}
        body.update(extra)
        return self.s.post(client, ROUTE, body)

    def routed_ips(self):
        return [decoder for decoder, _encoder in self.s.router.calls]


class DestinationResolutionTests(Fixture):
    def test_a_display_chosen_twice_and_inside_a_chosen_group_is_routed_once(self):
        client = self.signed_in(sources=[A], destinations=[D1], groups=[self.g1["id"]])
        response = self.multi(client, A, [D1, D1], [self.g1["id"], self.g1["id"]])
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(sorted(self.routed_ips()), sorted([IP[D1], IP[D2]]))
        body = response.get_json()
        self.assertEqual((body["routed"], body["total"]), (2, 2))

    def test_two_groups_that_share_a_member_route_that_member_once(self):
        client = self.signed_in(sources=[A], groups=[self.g1["id"], self.g2["id"]])
        body = self.multi(client, A, [], [self.g1["id"], self.g2["id"]]).get_json()
        self.assertEqual(sorted(self.routed_ips()), sorted([IP[D1], IP[D2], IP[D3]]))
        self.assertEqual(body["total"], 3)

    def test_displays_are_routed_first_in_the_order_chosen_then_group_members(self):
        client = self.signed_in(sources=[A], destinations=[D4, D3], groups=[self.g1["id"]])
        self.multi(client, A, [D4, D3], [self.g1["id"]])
        self.assertEqual(sorted(self.routed_ips()), sorted([IP[D4], IP[D3], IP[D1], IP[D2]]))

    def test_a_mixed_case_or_colon_free_identity_is_the_same_display(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        self.multi(client, A, [D1.upper(), D1])
        self.assertEqual(sorted(self.routed_ips()), sorted([IP[D1]]))


class PermissionTests(Fixture):
    def test_a_group_member_is_not_a_display_the_account_may_pick_on_its_own(self):
        client = self.signed_in(sources=[A], groups=[self.g1["id"]])
        response = self.multi(client, A, [D1])
        self.assertEqual((response.status_code, response.get_json()["reason"]),
                         (403, "destination_not_permitted"))
        self.assertEqual(self.s.router.calls, [])

    def test_holding_a_display_does_not_grant_a_group_that_contains_it(self):
        client = self.signed_in(sources=[A], destinations=[D1, D2])
        response = self.multi(client, A, [], [self.g1["id"]])
        self.assertEqual((response.status_code, response.get_json()["reason"]),
                         (403, "group_not_permitted"))
        self.assertEqual(self.s.router.calls, [])

    def test_one_ungranted_display_refuses_the_whole_request_before_any_write(self):
        client = self.signed_in(sources=[A], destinations=[D1, D2])
        response = self.multi(client, A, [D1, D2, D3])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.s.router.calls, [], "nothing was written for the permitted ones either")

    def test_an_ungranted_source_is_refused(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.multi(client, B, [D1])
        self.assertEqual((response.status_code, response.get_json()["reason"]),
                         (403, "source_not_permitted"))
        self.assertEqual(self.s.router.calls, [])

    def test_a_group_the_account_holds_works_without_holding_its_members(self):
        client = self.signed_in(sources=[A], groups=[self.g2["id"]])
        body = self.multi(client, A, [], [self.g2["id"]]).get_json()
        self.assertEqual(sorted(self.routed_ips()), sorted([IP[D2], IP[D3]]))
        self.assertTrue(body["ok"])

    def test_an_administrator_is_not_held_to_a_users_grants(self):
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        response = self.multi(admin, B, [D4, D3])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sorted(self.routed_ips()), sorted([IP[D4], IP[D3]]))

    def test_the_gate_itself_reads_the_lists_it_is_given(self):
        for key, value, reason in (("displays", [D1, D3], "destination_not_permitted"),
                                   ("groups", ["grp_other"], "group_not_permitted")):
            self.assertIn(key, luma_gate.DESTINATION_KEYS + luma_gate.GROUP_KEYS)
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.multi(client, A, [D1, D3])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "destination_not_permitted")


class SequencingTests(Fixture):
    def test_destinations_are_dispatched_together_so_they_change_at_the_same_moment(self):
        # Superseded the strict one-after-another rule (MA-005): a room switched visibly display by
        # display. Every destination is now authorised first and then written in one bounded batch.
        client = self.signed_in(sources=[A], destinations=[D1, D2, D3])
        order = []

        def slow(decoder_ip, encoder_ip):
            order.append(("start", decoder_ip))
            time.sleep(0.15)
            order.append(("end", decoder_ip))

        self.s.router.on_call = slow
        body = self.multi(client, A, [D1, D2, D3]).get_json()
        self.assertEqual(self.s.router.max_active, 3, "all three were in flight together")
        self.assertEqual([kind for kind, _ip in order[:3]], ["start"] * 3, "every write began before any ended")
        self.assertEqual([r["index"] for r in body["results"]], [0, 1, 2], "reported in the order chosen")
        self.assertEqual(sorted(ip for kind, ip in order if kind == "start"), sorted([IP[D1], IP[D2], IP[D3]]))

    def test_a_failed_destination_does_not_stop_the_rest_and_nothing_is_undone(self):
        client = self.signed_in(sources=[A], destinations=[D1, D2, D3])
        self.s.router.fail[IP[D2]] = "unreachable"
        response = self.multi(client, A, [D1, D2, D3])
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sorted(self.routed_ips()), sorted([IP[D1], IP[D2], IP[D3]]), "the third was still attempted")
        self.assertEqual(len(self.s.router.calls), 3, "no rollback write of any kind")
        self.assertEqual((body["ok"], body["routed"], body["total"]), (False, 2, 3))
        self.assertEqual(body["summary"], "2 of 3 displays routed")
        self.assertEqual([r["ok"] for r in body["results"]], [True, False, True])
        self.assertEqual(body["results"][1]["result"], "unreachable")

    def test_a_display_that_does_not_confirm_is_a_failure_of_that_display_only(self):
        client = self.signed_in(sources=[A], destinations=[D1, D2])
        self.s.router.fail[IP[D1]] = "not_verified"
        body = self.multi(client, A, [D1, D2]).get_json()
        self.assertEqual([r["result"] for r in body["results"]], ["not_verified", "verified"])
        self.assertEqual(body["routed"], 1)

    def test_a_raising_route_is_one_displays_failure(self):
        client = self.signed_in(sources=[A], destinations=[D1, D2])
        calls = []

        def boom(decoder_ip, encoder_ip):
            calls.append(decoder_ip)
            if decoder_ip == IP[D1]:
                raise RuntimeError("device exploded")

        self.s.router.on_call = boom
        body = self.multi(client, A, [D1, D2]).get_json()
        self.assertEqual(calls, [IP[D1], IP[D2]])
        self.assertEqual([r["ok"] for r in body["results"]], [False, True])

    def test_a_busy_display_is_reported_and_the_others_still_route(self):
        from test_user_routing import mock_timeout
        client = self.signed_in(sources=[A], destinations=[D1, D2])
        locks = self.s.remote.locks
        with mock_timeout(locks):
            with locks.hold(locks.decoder(D1)):
                box = []
                t = threading.Thread(target=lambda: box.append(self.multi(client, A, [D1, D2])))
                t.start()
                t.join(10)
        body = box[0].get_json()
        self.assertEqual([r["result"] for r in body["results"]], ["busy", "verified"])
        self.assertEqual(sorted(self.routed_ips()), sorted([IP[D2]]))

    def test_a_permission_withdrawn_while_a_display_is_locked_stops_that_display(self):
        client = self.signed_in(sources=[A], destinations=[D1, D2])
        user_id = next(a["id"] for a in self.s.store.list_accounts() if a["username"] == "alex")
        locks = self.s.remote.locks
        acquired, release = threading.Event(), threading.Event()

        def holder():
            # Both displays are held: with the batch parallel an unheld display is written at once,
            # which is correct, so holding only one would leave nothing waiting to be withdrawn.
            with locks.hold(locks.decoder(D1), locks.decoder(D2)):
                acquired.set()
                release.wait(5)

        threading.Thread(target=holder, daemon=True).start()
        acquired.wait(5)
        box = []
        waiter = threading.Thread(target=lambda: box.append(self.multi(client, A, [D1, D2])), daemon=True)
        waiter.start()
        time.sleep(0.4)
        self.assertEqual(self.s.router.calls, [], "the request ran while the displays were held")
        self.s.store.set_permissions(user_id, luma_users.Permissions([A], [], []))
        release.set()
        waiter.join(10)
        self.assertEqual(self.s.router.calls, [], "nothing was written after the permission went")
        # The request was admitted, so it answers with a result per display: every one refused.
        body = box[0].get_json()
        self.assertEqual(box[0].status_code, 200)
        self.assertEqual([r["result"] for r in body["results"]], ["refused", "refused"])
        self.assertEqual(body["routed"], 0)


class RequestShapeTests(Fixture):
    def test_the_reply_carries_counts_names_and_outcomes_and_nothing_that_identifies_a_device(self):
        client = self.signed_in(sources=[A], destinations=[D1], groups=[self.g1["id"]])
        text = self.multi(client, A, [D1], [self.g1["id"]]).get_data(as_text=True)
        body = self.multi(client, A, [D1], [self.g1["id"]]).get_json()
        self.assertEqual(set(body), {"ok", "routed", "total", "summary", "results", "timing", "targets"})
        # `targets` says what became of each thing chosen, by position in the list sent: no identity.
        for target in body["targets"]:
            self.assertEqual(set(target), {"kind", "index", "state"})
            self.assertIn(target["state"], ("verified", "unconfirmed", "failed"))
        self.assertEqual(set(body["timing"]), {"workers", "first_start_ms", "last_start_ms", "first_end_ms",
                                               "last_end_ms", "start_spread_ms"})
        for row in body["results"]:
            self.assertEqual(set(row), {"index", "name", "ok", "result", "audio"})
        for marker in (D1, D2, A, "198.51.100", "192.0.2", self.g1["id"]):
            self.assertNotIn(marker, text, marker)
        self.assertEqual([r["index"] for r in body["results"]], [0, 1])

    def test_malformed_requests_are_400_and_write_nothing(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        bad = (
            {}, {"source": A}, {"source": A, "displays": [], "groups": []},
            {"source": A, "displays": D1}, {"source": A, "displays": [1, 2]},
            {"source": A, "displays": [""]}, {"source": A, "groups": "grp"},
            {"source": A, "displays": ["02:00:5e:20:%02x:%02x" % (i // 256, i % 256) for i in range(65)]},
            {"source": A, "displays": None, "groups": [None]},
            {"source": "", "displays": [D1]},
        )
        for body in bad:
            response = self.s.post(client, ROUTE, body)
            self.assertIn(response.status_code, (400, 403), body)
        self.assertEqual(self.s.router.calls, [])

    def test_an_oversized_list_is_refused_before_anything_is_resolved(self):
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        too_many = ["02:00:5e:20:%02x:%02x" % (i // 256, i % 256) for i in range(65)]
        response = self.s.post(admin, ROUTE, {"source": A, "displays": too_many})
        self.assertEqual(response.status_code, 400)
        response = self.s.post(admin, ROUTE, {"source": A, "groups": ["grp_%016x" % i for i in range(17)]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.s.router.calls, [])

    def test_a_granted_group_that_no_longer_exists_is_404_and_writes_nothing(self):
        client = self.signed_in(sources=[A], destinations=[D1], groups=["grp_0000000000000000"])
        response = self.multi(client, A, [D1], ["grp_0000000000000000"])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.s.router.calls, [])

    def test_a_permitted_display_the_host_does_not_know_is_reported_not_found(self):
        ghost = "02:00:5e:99:99:99"
        client = self.signed_in(sources=[A], destinations=[ghost, D1])
        body = self.multi(client, A, [ghost, D1]).get_json()
        self.assertEqual([r["result"] for r in body["results"]], ["not_found", "verified"])

    def test_every_request_is_audited_with_its_outcome_and_no_address(self):
        client = self.signed_in(sources=[A], destinations=[D1, D2])
        self.s.router.fail[IP[D2]] = "unreachable"
        self.multi(client, A, [D1, D2])
        entry = [e for e in self.s.store.audit_entries(limit=20) if e["event"] == "route_multi"][0]
        self.assertEqual(entry["actor"], "alex")
        self.assertEqual((entry["detail"]["routed"], entry["detail"]["total"]), (1, 2))
        self.assertNotIn("198.51.100", repr(entry))
        import luma_audit_text
        sentence = luma_audit_text.describe(entry, lambda ident: "X")
        self.assertIn("1 of 2 displays routed", sentence)
        self.assertNotIn("route_multi", sentence)


class HandlerSecondLineTests(Fixture):
    """The handler re-checks what the gate checks, reached by taking the gate out of the way."""

    def around_the_gate(self, name):
        account = next(a for a in self.s.store._accounts.values() if a.username == name)
        original = luma_gate.evaluate
        patcher = mock.patch.object(luma_gate, "evaluate",
                                    lambda *a, **k: luma_gate.Decision(True, 200, "permitted", account))
        patcher.start()
        self.addCleanup(patcher.stop)
        return original, account

    def gate_would_refuse(self, original, account, payload):
        from types import SimpleNamespace
        store = SimpleNamespace(resolve_session=lambda token: (account, SimpleNamespace(
            csrf_token="c", from_direct_link=False)))
        environ = {"luma.listener": "remote", "HTTP_COOKIE": "lumasuite_session=x",
                   "HTTP_X_LUMASUITE_CSRF": "c"}
        decision = original(ROUTE, "POST", environ, payload, store)
        self.assertFalse(decision.allowed, "the gate would have admitted this: the test measures nothing")

    def test_the_handler_refuses_what_the_account_was_not_given(self):
        client = self.signed_in(sources=[A], destinations=[D1], groups=[self.g1["id"]])
        original, account = self.around_the_gate("alex")
        cases = (({"source": B, "displays": [D1], "groups": []}, "source_not_permitted"),
                 ({"source": A, "displays": [D3], "groups": []}, "destination_not_permitted"),
                 ({"source": A, "displays": [], "groups": [self.g2["id"]]}, "group_not_permitted"),
                 ({"source": A, "displays": [D2], "groups": [self.g1["id"]]}, "destination_not_permitted"))
        for payload, reason in cases:
            self.gate_would_refuse(original, account, payload)
            response = self.s.post(client, ROUTE, payload)
            self.assertEqual((response.status_code, response.get_json()["reason"]), (403, reason), payload)
        self.assertEqual(self.s.router.calls, [])


class RouteTableTests(unittest.TestCase):
    def test_the_endpoint_is_classified_as_a_user_route_and_exists_in_the_route_table(self):
        self.assertIs(luma_access.ROUTES[ROUTE], luma_access.Access.USER)
        s = System()
        self.addCleanup(s.close)
        rules = {r.rule for r in s.app.url_map.iter_rules()}
        self.assertIn(ROUTE, rules)

    def test_the_page_calls_exactly_the_endpoint_the_server_registers(self):
        with open(os.path.join(_support.ROOT, "ui", "user.js"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("'" + ROUTE + "'", script)
        for retired in ("/api/user/route_group'", "/api/user/route'"):
            self.assertNotIn(retired, script, "the page routes through one endpoint")

    def test_the_older_endpoints_still_work(self):
        s = System()
        self.addCleanup(s.close)
        s.turn_remote_on()
        s.admin("root")
        group = s.remote.groups.create("G", [D1, D2])["group"]
        s.user("alex", sources=[A], destinations=[D1], groups=[group["id"]])
        client = s.sign_in("alex", USER_PASSWORD)
        self.assertEqual(s.post(client, "/api/user/route", {"source": A, "destination": D1}).status_code, 200)
        self.assertEqual(s.post(client, "/api/user/route_group", {"source": A, "group": group["id"]}).status_code, 200)


class PreviewCounterTests(Fixture):
    def setUp(self):
        super().setUp()
        luma_previewstats.reset()
        self.addCleanup(luma_previewstats.reset)

    def with_stats(self, value="1"):
        patcher = mock.patch.dict(os.environ, {"LUMA_PREVIEW_STATS": value})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_counters_are_off_by_default_and_silent(self):
        os.environ.pop("LUMA_PREVIEW_STATS", None)
        luma_previewstats.note("requests", "source")
        self.assertEqual(luma_previewstats.snapshot(), {})
        client = self.s.sign_in("root", ADMIN_PASSWORD, remote=False)
        self.assertEqual(client.get("/api/preview_stats").status_code, 404)

    def test_the_endpoint_is_local_only_so_a_remote_account_cannot_read_or_reset_it(self):
        self.with_stats()
        self.assertIs(luma_access.ROUTES["/api/preview_stats"], luma_access.Access.LOCAL_ONLY)
        admin = self.s.sign_in("root", ADMIN_PASSWORD)                    # remote admin
        local = self.s.sign_in("root", ADMIN_PASSWORD, remote=False)
        luma_previewstats.note("requests", "source")
        self.assertIn("counts", local.get("/api/preview_stats").get_json(), "precondition: local can read it")
        # A local-only route is not served to a remote request at all, whoever signed in.
        for response in (admin.get("/api/preview_stats"), self.s.post(admin, "/api/preview_stats", {})):
            self.assertIn(response.status_code, (403, 404))
            self.assertNotIn("counts", response.get_data(as_text=True))
        self.assertEqual(luma_previewstats.snapshot(), {"requests.source": 1}, "a remote reset did nothing")

    def test_requests_and_answers_are_counted_when_enabled_and_zeroed_by_post(self):
        self.with_stats()
        client = self.signed_in(sources=[A], destinations=[D1])
        client.get("/api/user/preview?source=" + A + "&fresh=1")
        client.get("/api/user/preview?display=" + D1)
        counts = luma_previewstats.snapshot()
        self.assertEqual(counts["requests.source.fresh"], 1)
        self.assertEqual(counts["requests.display"], 1)
        self.assertEqual(counts["answers.image"], 2)
        local = self.s.sign_in("root", ADMIN_PASSWORD, remote=False)
        self.assertEqual(local.get("/api/preview_stats").get_json()["counts"], counts)
        local.post("/api/preview_stats", json={}, headers={"X-LumaSuite-CSRF": local.csrf})
        self.assertEqual(luma_previewstats.snapshot(), {})

    def test_a_refused_preview_is_not_counted_as_a_request(self):
        self.with_stats()
        client = self.signed_in(sources=[A], destinations=[D1])
        client.get("/api/user/preview?source=" + B)
        self.assertEqual(luma_previewstats.snapshot(), {})

    def test_the_table_is_bounded(self):
        self.with_stats()
        for i in range(500):
            luma_previewstats.note("requests", "k%d" % i)
        self.assertLessEqual(len(luma_previewstats.snapshot()), 64)


class SwitcherPreviewTests(unittest.TestCase):
    def test_a_switcher_is_never_asked_for_a_picture(self):
        import lumaserver
        self.assertEqual(lumaserver._preview_urls("display", "192.0.2.9", "switcher"), [])
        with mock.patch.object(lumaserver, "_preview_address_ok", return_value=True), \
                mock.patch.dict(lumaserver.cache_units, {"192.0.2.9": {"type": "switcher"}}), \
                mock.patch.object(lumaserver, "_preview_still") as still:
            result = lumaserver._fetch_preview_uncached("display", "192.0.2.9")
        still.assert_not_called()
        self.assertIsNone(result["image"])
        self.assertEqual(result["reason"], "preview_unavailable")

    def test_a_decoder_uses_its_own_thumbnail_and_an_encoder_the_stream(self):
        import lumaserver
        self.assertEqual(lumaserver._preview_urls("display", "192.0.2.9", "decoder"),
                         ["http://192.0.2.9/thumbnail/thumbnail1.jpg"])
        self.assertTrue(lumaserver._preview_urls("source", "192.0.2.9", "encoder")[0].startswith(
            "http://192.0.2.9/stream?"))

    def test_device_fetches_are_counted_per_url_not_per_call(self):
        import lumaserver
        with mock.patch.dict(os.environ, {"LUMA_PREVIEW_STATS": "1"}):
            luma_previewstats.reset()
            self.addCleanup(luma_previewstats.reset)
            with mock.patch.object(lumaserver, "_preview_address_ok", return_value=True), \
                    mock.patch.dict(lumaserver.cache_units, {"192.0.2.9": {"type": "decoder"}}), \
                    mock.patch.object(lumaserver, "_preview_still", return_value=b"\xff\xd8\xff\xd9"):
                lumaserver._fetch_preview_uncached("display", "192.0.2.9")
            self.assertEqual(luma_previewstats.snapshot(), {"device.display": 1})


if __name__ == "__main__":
    unittest.main()
