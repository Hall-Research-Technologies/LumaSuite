"""A unit's own thumbnail switch: the parser, the verified write, the endpoint, the scan and Settings' firmware rows.

Everything runs against an in-memory device. A mutating call (`ThumbnailStatus.Set`) is recorded and the
stub is installed through the shared test base, so nothing here can reach hardware or the Internet.
"""
import copy
import unittest

import _support
from _support import RealApp

import luma_thumbnail as th


class ParseTests(unittest.TestCase):
    def test_only_a_plain_boolean_is_a_state(self):
        self.assertIs(th.parse_state({"result": {"thumbnailenable": True}}), True)
        self.assertIs(th.parse_state({"result": {"thumbnailenable": False}}), False)
        for bad in (None, {}, {"result": {}}, {"result": []}, {"error": {"code": 1}},
                    {"result": {"thumbnailenable": 1}}, {"result": {"thumbnailenable": "true"}},
                    {"result": {"thumbnailenable": None}}, {"result": {"thumbnailenable": True}, "error": {}}, "x"):
            self.assertIsNone(th.parse_state(bad), bad)

    def test_a_switcher_is_never_asked(self):
        for role, model in (("switcher", ""), ("Decoder/Switcher", "AT-OME-CS31"), ("decoder", "HW-OME-CS31")):
            self.assertFalse(th.supported(role, model))
        self.assertTrue(th.supported("Encoder", "HW-LUMA-E4211"))
        self.assertTrue(th.supported("decoder", "HW-LUMA-D4111"))


class Device:
    """Holds the switch; holds=False accepts a Set and ignores it; lose_reply answers nothing but acts."""

    def __init__(self, state=True, holds=True, lose_reply=False, readable=True):
        self.state, self.holds, self.lose_reply, self.readable = state, holds, lose_reply, readable
        self.calls = []

    def __call__(self, ip, method, params=None, timeout=None):
        self.calls.append((method, copy.deepcopy(params)))
        if method == th.GET_METHOD:
            return {"result": {"thumbnailenable": self.state}} if self.readable else None
        if method == th.SET_METHOD:
            if self.holds:
                self.state = params["thumbnailenable"]
            return None if self.lose_reply else {"result": {"thumbnailenable": params["thumbnailenable"]}}
        return {"result": {}}

    def sets(self):
        return [p for (m, p) in self.calls if m == th.SET_METHOD]


class WriteVerifiedTests(unittest.TestCase):
    def test_success_is_the_readback_agreeing(self):
        dev = Device(True)
        r = th.write_verified(dev, "198.18.0.1", False, 2)
        self.assertEqual((r["ok"], r["enabled"], r["verified"]), (True, False, True))
        self.assertEqual(dev.sets(), [{"thumbnailenable": False}])

    def test_an_acknowledgement_the_unit_does_not_hold_is_a_failure(self):
        dev = Device(True, holds=False)
        r = th.write_verified(dev, "198.18.0.1", False, 2)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "not_held")
        self.assertIs(r["enabled"], True)

    def test_a_lost_reply_is_answered_by_reading_the_unit(self):
        dev = Device(True, lose_reply=True)
        r = th.write_verified(dev, "198.18.0.1", False, 2)
        self.assertTrue(r["ok"])
        self.assertEqual(len(dev.sets()), 1, "a write is never repeated")

    def test_a_lost_reply_that_did_not_act_is_a_failure(self):
        dev = Device(True, holds=False, lose_reply=True)
        r = th.write_verified(dev, "198.18.0.1", False, 2)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_answer")

    def test_a_unit_that_cannot_be_read_is_not_written(self):
        dev = Device(True, readable=False)
        r = th.write_verified(dev, "198.18.0.1", False, 2)
        self.assertEqual(r["reason"], "unreadable")
        self.assertEqual(dev.sets(), [])

    def test_a_unit_already_in_the_wanted_state_is_not_written(self):
        dev = Device(False)
        r = th.write_verified(dev, "198.18.0.1", False, 2)
        self.assertTrue(r["ok"])
        self.assertFalse(r["changed"])
        self.assertEqual(dev.sets(), [])

    def test_only_a_boolean_may_be_written(self):
        dev = Device(True)
        for bad in ("false", 0, None, [], {}):
            self.assertEqual(th.write_verified(dev, "198.18.0.1", bad, 2)["reason"], "invalid")
        self.assertEqual(dev.calls, [])


