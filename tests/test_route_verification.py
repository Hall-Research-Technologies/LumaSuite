"""The A/V route is verified from the device, never from what was asked or cached."""
import unittest

from _support import DISPLAY_1, SOURCE_A, SOURCE_B, RealApp

ENC_A, ENC_B = SOURCE_A["ip"], SOURCE_B["ip"]
DEC_1 = DISPLAY_1["ip"]


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.dev = self.app.devices
        self.app.seed()
        self.client = self.m.APP.test_client()

    def route(self, decoder=DEC_1, encoder=ENC_A):
        return self.client.post("/api/route_matrix", json={"decoder_ip": decoder, "encoder_ip": encoder})


class VerifiedRouteTests(Base):
    def test_a_route_the_display_holds_is_a_success_with_a_confirmed_read_back(self):
        body = self.route().get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["verified"])
        self.assertEqual(self.dev.decoder_stream[DEC_1], "stream-alpha")
        self.assertEqual(self.app.unit(DEC_1)["routed_streamname"], "stream-alpha")
        self.assertEqual(self.app.unit(DEC_1)["dec"]["video"]["streamname"], "stream-alpha")

    def test_the_response_shape_the_matrix_page_reads_is_unchanged(self):
        body = self.route().get_json()
        self.assertEqual(set(body), {"ok", "verified", "set_result", "get_result"})
        self.assertIn("result", body["get_result"])

    def test_an_accepted_command_the_display_does_not_hold_is_a_failure(self):
        self.dev.ignores_set.add(DEC_1)
        response = self.route()
        body = response.get_json()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(body["ok"])
        self.assertFalse(body["verified"])
        self.assertEqual(body["status"], "not_verified")

    def test_a_unit_that_does_not_answer_is_an_upstream_condition_not_a_server_fault(self):
        # Found by the soak: the plain route answered 500 whenever a unit stopped answering, so a
        # routine offline display read as a crash. The breakaway modes already said 503 and 502.
        self.dev.unreachable.add(DEC_1)
        response = self.route()
        body = response.get_json()
        self.assertIn(response.status_code, (502, 503),
                      "a display that is not there answered %s" % response.status_code)
        self.assertFalse(body["ok"])
        self.assertFalse(body["verified"])

    def test_a_failed_verification_never_tells_the_cache_the_route_happened(self):
        self.dev.ignores_set.add(DEC_1)
        self.dev.decoder_stream[DEC_1] = "stream-bravo"        # what the display really holds
        self.route()
        unit = self.app.unit(DEC_1)
        self.assertNotIn("routed_streamname", unit)
        self.assertNotIn("routed_encoder_ip", unit)
        # and the cache now agrees with the device, not with what was requested
        self.assertEqual(unit["dec"]["video"]["streamname"], "stream-bravo")

    def test_the_cache_is_not_written_before_the_device_has_been_read_back(self):
        seen = {}

        def at_write(ip, name):
            seen["routed_before_write"] = "routed_streamname" in (self.app.unit(DEC_1) or {})

        self.dev.on_set = at_write
        self.route()
        self.assertFalse(seen["routed_before_write"])
        # and the read-back came after the write, from the device
        methods = self.dev.methods(DEC_1)
        self.assertLess(methods.index("SelectVideoStream.Set"), methods.index("SelectVideoStream.Get"))

    def test_a_display_that_is_slow_to_report_is_given_a_few_reads(self):
        self.dev.readback_script[DEC_1] = ["", "stream-alpha"]    # stale, then right
        self.assertTrue(self.route().get_json()["ok"])
        self.assertEqual(self.dev.methods(DEC_1).count("SelectVideoStream.Get"), 2)

    def test_the_number_of_confirmation_reads_is_bounded(self):
        self.dev.ignores_set.add(DEC_1)
        self.route()
        self.assertEqual(self.dev.methods(DEC_1).count("SelectVideoStream.Get"),
                         self.m.ROUTE_READBACK_ATTEMPTS)

    def test_a_write_is_never_repeated_to_get_a_confirmation(self):
        self.dev.ignores_set.add(DEC_1)
        self.route()
        self.assertEqual(self.dev.methods(DEC_1).count("SelectVideoStream.Set"), 1)

    def test_a_display_that_cannot_be_read_back_is_unreachable_not_a_success(self):
        self.dev.readback_script[DEC_1] = [None, None, None]
        result = self.m._route_av(DEC_1, ENC_A)
        self.assertFalse(result["ok"])
        self.assertIn(result["status"], ("unreachable", "not_verified"))

    def test_an_encoder_that_does_not_answer_is_reported_and_nothing_is_written(self):
        self.dev.unreachable.add(ENC_A)
        result = self.m._route_av(DEC_1, ENC_A)
        self.assertEqual((result["ok"], result["status"]), (False, "unreachable"))
        self.assertEqual(self.dev.writes(), [])

    def test_a_restricted_route_never_falls_back_to_a_cached_stream_name(self):
        self.dev.unreachable.add(ENC_A)
        self.assertFalse(self.m._route_av(DEC_1, ENC_A)["ok"])             # users: live state only
        self.assertEqual(self.dev.writes(), [])

    def test_the_administrators_matrix_keeps_its_cached_fallback(self):
        self.dev.unreachable.add(ENC_A)
        self.dev.calls.clear()
        # unreachable encoder + cached stream: the administrator's route still proceeds,
        # and is still verified from the decoder.
        result = self.m._route_av(DEC_1, ENC_A, allow_cached_stream=True)
        self.assertEqual(result["streamname"], "stream-alpha")
        self.assertTrue(result["ok"])

    def test_audio_is_reported_as_following_the_video_selection(self):
        result = self.m._route_av(DEC_1, ENC_A, check_audio=True)
        self.assertEqual(result["audio"], "follows")
        self.assertEqual(self.dev.writes(), [(DEC_1, "SelectVideoStream.Set")])   # no separate audio write

    def test_a_display_not_set_to_follow_video_is_reported_not_assumed(self):
        self.dev.follows_video[DEC_1] = False
        result = self.m._route_av(DEC_1, ENC_A, check_audio=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["audio"], "not_following")

    def test_the_administrators_route_does_not_pay_for_the_audio_read(self):
        self.route()
        self.assertNotIn("SelectAudioStream.Get", self.dev.methods(DEC_1))

    def test_a_route_does_not_send_any_audio_command(self):
        self.m._route_av(DEC_1, ENC_A, check_audio=True)
        self.assertFalse([m for _, m in self.dev.writes() if "Audio" in m])

    def test_bad_input_is_a_400_and_touches_nothing(self):
        for body in ({}, {"decoder_ip": "nope", "encoder_ip": ENC_A}, {"decoder_ip": DEC_1}):
            self.assertEqual(self.client.post("/api/route_matrix", json=body).status_code, 400)
        self.assertEqual(self.dev.calls, [])


