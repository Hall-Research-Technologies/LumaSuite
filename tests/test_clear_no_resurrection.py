"""A unit that Clear Units removed is never brought back by a stale request (LUMA-REL-002).

The defect: the verified A/V route recorded its result with `cache_units.get(decoder_ip, {})` and then
stored whatever it held back under the address, so a route that targeted a cleared unit -- or one that was
still running when the unit was cleared -- recreated a record with no address and no hardware identity
(`dec` and `routed_*` only). Nothing could remove it but another Clear Units.

These tests hold the contract at the cache and at the status codes the application already uses:

  400  the request is malformed            404  well-formed, and the inventory does not hold it
  409  the inventory changed under it      never 5xx for any of them

A stale request fails truthfully and creates nothing; a unit that is rediscovered afterwards works and is
not confused with the stale request. Fixtures are synthetic and deep-copied; the transport is the
in-memory model from `_support`, and the hardware fence is installed by `_support` itself.
"""
import copy
import threading
import unittest

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, SOURCE_A, SOURCE_B,
                      RealApp, RealRemote, identity)

import luma_users

ENC_A, ENC_B = SOURCE_A["ip"], SOURCE_B["ip"]
DEC_1, DEC_2 = DISPLAY_1["ip"], DISPLAY_2["ip"]


def is_partial(unit):
    """A record that has no address or no hardware identity: what a resurrection looks like."""
    return not unit.get("ip") or not unit.get("mac")


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.dev = self.app.devices
        self.app.seed()
        self.client = self.m.APP.test_client()
        self.addCleanup(self.m._device_credential_cache.clear)

    # -- helpers ----------------------------------------------------------
    def route(self, decoder=DEC_1, encoder=ENC_A, **extra):
        body = {"decoder_ip": decoder, "encoder_ip": encoder}
        body.update(extra)
        return self.client.post("/api/route_matrix", json=body)

    def clear(self):
        """Clear Units through the real endpoint (Remote Access is off in these tests)."""
        response = self.client.post("/api/clear_units", json={})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def clear_silently(self):
        """What the endpoint does to the inventory, without the files: a new generation and no units."""
        with self.m.cache_lock:
            self.m.cache_generation += 1
            self.m.cache_units.clear()

    def inventory(self):
        with self.m.cache_lock:
            return copy.deepcopy(dict(self.m.cache_units))

    def epoch(self):
        return self.client.get("/api/cache").get_json()["inventory_epoch"]

    def rediscover(self, unit):
        """The scan folding one unit back in, as `_scan_fast` does under the cache lock."""
        with self.m.cache_lock:
            self.m._ingest_scanned_unit(unit["ip"], copy.deepcopy(unit), {})

    def assert_clean(self, note=""):
        held = self.inventory()
        self.assertEqual(held, {}, "the cleared inventory was repopulated %s: %r" % (note, held))

    def assert_not_5xx(self, response):
        self.assertLess(response.status_code, 500, response.get_data(as_text=True))


