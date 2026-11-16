"""What a restricted User sees and can do: projection, permissions, preview and route."""
import threading
import unittest

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4,
                      SOURCE_A, SOURCE_B, SOURCE_C, System, identity)

import luma_projection
import luma_users

A, B, C = (identity(u) for u in (SOURCE_A, SOURCE_B, SOURCE_C))
D1, D2, D3, D4 = (identity(u) for u in (DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4))


def secret_markers(units=_support.ALL_UNITS):
    """Everything a restricted response must never contain."""
    marks = []
    for unit in units:
        marks += [unit["ip"], unit["model"], unit["enc"]["video"]["streamname"] if "enc" in unit else ""]
    return [m for m in marks if m]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        self.group = self.s.remote.groups.create("Lobby", [D1, D2, D3])["group"]

    def signed_in(self, name="alex", **grants):
        self.s.user(name, **grants)
        return self.s.sign_in(name, USER_PASSWORD)

    def state(self, client):
        return client.get("/api/user/state").get_json()


class ProjectionTests(Fixture):
    def test_a_user_sees_only_what_they_were_given(self):
        client = self.signed_in(sources=[A], destinations=[D1], groups=[self.group["id"]])
        body = self.state(client)
        self.assertEqual([x["id"] for x in body["sources"]], [A])
        self.assertEqual([x["id"] for x in body["destinations"]], [D1])
        self.assertEqual([x["id"] for x in body["groups"]], [self.group["id"]])

    def test_the_exact_keys_of_every_projected_object(self):
        client = self.signed_in(sources=[A, B], destinations=[D1], groups=[self.group["id"]])
        body = self.state(client)
        self.assertEqual(set(body), luma_projection.STATE_FIELDS)
        for source in body["sources"]:
            self.assertEqual(set(source), luma_projection.SOURCE_FIELDS)
        for display in body["destinations"]:
            self.assertEqual(set(display), luma_projection.DESTINATION_FIELDS)
            self.assertEqual(set(display["showing"]), luma_projection.SHOWING_FIELDS)
        for group in body["groups"]:
            self.assertEqual(set(group), luma_projection.GROUP_FIELDS)
            self.assertEqual(set(group["showing"]), luma_projection.SHOWING_FIELDS)

    def test_no_address_hostname_model_stream_or_password_reaches_a_user(self):
        client = self.signed_in(sources=[A, B, C], destinations=[D1, D2], groups=[self.group["id"]])
        text = client.get("/api/user/state").get_data(as_text=True)
        for marker in secret_markers():
            self.assertNotIn(marker, text, marker)
        for word in ("password", "hostname", "streamname", "ip", "mac", "serial", "firmware"):
            self.assertNotIn('"%s"' % word, text, word)

    def test_an_ungranted_device_is_neither_listed_nor_named(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        text = client.get("/api/user/state").get_data(as_text=True)
        for other in (B, C, D2, D3, D4):
            self.assertNotIn(other, text)
        for name in ("Bravo", "Charlie", "Lobby Two", "Lobby Three"):
            self.assertNotIn(name, text)

    def test_an_ungranted_group_is_neither_listed_nor_named(self):
        other = self.s.remote.groups.create("Boardroom Secret", [D4])["group"]
        client = self.signed_in(groups=[self.group["id"]])
        body = self.state(client)
        self.assertEqual([g["id"] for g in body["groups"]], [self.group["id"]])
        text = client.get("/api/user/state").get_data(as_text=True)
        self.assertNotIn(other["id"], text)
        self.assertNotIn("Boardroom Secret", text)
        nobody = self.signed_in("sam")
        self.assertEqual(self.state(nobody)["groups"], [])

    def test_a_group_reports_a_count_and_never_lists_its_members(self):
        client = self.signed_in(groups=[self.group["id"]])
        body = self.state(client)
        self.assertEqual(body["groups"][0]["members"], 3)
        self.assertEqual(body["destinations"], [])
        text = client.get("/api/user/state").get_data(as_text=True)
        for member in (D1, D2, D3):
            self.assertNotIn(member, text)

    def test_a_display_showing_a_source_the_user_may_not_use_is_not_named(self):
        self.s.units[3]["dec"]["video"]["streamname"] = "stream-bravo"
        client = self.signed_in(sources=[A], destinations=[D1])
        shown = self.state(client)["destinations"][0]["showing"]
        self.assertEqual(shown["mode"], "other")
        self.assertEqual(shown["source_id"], "")
        self.assertNotIn("Bravo", repr(shown))

    def test_a_display_showing_a_permitted_source_names_it(self):
        self.s.units[3]["dec"]["video"]["streamname"] = "stream-alpha"
        client = self.signed_in(sources=[A], destinations=[D1])
        shown = self.state(client)["destinations"][0]["showing"]
        self.assertEqual((shown["mode"], shown["source_id"], shown["source_name"]),
                         ("source", A, "Alpha Camera"))

    def test_an_ambiguous_stream_name_is_reported_as_something_else(self):
        self.s.units[1]["enc"]["video"]["streamname"] = "stream-alpha"   # two encoders, one name
        self.s.units[3]["dec"]["video"]["streamname"] = "stream-alpha"
        client = self.signed_in(sources=[A, B], destinations=[D1])
        self.assertEqual(self.state(client)["destinations"][0]["showing"]["mode"], "other")

    def test_an_offline_device_stays_visible_and_is_marked(self):
        self.s.units[3]["status"] = "failed"
        client = self.signed_in(destinations=[D1])
        row = self.state(client)["destinations"][0]
        self.assertFalse(row["online"])

    def test_friendly_names_are_used_and_never_decide_access(self):
        self.s.remote.aliases.set_alias(A, "Main Stage")
        client = self.signed_in(sources=[A])
        self.assertEqual(self.state(client)["sources"][0]["name"], "Main Stage")
        # A name is a label: it is not an identity, so asking by name asks for nothing.
        response = self.s.post(client, "/api/user/route", {"source": "Main Stage", "destination": D1})
        self.assertIn(response.status_code, (400, 403))
        self.assertEqual(self.s.router.calls, [])

    def test_changing_a_device_address_keeps_its_name_and_its_permission(self):
        self.s.remote.aliases.set_alias(D1, "Lobby Left")
        client = self.signed_in(sources=[A], destinations=[D1])
        self.s.units[3]["ip"] = "198.51.100.99"                # the display moved
        body = self.state(client)
        self.assertEqual(body["destinations"][0]["name"], "Lobby Left")
        response = self.s.post(client, "/api/user/route", {"source": A, "destination": D1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.s.router.calls[-1][0], "198.51.100.99")   # routed to where it is now

    def test_duplicate_names_are_told_apart_by_location_only_where_they_repeat(self):
        self.s.units[3]["hostname"] = self.s.units[4]["hostname"] = "Front Display"
        self.s.units[3]["systeminfo"] = {"config": {"location": "Room 101"}}
        self.s.units[4]["systeminfo"] = {"config": {"location": "Room 102"}}
        client = self.signed_in(destinations=[D1, D2])
        names = sorted(d["name"] for d in self.state(client)["destinations"])
        self.assertEqual(names, ["Front Display (Room 101)", "Front Display (Room 102)"])
        lone = self.signed_in("sam", destinations=[D1])
        self.assertEqual(self.state(lone)["destinations"][0]["name"], "Front Display")

    def test_the_order_is_deterministic_whatever_order_devices_arrive_in(self):
        client = self.signed_in(sources=[A, B, C], destinations=[D1, D2, D3, D4])
        first = self.state(client)
        self.s.units.reverse()
        self.assertEqual(self.state(client), first)

    def test_a_group_member_without_a_hardware_address_cannot_exist(self):
        result = self.s.remote.groups.create("Bad", ["192.0.2.10"])
        self.assertFalse(result["ok"])


class PermissionTests(Fixture):
    def route(self, client, source, destination):
        return self.s.post(client, "/api/user/route", {"source": source, "destination": destination})

    def test_a_permitted_route_is_made(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        body = self.route(client, A, D1).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(self.s.router.calls, [("198.51.100.21", "192.0.2.10")])

    def test_an_unpermitted_source_is_refused_before_anything_is_touched(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.route(client, B, D1)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "source_not_permitted")
        self.assertEqual(self.s.router.calls, [])

    def test_an_unpermitted_display_is_refused_before_anything_is_touched(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.route(client, A, D2)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "destination_not_permitted")
        self.assertEqual(self.s.router.calls, [])

    def test_an_address_in_place_of_an_identity_is_refused(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        for source, destination in (("192.0.2.10", D1), (A, "198.51.100.21")):
            response = self.route(client, source, destination)
            self.assertIn(response.status_code, (400, 403))
        self.assertEqual(self.s.router.calls, [])

    def test_a_refusal_does_not_reveal_whether_the_device_exists(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        real = self.route(client, B, D1)
        invented = self.route(client, "02:00:5e:99:99:99", D1)
        self.assertEqual(real.status_code, invented.status_code)
        self.assertEqual(real.get_json(), invented.get_json())

    def test_a_permitted_but_unknown_device_is_a_404_after_authorization(self):
        client = self.signed_in(sources=["02:00:5e:99:99:99"], destinations=[D1])
        response = self.route(client, "02:00:5e:99:99:99", D1)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.s.router.calls, [])

    def test_malformed_input_is_400(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        for body in ({}, {"source": A}, {"destination": D1}, {"source": "", "destination": ""}):
            self.assertEqual(self.s.post(client, "/api/user/route", body).status_code, 400)

    def test_permissions_are_reread_after_the_wait_for_the_display(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        user_id = next(a["id"] for a in self.s.store.list_accounts() if a["username"] == "alex")

        # Hold the display's lock from "another operation", release it only after the
        # permission has been withdrawn: the waiting request must then be refused.
        lock = self.s.remote.locks
        results = []
        acquired, release = threading.Event(), threading.Event()

        def holder():
            with lock.hold(lock.decoder(D1)):
                acquired.set()
                release.wait(5)

        threading.Thread(target=holder, daemon=True).start()
        acquired.wait(5)

        def request():
            results.append(self.route(client, A, D1))

        waiter = threading.Thread(target=request, daemon=True)
        waiter.start()
        __import__("time").sleep(0.4)          # the request is now queued behind the held lock
        self.assertEqual(self.s.router.calls, [], "the request ran while the display was held")
        # Withdraw the permission while the request waits.
        self.s.store.set_permissions(user_id, luma_users.Permissions([A], [], []))
        release.set()
        waiter.join(10)
        self.assertEqual(self.s.router.calls, [])
        self.assertIn(results[0].status_code, (401, 403))

    def test_two_routes_to_one_display_never_overlap(self):
        client_a = self.signed_in("one", sources=[A], destinations=[D1])
        client_b = self.signed_in("two", sources=[B], destinations=[D1])
        self.s.router.on_call = lambda *_: __import__("time").sleep(0.05)
        threads = [threading.Thread(target=lambda c=c, src=src: self.route(c, src, D1))
                   for c, src in ((client_a, A), (client_b, B))]
        for t in threads: t.start()
        for t in threads: t.join(10)
        self.assertEqual(self.s.router.max_active, 1)
        self.assertEqual(len(self.s.router.calls), 2)

    def test_a_busy_display_is_a_409_with_its_own_reason(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        lock = self.s.remote.locks
        with mock_timeout(lock):
            with lock.hold(lock.decoder(D1)):
                response = self._route_in_thread(client, A, D1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "resource_busy")

    def _route_in_thread(self, client, source, destination):
        box = []
        t = threading.Thread(target=lambda: box.append(self.route(client, source, destination)))
        t.start()
        t.join(10)
        return box[0]

    def test_an_administrator_is_not_held_to_a_users_grants(self):
        admin = self.s.sign_in("root", ADMIN_PASSWORD)
        self.assertEqual(self.s.post(admin, "/api/user/route", {"source": C, "destination": D4}).status_code, 200)

    def test_every_route_is_audited_with_its_outcome_and_no_address(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        self.route(client, A, D1)
        entry = [e for e in self.s.store.audit_entries(limit=20) if e["event"] == "route"][0]
        self.assertEqual(entry["actor"], "alex")
        self.assertEqual(entry["detail"]["result"], "verified")
        self.assertNotIn("192.0.2", repr(entry))


class HandlerSecondLineTests(Fixture):
    """The handlers re-check what the gate checks.

    Through HTTP the gate refuses first, so the handler's own check can never be observed.
    That makes it untested rather than redundant, so the gate is taken out of the way for
    the span of each test -- and the precondition is asserted, so the test says which of the
    two checks it is measuring.
    """

    def around_the_gate(self, client_account_name):
        import luma_gate
        from unittest import mock
        account = next(a for a in self.s.store._accounts.values() if a.username == client_account_name)
        original = luma_gate.evaluate

        def open_gate(*args, **kwargs):
            return luma_gate.Decision(True, 200, "permitted", account)

        patcher = mock.patch.object(luma_gate, "evaluate", open_gate)
        patcher.start()
        self.addCleanup(patcher.stop)
        return original

    def gate_would_refuse(self, original, payload, rule, account_name):
        account = next(a for a in self.s.store._accounts.values() if a.username == account_name)
        from types import SimpleNamespace
        store = SimpleNamespace(resolve_session=lambda token: (account, SimpleNamespace(
            csrf_token="c", from_direct_link=False)))
        environ = {"luma.listener": "remote", "HTTP_COOKIE": "lumasuite_session=x",
                   "HTTP_X_LUMASUITE_CSRF": "c"}
        decision = original(rule, "POST", environ, payload, store)
        self.assertFalse(decision.allowed, "the gate would have admitted this: the test measures nothing")

    def test_route_refuses_an_unpermitted_source_or_display_on_its_own(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        original = self.around_the_gate("alex")
        for payload, reason in (({"source": B, "destination": D1}, "source_not_permitted"),
                                ({"source": A, "destination": D2}, "destination_not_permitted")):
            self.gate_would_refuse(original, payload, "/api/user/route", "alex")
            response = self.s.post(client, "/api/user/route", payload)
            self.assertEqual((response.status_code, response.get_json()["reason"]), (403, reason))
        self.assertEqual(self.s.router.calls, [])

    def test_group_route_refuses_an_unpermitted_group_or_source_on_its_own(self):
        client = self.signed_in(sources=[A], groups=[self.group["id"]])
        other = self.s.remote.groups.create("Other", [D1])["group"]
        original = self.around_the_gate("alex")
        for payload, reason in (({"source": A, "group": other["id"]}, "group_not_permitted"),
                                ({"source": B, "group": self.group["id"]}, "source_not_permitted")):
            self.gate_would_refuse(original, payload, "/api/user/route_group", "alex")
            response = self.s.post(client, "/api/user/route_group", payload)
            self.assertEqual((response.status_code, response.get_json()["reason"]), (403, reason))
        self.assertEqual(self.s.router.calls, [])

    def test_preview_refuses_an_unpermitted_device_on_its_own(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        original = self.around_the_gate("alex")
        for query, field, value in (("source=" + B, "source", B), ("display=" + D2, "display", D2)):
            self.gate_would_refuse(original, {field: value}, "/api/user/preview", "alex")
            response = client.get("/api/user/preview?" + query)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.s.previews, [])


class mock_timeout:
    """Make a lock wait short, so a busy display is observable without a 45 s test."""

    def __init__(self, locks):
        self.locks = locks

    def __enter__(self):
        import luma_locks
        self.old = luma_locks.DEFAULT_TIMEOUT_SECONDS
        luma_locks.DEFAULT_TIMEOUT_SECONDS = 0.2
        self._hold = self.locks.hold
        original = self.locks.hold
        self.locks.hold = lambda *names, timeout=None: original(*names, timeout=0.2)
        return self

    def __exit__(self, *exc):
        import luma_locks
        luma_locks.DEFAULT_TIMEOUT_SECONDS = self.old
        self.locks.hold = self._hold


class RouteResultTests(Fixture):
    def test_a_route_the_display_did_not_confirm_is_reported_as_a_failure(self):
        self.s.router.fail["198.51.100.21"] = "not_verified"
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.s.post(client, "/api/user/route", {"source": A, "destination": D1})
        body = response.get_json()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(body["ok"])
        self.assertEqual(body["result"], "not_verified")
        self.assertIn("did not confirm", body["error"])

    def test_an_unreachable_display_is_503_and_names_nothing_internal(self):
        self.s.router.fail["198.51.100.21"] = "unreachable"
        client = self.signed_in(sources=[A], destinations=[D1])
        response = self.s.post(client, "/api/user/route", {"source": A, "destination": D1})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("198.51", response.get_data(as_text=True))

    def test_the_response_carries_only_the_projected_fields(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        body = self.s.post(client, "/api/user/route", {"source": A, "destination": D1}).get_json()
        self.assertEqual(set(body), {"ok", "result", "audio"})
        self.assertEqual(body["audio"], "follows")


class PreviewTests(Fixture):
    def test_a_permitted_preview_is_an_image_fetched_by_the_server(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        response = client.get("/api/user/preview?source=" + A)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.s.previews, [("source", "192.0.2.10")])
        self.assertNotIn("192.0.2", repr(dict(response.headers)))

    def test_a_display_preview_asks_for_the_display_mechanism(self):
        client = self.signed_in(destinations=[D1])
        client.get("/api/user/preview?display=" + D1)
        self.assertEqual(self.s.previews, [("display", "198.51.100.21")])

    def test_an_unpermitted_preview_is_refused_before_any_device_is_asked(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        for query in ("source=" + B, "display=" + D2):
            response = client.get("/api/user/preview?" + query)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.s.previews, [])

    def test_a_group_members_preview_is_not_available_through_the_group(self):
        client = self.signed_in(groups=[self.group["id"]])
        self.assertEqual(client.get("/api/user/preview?display=" + D1).status_code, 403)
        self.assertEqual(self.s.previews, [])

    def test_naming_both_or_neither_is_a_400(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        self.assertEqual(client.get("/api/user/preview").status_code, 400)
        self.assertEqual(client.get("/api/user/preview?source=%s&display=%s" % (A, D1)).status_code, 400)

    def test_a_preview_that_is_unavailable_is_a_normal_answer_not_an_error(self):
        self.s._preview = lambda *a, **k: {"image": None, "reason": "preview_unavailable"}
        # the stub captured the original bound method; replace what the API calls instead
        client = self.signed_in(sources=[A])
        # (the bound method captured at registration cannot be swapped; use the unit's absence)
        self.s.units[:] = [u for u in self.s.units if identity(u) != A]
        response = client.get("/api/user/preview?source=" + A)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["reason"], "preview_unavailable")

    def test_a_preview_by_address_is_not_accepted(self):
        client = self.signed_in(sources=[A])
        self.assertIn(client.get("/api/user/preview?source=192.0.2.10").status_code, (400, 403))
        self.assertEqual(self.s.previews, [])

    def test_preview_requires_a_session(self):
        self.assertEqual(self.s.client().get("/api/user/preview?source=" + A).status_code, 401)


class SharedObservationTests(Fixture):
    def test_the_state_endpoint_asks_the_shared_observer_not_the_devices(self):
        client = self.signed_in(sources=[A], destinations=[D1])
        before = self.s.observed
        self.state(client)
        self.assertEqual(self.s.observed, before + 1)
        self.assertEqual(self.s.router.calls, [])


if __name__ == "__main__":
    unittest.main()