class SwitcherRouteTests(Base):
    def setUp(self):
        super().setUp()
        with self.m.cache_lock:
            self.m.cache_units[DEC_1]["type"] = "switcher"

    def test_a_switcher_is_verified_the_same_way(self):
        body = self.route().get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(self.dev.decoder_stream[DEC_1], "stream-alpha")

    def test_a_switcher_that_does_not_hold_the_stream_is_a_failure(self):
        self.dev.ignores_set.add(DEC_1)
        self.assertEqual(self.route().status_code, 502)
        self.assertNotIn("routed_streamname", self.app.unit(DEC_1))

    def test_a_switcher_audio_follow_is_not_asked(self):
        result = self.m._route_av(DEC_1, ENC_A, check_audio=True)
        self.assertEqual(result["audio"], "unknown")


class UserRouteUsesTheSameEngineTests(Base):
    def test_the_user_route_is_the_verified_engine_with_live_state_only(self):
        import inspect
        source = inspect.getsource(self.m._route_av_for_users)
        self.assertIn("allow_cached_stream=False", source)
        self.assertIn("check_audio=True", source)

    def test_the_user_route_invalidates_the_shared_observation_afterwards(self):
        self.m._observe_cache.get("matrix", lambda: 1)
        self.m._route_av_for_users(DEC_1, ENC_A)
        sentinel = []
        self.m._observe_cache.get("matrix", lambda: sentinel.append(1) or 2)
        self.assertEqual(sentinel, [1])                       # recomputed, not served stale


if __name__ == "__main__":
    unittest.main()
