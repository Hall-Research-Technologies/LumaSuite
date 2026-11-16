"""Video Wall Mode per decoder, and "Apply to Entire Wall" leaving every member in wall mode.

Wall mode is a state of the DECODER (StandardVideoWallInfo.vmenable, with the position in
vwmatrix). Saved wall membership says who should take part; it is never evidence of the flag.
Everything here runs against the stub device model in `_support.FakeDevices`; nothing reaches
a device (tests/_fence.py blocks every non-loopback socket regardless).
"""
import copy
import unittest

import _support
from _support import (DISPLAY_1, DISPLAY_2, DISPLAY_3, SOURCE_A, RealApp, saved_wall, wall_device_id)

import luma_access

WALL = "wall-aaaaaaaaaaaa"
D1, D2 = DISPLAY_1["ip"], DISPLAY_2["ip"]


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.app.seed()
        self.m = self.app.m
        self.client = self.m.APP.test_client()
        self._saved = {}
        self.app.patch("_load_video_wall_state", lambda: copy.deepcopy(self._saved) if self._saved
                       else {"walls": [], "ignored_candidates": []})
        self.app.patch("_save_video_wall_state", lambda data: self._saved.update(copy.deepcopy(data)))
        self.devices = self.app.devices

    def wall(self, members=(DISPLAY_1, DISPLAY_2), **extra):
        wall = saved_wall(WALL, "Atrium Wall", list(members), source_id=wall_device_id(SOURCE_A))
        wall.update(extra)
        self._saved.update({"walls": [wall], "ignored_candidates": []})
        return wall

    def apply(self, wall=None):
        return self.client.post("/api/video_wall/apply", json={"wall": wall or self.wall()}).get_json()

    def mode(self, unit, **body):
        body["decoder_id"] = wall_device_id(unit)
        return self.client.post("/api/video_wall/decoder_mode", json=body)

    def writes(self, ip=None):
        return [w for w in self.devices.writes() if ip is None or w[0] == ip]

    def offline(self, ip):
        import luma_liveness
        with self.m.cache_lock:
            unit = self.m.cache_units[ip]
            luma_liveness.record(unit, False)
            luma_liveness.record(unit, False)


class ApplyEntireWallTests(Base):
    def test_a_decoder_whose_wall_mode_is_disabled_is_enabled_by_apply(self):
        self.assertNotIn(D1, self.devices.wall_info, "precondition: the stub decoder holds the default, wall mode off")
        data = self.apply()
        self.assertTrue(data["ok"])
        self.assertEqual((data["status"], data["members"], data["succeeded"]), ("complete", 2, 2))
        self.assertEqual(self.devices.wall_info[D1], {"vmenable": True, "vwmatrix": "1_2_1_1"})
        self.assertEqual(self.devices.wall_info[D2], {"vmenable": True, "vwmatrix": "1_2_1_2"})
        for unit in (DISPLAY_1, DISPLAY_2):
            mode = data["results"][wall_device_id(unit)]["wall_mode"]
            self.assertEqual((mode["before"], mode["after"], mode["enabled_by_apply"]), (False, True, True))

    def test_the_state_is_read_back_after_the_write_not_assumed_from_the_reply(self):
        self.apply()
        for ip in (D1, D2):
            methods = self.devices.methods(ip)
            last_set = max(i for i, m in enumerate(methods) if m == "StandardVideoWallInfo.Set")
            self.assertIn("StandardVideoWallInfo.Get", methods[last_set + 1:], "wall mode was read again after the last write")

    def test_a_member_that_accepts_the_write_and_does_not_hold_it_is_reported_partial_by_name(self):
        self.devices.wall_ignores.add(D2)
        data = self.apply()
        self.assertFalse(data["ok"])
        self.assertEqual((data["status"], data["members"], data["succeeded"]), ("partial", 2, 1))
        failed = data["failed_members"]
        self.assertEqual([f["label"] for f in failed], ["Lobby Two"])
        self.assertIn("verify wall mode", failed[0]["reason"])
        self.assertEqual(self.devices.wall_info[D1]["vmenable"], True, "the other member is still configured")

    def test_an_unreadable_member_is_never_counted_verified(self):
        self.devices.unreachable.add(D2)
        data = self.apply()
        self.assertEqual((data["status"], data["succeeded"]), ("partial", 1))
        self.assertFalse(data["results"][wall_device_id(DISPLAY_2)]["ok"])

    def test_an_offline_member_is_not_written_and_the_rest_continue(self):
        self.offline(D2)
        data = self.apply()
        self.assertEqual(data["status"], "partial")
        self.assertEqual(self.writes(D2), [])
        self.assertTrue(data["results"][wall_device_id(DISPLAY_1)]["ok"])
        self.assertEqual(data["failed_members"][0]["reason"], "display is offline")

    def test_every_member_failing_is_failed_not_partial(self):
        self.devices.wall_ignores.update({D1, D2})
        data = self.apply()
        self.assertEqual((data["status"], data["succeeded"]), ("failed", 0))

    def test_an_override_cell_is_verified_as_disabled_not_forced_on(self):
        wall = self.wall()
        wall["cells"][1].update({"source_mode": "override", "override_source_id": wall_device_id(SOURCE_A)})
        self.devices.wall_info[D2] = {"vmenable": True, "vwmatrix": "1_2_1_2"}
        data = self.apply(wall)
        self.assertEqual(data["status"], "complete")
        self.assertEqual(self.devices.wall_info[D2]["vmenable"], False)
        self.assertEqual(self.devices.wall_info[D1]["vmenable"], True)

    def test_an_empty_wall_is_not_complete(self):
        data = self.apply(saved_wall(WALL, "Empty", []))
        self.assertEqual((data["ok"], data["status"]), (False, "empty"))