ENC = {"ip": "198.18.0.31", "mac": "02:00:5e:00:31:01", "model": "HW-LUMA-E4211", "type": "encoder", "role": "Encoder",
       "hostname": "enc", "version": "V1.0.2.3", "status": "idle",
       "enc": {"video": {"streamname": "enc-stream"}}}
DEC = {"ip": "198.18.0.32", "mac": "02:00:5e:00:32:01", "model": "HW-LUMA-D4111", "type": "decoder", "role": "Decoder",
       "hostname": "dec", "version": "V1.0.2.3", "status": "idle",
       "dec": {"video": {"streamname": ""}}}
SWITCHER = {"ip": "198.18.0.33", "mac": "02:00:5e:00:33:01", "model": "HW-OME-CS31", "type": "switcher", "role": "Switcher",
            "hostname": "sw", "version": "V1.0.0.0", "status": "idle",
            "dec": {"video": {"streamname": ""}}}


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.dev = Device(True)
        self.app.patch("_ws_call_auth", self.dev)
        self.app.seed([copy.deepcopy(u) for u in (ENC, DEC, SWITCHER)])
        self.client = self.app.m.APP.test_client()

    def post(self, body):
        return self.client.post("/api/thumbnail", json=body)

    def test_it_writes_reads_back_and_records_what_the_unit_holds(self):
        r = self.post({"ip": ENC["ip"], "mac": ENC["mac"], "enable": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "enabled": False, "verified": True})
        self.assertIs(self.app.unit(ENC["ip"])["thumbnail_enabled"], False)

    def test_the_same_commands_serve_encoders_and_decoders(self):
        for unit in (ENC, DEC):
            self.dev.calls.clear()
            self.dev.state = True
            self.assertEqual(self.post({"ip": unit["ip"], "mac": unit["mac"], "enable": False}).status_code, 200)
            self.assertEqual([m for (m, _p) in self.dev.calls], [th.GET_METHOD, th.SET_METHOD, th.GET_METHOD])

    def test_a_switcher_is_refused_and_nothing_is_sent(self):
        r = self.post({"ip": SWITCHER["ip"], "mac": SWITCHER["mac"], "enable": False})
        self.assertEqual((r.status_code, r.get_json()["reason"]), (409, "unsupported"))
        self.assertEqual(self.dev.calls, [])

    def test_malformed_input_is_400_and_unknown_is_404_and_a_moved_unit_is_409(self):
        cases = [({}, 400), ({"ip": ENC["ip"], "mac": ENC["mac"]}, 400),
                 ({"ip": ENC["ip"], "mac": ENC["mac"], "enable": "false"}, 400),
                 ({"ip": "not-an-ip", "mac": ENC["mac"], "enable": True}, 400),
                 ({"ip": ENC["ip"], "mac": "nope", "enable": True}, 400),
                 ({"ip": "198.18.0.99", "mac": ENC["mac"], "enable": True}, 404),
                 ({"ip": ENC["ip"], "mac": DEC["mac"], "enable": True}, 409)]
        for body, status in cases:
            self.assertEqual(self.post(body).status_code, status, body)
        self.assertEqual(self.dev.calls, [], "none of those reached a unit")

    def test_the_mac_is_normalised_but_must_be_the_recorded_one(self):
        r = self.post({"ip": ENC["ip"], "mac": ENC["mac"].upper().replace(":", "-"), "enable": False})
        self.assertEqual(r.status_code, 200)

    def test_a_write_the_unit_does_not_hold_reports_what_it_holds_and_the_cache_agrees_with_the_unit(self):
        self.dev.holds = False
        r = self.post({"ip": ENC["ip"], "mac": ENC["mac"], "enable": False})
        self.assertEqual(r.status_code, 409)
        self.assertIs(r.get_json()["enabled"], True)
        self.assertIs(self.app.unit(ENC["ip"])["thumbnail_enabled"], True)

    def test_a_unit_that_does_not_answer_is_an_upstream_condition_not_a_server_fault(self):
        self.dev.readable = False
        r = self.post({"ip": ENC["ip"], "mac": ENC["mac"], "enable": False})
        self.assertEqual((r.status_code, r.get_json()["reason"]), (502, "unreadable"))
        self.assertNotIn("thumbnail_enabled", self.app.unit(ENC["ip"]))

    def test_the_route_is_administrative(self):
        import luma_access
        self.assertEqual(luma_access.classify("/api/thumbnail", "POST"), luma_access.Access.ADMIN)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.dev = Device(False)
        self.base = lambda ip: dict(ENC, ip=ip)
        self.app.patch("_tcp_probe", lambda ip, port: True)
        self.app.patch("_ws_get_minimal", lambda ip: self.base(ip))
        self.app.patch("_query_encoder_details", lambda ip, timeout: {})
        self.app.patch("_ws_call_auth", self.dev)

    def test_the_scan_learns_the_switch_while_it_is_already_reading_the_unit(self):
        self.assertIs(self.app.m._scan_one("198.18.0.40")["thumbnail_enabled"], False)

    def test_an_unread_switch_adds_nothing_so_nothing_verified_is_erased(self):
        self.dev.readable = False
        self.assertNotIn("thumbnail_enabled", self.app.m._scan_one("198.18.0.41"))

    def test_a_switcher_is_not_asked(self):
        self.base = lambda ip: dict(SWITCHER, ip=ip)
        self.app.m._scan_one("198.18.0.42")
        self.assertNotIn(th.GET_METHOD, [m for (m, _p) in self.dev.calls])