class StaleRouteAfterClearTests(Base):
    def test_clear_while_idle_then_route_is_unknown_not_a_resurrection(self):
        self.clear()
        self.dev.calls.clear()
        response = self.route()
        body = response.get_json()
        self.assertEqual(response.status_code, 404)
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "unknown_unit")
        self.assert_clean()
        self.assertEqual(self.dev.calls, [], "a unit the inventory does not hold was sent a command")

    def test_the_defect_itself_the_verified_route_never_creates_a_record(self):
        """The function that used to resurrect, called directly, with the decoder gone."""
        self.clear_silently()
        outcome = self.m._route_av(DEC_1, ENC_A, allow_cached_stream=True)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "unknown_unit")
        self.assert_clean()

    def test_clear_immediately_before_the_route(self):
        self.clear_silently()
        response = self.route()
        self.assertEqual(response.status_code, 404)
        self.assert_clean()

    def test_a_source_the_inventory_does_not_hold_is_still_routable_as_it_always_was(self):
        """Only the display is recorded on; a source is read from its own device."""
        with self.m.cache_lock:
            self.m.cache_units.pop(ENC_A)
        self.assertEqual(self.route().status_code, 200)
        self.assertIsNone(self.app.unit(ENC_A), "a route must not create the source's record either")

    def test_a_breakaway_route_to_a_cleared_unit_is_unknown_too(self):
        self.clear()
        for mode in ("video", "audio", "av", "breakaway"):
            response = self.client.post("/api/route_matrix", json={
                "decoder_ip": DEC_1, "encoder_ip": ENC_A, "mode": mode,
                "video_encoder_ip": ENC_A, "audio_encoder_ip": ENC_B})
            self.assertEqual(response.status_code, 404, mode)
            self.assertEqual(response.get_json()["reason"], "unknown_unit", mode)
        self.assert_clean()

    def test_the_endpoint_refuses_before_the_engine_is_reached(self):
        """The endpoint's own refusal and the engine's entry check are two lines of defence; this
        reaches the first by making the second fail the test if it is the one that answers."""
        self.clear_silently()

        def never(*args, **kwargs):
            raise AssertionError("the breakaway engine was reached for a unit the inventory does not hold")
        patcher = self.app.mock.patch.object(self.m, "_route_breakaway", never)
        patcher.start()
        self.addCleanup(patcher.stop)
        response = self.client.post("/api/route_matrix", json={
            "decoder_ip": DEC_1, "encoder_ip": ENC_A, "mode": "video"})
        self.assertEqual(response.status_code, 404)

    def test_the_engine_refuses_an_unknown_display_by_itself(self):
        """The second line, reached directly: no device call, no record."""
        self.clear_silently()
        self.dev.calls.clear()
        outcome = self.m._route_breakaway(DEC_1, "video", ENC_A, None)
        self.assertEqual((outcome["ok"], outcome["reason"]), (False, "unknown_unit"))
        self.assertEqual(self.dev.calls, [])
        self.assert_clean()

    def test_repeated_clear_is_harmless_and_the_route_stays_unknown(self):
        for _ in range(3):
            self.clear()
        self.assertEqual(self.route().status_code, 404)
        self.assertEqual(self.route(decoder=DEC_2, encoder=ENC_B).status_code, 404)
        self.assert_clean()

    def test_malformed_is_400_and_never_unknown(self):
        self.clear()
        for body in ({"decoder_ip": "not-an-ip", "encoder_ip": ENC_A},
                     {"decoder_ip": DEC_1},
                     {"decoder_ip": None, "encoder_ip": None},
                     {"decoder_ip": 7, "encoder_ip": ENC_A}):
            response = self.client.post("/api/route_matrix", json=body)
            self.assertEqual(response.status_code, 400, body)
        self.assert_clean()

    def test_clear_with_offline_units_present_still_leaves_nothing_to_route_to(self):
        with self.m.cache_lock:
            for _ in range(10):
                self.m.luma_liveness.record(self.m.cache_units[DEC_1], False)
            self.assertTrue(self.m.luma_projection.is_offline(self.m.cache_units[DEC_1]))
        self.assertEqual(self.route().status_code, 409, "the offline unit is refused as offline while held")
        self.clear()
        response = self.route()
        self.assertEqual(response.status_code, 404, "a cleared offline unit is unknown, not offline")
        self.assertEqual(response.get_json()["reason"], "unknown_unit")
        self.assert_clean()