class DecoderModeEndpointTests(Base):
    def test_reading_reports_the_decoder_not_the_saved_membership(self):
        self.wall()                                    # D1 is a saved member; the decoder says wall mode is off
        body = self.mode(DISPLAY_1).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["wall_mode"]["enabled"], False)
        self.devices.wall_info[D1] = {"vmenable": True, "vwmatrix": "1_2_1_1"}
        self.assertEqual(self.mode(DISPLAY_1).get_json()["wall_mode"]["enabled"], True)
        self.assertEqual(self.writes(), [], "a read writes nothing")

    def test_an_unreadable_decoder_is_unknown_not_off(self):
        self.devices.unreachable.add(D1)
        response = self.mode(DISPLAY_1)
        self.assertEqual(response.status_code, 502)
        self.assertIsNone(response.get_json()["wall_mode"])

    def test_a_garbled_flag_is_unreadable_not_coerced(self):
        original = self.devices.call
        self.app.patch("_ws_call_auth", lambda ip, method, params=None, timeout=None:
                       {"result": {"vmenable": "yes", "vwmatrix": "1_1_1_1"}} if method == "StandardVideoWallInfo.Get"
                       else original(ip, method, params, timeout))
        response = self.mode(DISPLAY_1)
        self.assertEqual(response.status_code, 502)
        self.assertIsNone(response.get_json()["wall_mode"])

    def test_enabling_writes_through_the_wall_info_and_is_verified_by_read_back(self):
        self.wall()
        response = self.mode(DISPLAY_1, enabled=True, wall_id=WALL)
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["wall_mode"], {"enabled": True, "vwmatrix": "1_2_1_1"})
        self.assertEqual(self.devices.wall_info[D1], {"vmenable": True, "vwmatrix": "1_2_1_1"})
        self.assertEqual(self.writes(D2), [], "only the addressed decoder is written")

    def test_disabling_keeps_the_decoders_position_and_is_verified(self):
        self.devices.wall_info[D1] = {"vmenable": True, "vwmatrix": "2_2_2_1"}
        body = self.mode(DISPLAY_1, enabled=False).get_json()
        self.assertEqual(body["wall_mode"]["enabled"], False)
        self.assertEqual(self.devices.wall_info[D1], {"vmenable": False, "vwmatrix": "2_2_2_1"})

    def test_a_write_the_decoder_does_not_hold_is_a_failure_that_shows_the_real_state(self):
        self.devices.wall_ignores.add(D1)
        response = self.mode(DISPLAY_1, enabled=True)
        body = response.get_json()
        self.assertEqual((response.status_code, body["ok"], body["reason"]), (502, False, "not_verified"))
        self.assertEqual(body["wall_mode"]["enabled"], False, "what the decoder holds, not what was asked")

    def test_an_unreadable_decoder_is_never_written_blind(self):
        self.devices.unreachable.add(D1)
        response = self.mode(DISPLAY_1, enabled=True)
        self.assertEqual((response.status_code, response.get_json()["reason"]), (502, "unreadable"))
        self.assertEqual(self.writes(D1), [])

    def test_an_offline_decoder_is_not_written(self):
        self.offline(D1)
        response = self.mode(DISPLAY_1, enabled=True)
        self.assertEqual((response.status_code, response.get_json()["reason"]), (409, "unit_offline"))
        self.assertEqual(self.writes(D1), [])

    def test_the_flag_must_be_a_real_boolean_and_the_decoder_known(self):
        for bad in ("true", 1, "false", None):
            self.assertEqual(self.mode(DISPLAY_1, enabled=bad).status_code, 400, bad)
        self.assertEqual(self.client.post("/api/video_wall/decoder_mode", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/video_wall/decoder_mode", json={"decoder_id": "mac:ff:ff:ff:ff:ff:ff"}).status_code, 404)
        self.assertEqual(self.writes(), [])

    def test_the_route_is_administrative(self):
        self.assertIs(luma_access.ROUTES["/api/video_wall/decoder_mode"], luma_access.Access.ADMIN)


if __name__ == "__main__":
    unittest.main()