class FirmwareRowsTests(unittest.TestCase):
    """Settings' summary comes from what is already known: the endpoint makes no request of its own."""

    def setUp(self):
        self.app = RealApp(self)
        self.app.patch("_ensure_hall_firmware_lookup_started", lambda: None)
        self.app.patch("_hall_firmware_cache", {"hw-luma-e4211": {"version": "V1.0.3.0", "release_date": "2026-01-01",
                                                                  "checked_at": 1.0}})
        self.app.patch("_hall_firmware_fetch_state", "ready")
        self.app.patch("_hall_firmware_fetched_at", 1000.0)
        self.app.patch("_hall_slug_for_model",
                       lambda model: "hw-luma-e4211" if "E4211" in str(model or "").upper() else None)
        self.app.seed([copy.deepcopy(dict(ENC, ip="198.18.0.51", version="V1.0.2.3")),
                       copy.deepcopy(dict(ENC, ip="198.18.0.52", mac="02:00:5e:00:31:02", version="V1.0.3.0")),
                       copy.deepcopy(DEC)])
        self.client = self.app.m.APP.test_client()

    def test_one_row_per_model_with_the_most_pressing_state(self):
        r = self.client.get("/api/firmware_status").get_json()
        rows = {row["model"]: row for row in r["model_rows"]}
        e = rows["HW-LUMA-E4211"]
        self.assertEqual((e["units"], e["state"], e["latest"], e["installed"]),
                         (2, "update_available", "V1.0.3.0", ["V1.0.2.3", "V1.0.3.0"]))
        self.assertNotIn("HW-LUMA-D4111", rows, "a model the catalogue does not cover is not a row")
        self.assertEqual(r["hall_lookup"]["fetched_at"], 1000.0)


if __name__ == "__main__":
    unittest.main()