class RouteInFlightWhenClearedTests(Base):
    def test_clear_while_a_route_is_pending_fails_as_a_conflict_and_creates_nothing(self):
        fired = []

        def clear_at_the_write(ip, name):
            if not fired:
                fired.append(ip)
                self.clear_silently()
        self.dev.on_set = clear_at_the_write
        response = self.route()
        body = response.get_json()
        self.assertTrue(fired, "the clear never happened: the test did not measure what it claims")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(body["status"], "conflict")
        self.assert_clean("by a route that finished after the clear")

    def test_clear_while_a_breakaway_route_is_pending(self):
        # The engine is replaced by one that verifies a route and reports what the display holds, with
        # the inventory cleared while it runs -- the shape of a real breakaway that outlives a clear.
        fired = []

        def engine(*args, **kwargs):
            fired.append(True)
            self.clear_silently()
            return {"ok": True, "status": "verified", "mode": "video", "writes": [1], "video_stream": "s",
                    "after": {"video": "s", "audio": "a", "follow": True, "audio_address": "0.0.0.0"}}
        patcher = self.app.mock.patch.object(self.m.luma_breakaway, "route", engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        response = self.client.post("/api/route_matrix", json={
            "decoder_ip": DEC_1, "encoder_ip": ENC_A, "mode": "video", "video_encoder_ip": ENC_A})
        self.assertTrue(fired, "the clear never happened: the test did not measure what it claims")
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["reason"], "inventory_changed")
        self.assert_clean("by a breakaway route that finished after the clear")

    def test_a_breakaway_result_is_not_written_onto_a_different_unit_that_took_the_address(self):
        other = dict(copy.deepcopy(DISPLAY_1), mac="02:00:5e:20:00:98", hostname="Replacement")

        def engine(*args, **kwargs):
            self.clear_silently()
            self.rediscover(other)
            return {"ok": True, "status": "verified", "mode": "video", "writes": [1], "video_stream": "s",
                    "after": {"video": "stream-from-the-old-request", "audio": "a", "follow": True,
                              "audio_address": "0.0.0.0"}}
        patcher = self.app.mock.patch.object(self.m.luma_breakaway, "route", engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        response = self.client.post("/api/route_matrix", json={
            "decoder_ip": DEC_1, "encoder_ip": ENC_A, "mode": "video", "video_encoder_ip": ENC_A})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        held = self.app.unit(DEC_1)
        self.assertEqual(held["mac"], "02:00:5e:20:00:98")
        self.assertEqual(held["dec"]["video"]["streamname"], "",
                         "the old request's result landed on another device")
        self.assertNotIn("routed_streamname", held)

    def test_a_scan_that_puts_another_device_at_the_address_mid_route_is_not_written_to(self):
        """No clear at all: the row at the address is replaced by a different device while the route runs."""
        other = dict(copy.deepcopy(DISPLAY_1), mac="02:00:5e:20:00:55", hostname="Swapped")

        def swap(ip, name):
            self.rediscover(other)
        self.dev.on_set = swap
        response = self.route()
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        held = self.app.unit(DEC_1)
        self.assertEqual(held["mac"], "02:00:5e:20:00:55")
        self.assertNotIn("routed_streamname", held)

    def test_a_unit_that_cannot_be_shown_to_be_the_same_is_not_written_after_a_clear(self):
        """No hardware address on record: after a clear nothing proves the address is the same unit."""
        nameless = dict(copy.deepcopy(DISPLAY_1), mac="")
        self.app.seed([nameless, SOURCE_A])

        def bounce(ip, name):
            self.clear_silently()
            self.rediscover(nameless)
        self.dev.on_set = bounce
        response = self.route()
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertNotIn("routed_streamname", self.app.unit(DEC_1))

    def test_the_route_result_is_not_written_onto_a_different_unit_that_took_the_address(self):
        other = dict(copy.deepcopy(DISPLAY_1), mac="02:00:5e:20:00:99", hostname="Replacement")

        def swap(ip, name):
            self.clear_silently()
            self.rediscover(other)
        self.dev.on_set = swap
        response = self.route()
        self.assertEqual(response.status_code, 409)
        held = self.app.unit(DEC_1)
        self.assertEqual(held["mac"], "02:00:5e:20:00:99")
        self.assertNotIn("routed_streamname", held, "the old request's result landed on another device")
        self.assertNotIn("routed_encoder_ip", held)

    def test_the_same_unit_rediscovered_mid_route_is_still_that_unit(self):
        again = copy.deepcopy(DISPLAY_1)

        def bounce(ip, name):
            self.clear_silently()
            self.rediscover(again)
        self.dev.on_set = bounce
        response = self.route()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        held = self.app.unit(DEC_1)
        self.assertEqual(held["mac"], DISPLAY_1["mac"])
        self.assertEqual(held["routed_streamname"], "stream-alpha")


class StaleBrowserStateTests(Base):
    def test_the_inventory_revision_is_served_and_moves_with_a_clear(self):
        before = self.epoch()
        self.assertEqual(self.epoch(), before, "reading the inventory must not change its revision")
        self.clear()
        self.assertNotEqual(self.epoch(), before)
        self.assertEqual(self.client.post("/api/refresh_matrix_state", json={}).get_json()["inventory_epoch"],
                         self.epoch())

    def test_a_route_from_before_the_clear_is_a_conflict_and_sends_nothing(self):
        token = self.epoch()
        self.clear()
        # the unit is back (rediscovery) but the page that holds `token` was built from the old inventory
        self.rediscover(DISPLAY_1)
        self.rediscover(SOURCE_A)
        self.dev.calls.clear()
        response = self.route(inventory_epoch=token)
        body = response.get_json()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(body["reason"], "stale_inventory")
        self.assertEqual(self.dev.calls, [], "a stale request reached a device")
        self.assertEqual(body["inventory_epoch"], self.epoch(), "the refusal carries the current revision")

    def test_a_stale_token_on_a_breakaway_route_is_a_conflict(self):
        token = self.epoch()
        self.clear()
        response = self.client.post("/api/route_matrix", json={
            "decoder_ip": DEC_1, "encoder_ip": ENC_A, "mode": "video", "inventory_epoch": token})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "stale_inventory")

    def test_the_current_token_routes_and_so_does_no_token(self):
        self.assertEqual(self.route(inventory_epoch=self.epoch()).status_code, 200)
        self.assertEqual(self.route().status_code, 200, "a page that sends no token must keep working")
        self.assertEqual(self.route(inventory_epoch=None).status_code, 200)

    def test_a_malformed_token_is_400(self):
        for bad in (7, [], {}, "", "x" * 200, True):
            response = self.route(inventory_epoch=bad)
            self.assertEqual(response.status_code, 400, repr(bad))
            self.assertEqual(response.get_json()["reason"], "invalid_epoch")

    def test_a_token_from_another_run_of_the_server_is_stale(self):
        response = self.route(inventory_epoch="deadbeef.0")
        self.assertEqual(response.status_code, 409)

    def test_removing_one_unit_also_retires_the_old_token(self):
        token = self.epoch()
        self.client.post("/api/remove_units", json={"ips": [DEC_2]})
        self.assertEqual(self.route(inventory_epoch=token).status_code, 409)
        self.assertEqual(self.route(inventory_epoch=self.epoch()).status_code, 200)


class RediscoveryTests(Base):
    def test_clear_then_rediscovery_works_and_is_not_the_stale_request(self):
        stale = self.epoch()
        self.clear()
        self.assertEqual(self.route().status_code, 404)
        self.rediscover(DISPLAY_1)
        self.rediscover(SOURCE_A)
        self.dev.decoder_stream[DEC_1] = ""
        self.assertEqual(self.route(inventory_epoch=stale).status_code, 409)
        fresh = self.route(inventory_epoch=self.epoch())
        self.assertEqual(fresh.status_code, 200, fresh.get_data(as_text=True))
        held = self.app.unit(DEC_1)
        self.assertEqual(held["mac"], DISPLAY_1["mac"])
        self.assertEqual(held["routed_streamname"], "stream-alpha")
        self.assertFalse(any(is_partial(u) for u in self.inventory().values()))

    def test_a_different_unit_at_the_old_address_is_not_the_cleared_one(self):
        self.clear()
        newcomer = dict(copy.deepcopy(DISPLAY_1), mac="02:00:5e:20:00:77")
        self.rediscover(newcomer)
        self.rediscover(SOURCE_A)
        self.assertEqual(self.route().status_code, 200)
        held = self.app.unit(DEC_1)
        self.assertEqual(held["mac"], "02:00:5e:20:00:77")
        self.assertEqual(identity(held), "02:00:5e:20:00:77")


class ConcurrentStaleRequestTests(Base):
    def test_many_stale_requests_at_once_all_fail_truthfully_and_create_nothing(self):
        self.clear()
        self.dev.calls.clear()
        results, errors = [], []
        barrier = threading.Barrier(24)

        def one(i):
            try:
                barrier.wait(5)
                client = self.m.APP.test_client()
                body = {"decoder_ip": DEC_1 if i % 2 else DEC_2, "encoder_ip": ENC_A}
                if i % 3 == 0:
                    body["mode"] = "video"
                results.append(client.post("/api/route_matrix", json=body).status_code)
            except Exception as exc:       # pragma: no cover - reported below
                errors.append(repr(exc))
        threads = [threading.Thread(target=one, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(set(results)), [404], results)
        self.assertEqual(self.dev.calls, [])
        self.assert_clean()

    def test_clear_racing_a_stream_of_routes_never_leaves_a_partial_record(self):
        stop = threading.Event()
        statuses = []

        def hammer():
            client = self.m.APP.test_client()
            while not stop.is_set():
                statuses.append(client.post("/api/route_matrix", json={
                    "decoder_ip": DEC_1, "encoder_ip": ENC_A}).status_code)
        workers = [threading.Thread(target=hammer) for _ in range(4)]
        for w in workers:
            w.start()
        try:
            for _ in range(25):
                self.clear_silently()
                self.rediscover(DISPLAY_1)
                self.rediscover(SOURCE_A)
            self.clear_silently()
        finally:
            stop.set()
            for w in workers:
                w.join(30)
        self.assertTrue(statuses)
        self.assertFalse([s for s in statuses if s >= 500], sorted(set(statuses)))
        self.assert_clean("after racing routes")


class PollingAfterClearTests(Base):
    def test_a_poll_that_lands_after_the_clear_does_not_bring_the_unit_back(self):
        fired = []

        def clear_during_poll(ip, method, params=None, timeout=None):
            if not fired:
                fired.append(ip)
                self.clear_silently()
            return {"result": {"streamname": "late-" + method}}
        self.app.patch("_ws_call_auth", clear_during_poll)
        self.m._refresh_matrix_state_core(force=True)
        self.assertTrue(fired, "the clear never happened: the test did not measure what it claims")
        self.assert_clean("by a sweep that finished after the clear")

    def test_a_stale_poll_result_is_not_applied_to_a_rediscovered_record(self):
        fired = []
        original = self.m._ws_call_auth

        def clear_and_rediscover(ip, method, params=None, timeout=None):
            if not fired:
                fired.append(ip)
                self.clear_silently()
                fresh = copy.deepcopy(DISPLAY_1)
                fresh["dec"] = {"video": {"streamname": "fresh"}}
                self.rediscover(fresh)
            return {"result": {"streamname": "stale-poll-result"}}
        self.app.patch("_ws_call_auth", clear_and_rediscover)
        self.m._refresh_matrix_state_core(force=True)
        self.assertTrue(fired)
        held = self.inventory().get(DEC_1)
        if held is not None:
            self.assertEqual(held["dec"]["video"]["streamname"], "fresh")


class DerivedStateTests(Base):
    def test_state_remembered_by_address_goes_with_the_inventory(self):
        self.m._device_credential_cache["ip:" + DEC_1] = "fallback"
        self.m._device_credential_cache["mac:" + DISPLAY_1["mac"].replace(":", "")] = "fallback"
        self.m._preview_fresh_at["thumb:" + DEC_1] = 1.0
        self.clear()
        self.assertFalse([k for k in self.m._device_credential_cache if k.startswith("ip:")],
                         "a working-credential slot kept for an address that was cleared")
        self.assertTrue([k for k in self.m._device_credential_cache if k.startswith("mac:")],
                        "identity-keyed state is about the hardware and survives a clear")
        self.assertEqual(self.m._preview_fresh_at, {})


class UserRouteAfterClearTests(unittest.TestCase):
    """The restricted User's routes name identities and are resolved on the server."""

    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.dev = self.app.devices
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.store = self.remote.store
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions(
            [identity(SOURCE_A)], [identity(DISPLAY_1)], [], [], True, False))
        self.user = self.remote.sign_in("alex", USER_PASSWORD)
        self.body = {"source": identity(SOURCE_A), "destination": identity(DISPLAY_1)}

    def clear_silently(self):
        with self.m.cache_lock:
            self.m.cache_generation += 1
            self.m.cache_units.clear()

    def test_a_route_to_a_cleared_display_is_404_and_creates_nothing(self):
        self.clear_silently()
        self.dev.calls.clear()
        response = self.remote.post(self.user, "/api/user/route", self.body)
        self.assertEqual(response.status_code, 404, response.get_data(as_text=True))
        self.assertEqual(self.app.unit(DEC_1), None)
        self.assertEqual(self.dev.calls, [])

    def test_a_standard_route_to_a_cleared_display_in_a_group_or_multi_is_not_5xx(self):
        self.clear_silently()
        for path, body in (("/api/user/route_multi", {"source": identity(SOURCE_A),
                                                      "destinations": [identity(DISPLAY_1)]}),
                           ("/api/user/route", dict(self.body, audio_follows_video=False))):
            response = self.remote.post(self.user, path, body)
            self.assertLess(response.status_code, 500, "%s: %s" % (path, response.get_data(as_text=True)))
        with self.m.cache_lock:
            self.assertEqual(dict(self.m.cache_units), {})

    def test_clear_while_a_user_route_is_pending(self):
        fired = []

        def clear_at_the_write(ip, name):
            if not fired:
                fired.append(ip)
                self.clear_silently()
        self.dev.on_set = clear_at_the_write
        response = self.remote.post(self.user, "/api/user/route", self.body)
        self.assertTrue(fired)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["result"], "conflict")
        with self.m.cache_lock:
            self.assertEqual(dict(self.m.cache_units), {}, "the user route recreated the cleared display")


class RealServerTests(Base):
    """The same contract over a real listener: an actual HTTP round trip on loopback."""

    def test_stale_route_over_http_after_clear(self):
        import json
        import urllib.error
        import urllib.request
        from werkzeug.serving import make_server
        server = make_server("127.0.0.1", 0, self.m.APP, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_port

        def call(path, body=None):
            data = json.dumps(body if body is not None else {}).encode()
            request = urllib.request.Request(base + path, data=data,
                                             headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=10) as reply:
                    return reply.status, json.loads(reply.read())
            except urllib.error.HTTPError as err:
                try:
                    return err.code, json.loads(err.read() or b"{}")
                finally:
                    err.close()
        status, ok = call("/api/route_matrix", {"decoder_ip": DEC_1, "encoder_ip": ENC_A})
        self.assertEqual(status, 200, ok)
        status, _ = call("/api/clear_units")
        self.assertEqual(status, 200)
        status, body = call("/api/route_matrix", {"decoder_ip": DEC_1, "encoder_ip": ENC_A})
        self.assertEqual((status, body["reason"]), (404, "unknown_unit"))
        self.assert_clean("over HTTP")


if __name__ == "__main__":
    unittest.main()
