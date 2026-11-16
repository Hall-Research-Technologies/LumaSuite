"""AV LAN link speed and the LLDP neighbour: the parser, the read and how the scan keeps it.

The fixtures have the exact shape captured from a real encoder and decoder (the device's own spelling
`ehternet`, a speed STRING, an `lldp` object, a second port that must never be mistaken for the AV LAN,
and a down link with no speed at all), with every address replaced by a documentation address. Their
lengths are asserted before use, so a fixture that quietly lost an interface cannot turn a test into
a tautology.
"""
import copy
import unittest

import _support
from _support import RealApp

import luma_netstatus as ns

LLDP = {"Capabilities": ["Bridge, on", "Router, on"], "ChassisID": "02:00:5e:00:00:01", "MgmtIP": "192.0.2.1",
        "MgmtIface": "0", "PortDescr": "GigabitEthernet1/0/32", "PortID": "Gi1/0/32",
        "SysDescr": "Example IOS\nTechnical Support: https://example.invalid/support", "SysName": "Switch", "TTL": 120}

ENCODER_1G = [{"lanid": "ehternet", "link": True, "lldp": LLDP, "mac": "02:00:5e:00:10:19", "speed": "1000M"},
              {"lanid": "audiolan", "link": False}]
ENCODER_100M = [{"lanid": "ehternet", "link": True, "lldp": dict(LLDP, PortDescr="GigabitEthernet2/0/21", PortID="Gi2/0/21"),
                 "mac": "02:00:5e:00:10:1a", "speed": "100M"},
                {"lanid": "audiolan", "link": True, "mac": "02:00:5e:00:20:77", "speed": "1000M"}]   # the SECOND port is fast
DECODER_1G = [{"lanid": "ehternet", "link": True, "lldp": LLDP, "mac": "02:00:5e:00:10:4a", "speed": "1000M"},
              {"lanid": "lan", "link": False}]
DOWN = [{"lanid": "ehternet", "link": False}, {"lanid": "audiolan", "link": True, "speed": "1000M"}]
NO_NEIGHBOUR = [{"lanid": "ehternet", "link": True, "mac": "02:00:5e:00:10:1b", "speed": "1000M"}]


class FixtureLengthTests(unittest.TestCase):
    def test_the_fixtures_are_the_size_they_were_captured_at(self):
        for fixture, size in ((ENCODER_1G, 2), (ENCODER_100M, 2), (DECODER_1G, 2), (DOWN, 2), (NO_NEIGHBOUR, 1)):
            self.assertEqual(len(fixture), size)
        self.assertEqual(len(LLDP), 9)


class SpeedTests(unittest.TestCase):
    def test_equivalent_forms_are_one_number(self):
        for raw in (1000, "1000", "1000M", "1000 Mbps", "1000Mb/s", "1000m/full", "1G", "1 Gbps", "1 gbit", 1000.0, " 1000M "):
            self.assertEqual(ns.normalize_speed(raw), 1000, repr(raw))
        self.assertEqual(ns.normalize_speed("100M"), 100)
        self.assertEqual(ns.normalize_speed("2.5G"), 2500)
        self.assertEqual(ns.normalize_speed("10G"), 10000)

    def test_what_is_not_a_speed_is_none_and_never_zero(self):
        for raw in (None, True, False, "", "  ", "0", 0, -1, "-100M", "down", "n/a", "1T", "1000X", "Mbps", [], {}, float("nan"),
                    10 ** 9, "9999999M", "1000M " + "x" * 40):
            self.assertIsNone(ns.normalize_speed(raw), repr(raw))


class ParseTests(unittest.TestCase):
    def test_the_av_lan_is_the_ehternet_entry_and_nothing_else(self):
        parsed = ns.parse(copy.deepcopy(ENCODER_100M))
        self.assertEqual(parsed["linkspeed_mbps"], 100)                  # not the 1000M second port
        self.assertEqual(parsed["linkspeed_raw"], "100M")
        self.assertIs(parsed["link_up"], True)
        self.assertEqual(parsed["switch"], {"name": "Switch", "chassis": "02:00:5e:00:00:01", "mgmt_ip": "192.0.2.1",
                                            "port": "GigabitEthernet2/0/21"})

    def test_the_order_of_the_interfaces_does_not_decide_which_is_the_av_lan(self):
        parsed = ns.parse(list(reversed(copy.deepcopy(ENCODER_100M))))
        self.assertEqual(parsed["linkspeed_mbps"], 100)                  # the fast second port listed first is still not the AV LAN
        self.assertIsNone(ns.parse([{"lanid": "audiolan", "link": True, "speed": "1000M"},
                                    {"lanid": "lan", "link": True, "speed": "1000M"}]))

    def test_both_families_parse_and_the_lanid_is_case_insensitive(self):
        self.assertEqual(ns.parse(copy.deepcopy(DECODER_1G))["linkspeed_mbps"], 1000)
        upper = copy.deepcopy(ENCODER_1G)
        upper[0]["lanid"] = "EHTERNET"
        self.assertEqual(ns.parse(upper)["linkspeed_mbps"], 1000)
        fixed = copy.deepcopy(ENCODER_1G)
        fixed[0]["lanid"] = "ethernet"                                   # should a later firmware spell it correctly
        self.assertEqual(ns.parse(fixed)["linkspeed_mbps"], 1000)

    def test_a_link_that_is_down_has_no_speed_and_no_neighbour_even_if_the_reply_carries_them(self):
        parsed = ns.parse(copy.deepcopy(DOWN))
        self.assertIs(parsed["link_up"], False)
        self.assertIsNone(parsed["linkspeed_mbps"])
        self.assertIsNone(parsed["lldp"])
        hostile = [{"lanid": "ehternet", "link": False, "speed": "1000M", "lldp": copy.deepcopy(LLDP)}]
        parsed = ns.parse(hostile)
        self.assertIsNone(parsed["linkspeed_mbps"])
        self.assertIsNone(parsed["lldp"])

    def test_a_link_with_no_neighbour_is_a_speed_without_a_switch(self):
        parsed = ns.parse(copy.deepcopy(NO_NEIGHBOUR))
        self.assertEqual(parsed["linkspeed_mbps"], 1000)
        self.assertIsNone(parsed["lldp"])
        self.assertIsNone(parsed["switch"])

    def test_an_unreadable_speed_is_unknown_and_keeps_its_text(self):
        odd = [{"lanid": "ehternet", "link": True, "speed": "auto"}]
        parsed = ns.parse(odd)
        self.assertIsNone(parsed["linkspeed_mbps"])
        self.assertEqual(parsed["linkspeed_raw"], "auto")

    def test_malformed_replies_are_none_so_the_cache_is_left_alone(self):
        for bad in (None, {}, "x", 5, [], [None], ["x"], [{"lanid": "audiolan", "link": True, "speed": "1000M"}],
                    [{"lanid": 5}], [{"link": True, "speed": "1000M"}], [{}] * 9, {"lanid": "ehternet"}):
            self.assertIsNone(ns.parse(bad), repr(bad)[:40])

    def test_a_non_boolean_link_is_unknown_not_up(self):
        parsed = ns.parse([{"lanid": "ehternet", "link": "yes", "speed": "1000M"}])
        self.assertIsNone(parsed["link_up"])
        self.assertEqual(parsed["linkspeed_mbps"], 1000)

    def test_neighbour_text_is_typed_bounded_and_stripped_of_control_characters(self):
        lldp = {"SysName": "sw\x00itch\x1b[31m", "PortDescr": "p" * 500, "PortID": True, "ChassisID": 12, "TTL": True,
                "Capabilities": ["ok", 5, None, "c" * 200] + ["x"] * 40, "SysDescr": "<img src=x onerror=alert(1)>", "Extra": "ignored"}
        cleaned = ns.clean_lldp(lldp)
        self.assertNotIn("\x00", cleaned["SysName"])
        self.assertNotIn("\x1b", cleaned["SysName"])
        self.assertEqual(len(cleaned["PortDescr"]), 128)
        self.assertNotIn("PortID", cleaned)                               # a boolean is not text
        self.assertEqual(cleaned["ChassisID"], "12")
        self.assertNotIn("TTL", cleaned)
        self.assertEqual(len(cleaned["Capabilities"]), 15)    # only the first 16 are read; None is not text, a number is kept as its text
        self.assertTrue(all(len(c) <= 64 for c in cleaned["Capabilities"]))
        self.assertEqual(cleaned["SysDescr"], "<img src=x onerror=alert(1)>")   # kept as text; the page only ever assigns it as text
        self.assertNotIn("Extra", cleaned)

    def test_no_usable_neighbour_is_none(self):
        for raw in (None, {}, [], "x", {"TTL": -1}, {"Capabilities": []}, {"SysName": "  "}):
            self.assertIsNone(ns.clean_lldp(raw), repr(raw))
        self.assertEqual(ns.clean_lldp({"TTL": 0}), {"TTL": 0})


class ReadTests(unittest.TestCase):
    def fake(self, replies):
        calls = []

        def call(ip, method, params, timeout):
            calls.append(method)
            reply = replies.get(method)
            return reply() if callable(reply) else reply
        call.calls = calls
        return call

    def test_an_encoder_is_asked_its_own_method_first_and_once(self):
        call = self.fake({ns.ENCODER_METHOD: {"result": copy.deepcopy(ENCODER_1G)}})
        got = ns.read(call, "198.18.0.5", "Encoder", 2.0)
        self.assertEqual(call.calls, [ns.ENCODER_METHOD])
        self.assertEqual(got["linkspeed_mbps"], 1000)
        self.assertIsInstance(got["read_at"], float)

    def test_a_decoder_is_asked_its_own_method_first(self):
        call = self.fake({ns.DECODER_METHOD: {"result": copy.deepcopy(DECODER_1G)}})
        self.assertEqual(ns.read(call, "198.18.0.6", "decoder", 2.0)["linkspeed_mbps"], 1000)
        self.assertEqual(call.calls, [ns.DECODER_METHOD])

    def test_a_refusal_from_a_unit_that_answered_tries_the_other_family_once(self):
        call = self.fake({ns.ENCODER_METHOD: {"error": {"code": -32603}}, ns.DECODER_METHOD: {"result": copy.deepcopy(DECODER_1G)}})
        got = ns.read(call, "198.18.0.7", "encoder", 2.0)
        self.assertEqual(call.calls, [ns.ENCODER_METHOD, ns.DECODER_METHOD])
        self.assertEqual(got["linkspeed_mbps"], 1000)

    def test_a_unit_that_does_not_answer_is_not_asked_again_under_another_name(self):
        call = self.fake({})
        self.assertIsNone(ns.read(call, "198.18.0.8", "encoder", 2.0))
        self.assertEqual(call.calls, [ns.ENCODER_METHOD])

    def test_both_names_refused_is_none(self):
        call = self.fake({ns.ENCODER_METHOD: {"error": {}}, ns.DECODER_METHOD: {"error": {}}})
        self.assertIsNone(ns.read(call, "198.18.0.9", "encoder", 2.0))
        self.assertEqual(len(call.calls), 2)

    def test_a_switcher_is_never_asked(self):
        call = self.fake({ns.ENCODER_METHOD: {"result": copy.deepcopy(ENCODER_1G)}})
        for role in ("switcher", "Decoder/Switcher", "SWITCHER"):
            self.assertIsNone(ns.read(call, "198.18.0.10", role, 2.0))
        self.assertEqual(call.calls, [])

    def test_a_reply_that_is_not_a_link_list_is_none(self):
        call = self.fake({ns.ENCODER_METHOD: {"result": {"unexpected": True}}})
        self.assertIsNone(ns.read(call, "198.18.0.11", "encoder", 2.0))


class ScanKeepsItTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.base = lambda ip: {"ip": ip, "model": "HW-LUMA-E4211", "type": "encoder", "role": "Encoder", "mac": "02:00:5e:00:30:01"}
        self.app.patch("_tcp_probe", lambda ip, port: True)
        self.app.patch("_ws_get_minimal", lambda ip: self.base(ip))
        self.app.patch("_query_encoder_details", lambda ip, timeout: {})
        self.reply = {"result": copy.deepcopy(ENCODER_100M)}
        self.app.patch("_ws_call_auth", lambda ip, method, params=None, timeout=None: self.reply)

    def test_the_scan_stores_the_link_read_under_net(self):
        unit = self.app.m._scan_one("198.18.0.20")
        self.assertEqual(unit["net"]["linkspeed_mbps"], 100)
        self.assertEqual(unit["net"]["switch"]["port"], "GigabitEthernet2/0/21")

    def test_a_failed_read_adds_nothing_so_nothing_verified_is_erased(self):
        self.reply = None
        unit = self.app.m._scan_one("198.18.0.21")
        self.assertNotIn("net", unit)

    def test_a_read_that_raises_does_not_fail_the_scan_of_the_unit(self):
        def boom(ip, method, params=None, timeout=None):
            raise OSError("socket closed")
        self.app.patch("_ws_call_auth", boom)
        unit = self.app.m._scan_one("198.18.0.22")
        self.assertEqual(unit["model"], "HW-LUMA-E4211")
        self.assertNotIn("net", unit)

    def test_a_new_read_replaces_the_old_one_outright(self):
        cached = {"ip": "198.18.0.23", "net": {"linkspeed_mbps": 1000, "lldp": copy.deepcopy(LLDP), "link_up": True}}
        fresh = {"net": ns.parse(copy.deepcopy(NO_NEIGHBOUR))}
        self.app.m._replace_net_status(cached, fresh)
        self.assertIsNone(cached["net"]["lldp"])          # the neighbour that went away is gone, not merged back in
        self.assertEqual(cached["net"]["linkspeed_mbps"], 1000)
        slow = {"net": ns.parse(copy.deepcopy(ENCODER_100M))}
        self.app.m._replace_net_status(cached, slow)
        self.assertEqual(cached["net"]["linkspeed_mbps"], 100)

    def test_a_scan_without_a_read_leaves_the_cached_one_alone(self):
        cached = {"ip": "198.18.0.24", "net": {"linkspeed_mbps": 1000}}
        self.app.m._replace_net_status(cached, {"model": "x"})
        self.app.m._replace_net_status(cached, {"net": None})
        self.assertEqual(cached["net"], {"linkspeed_mbps": 1000})

    def test_the_scan_endpoint_replaces_the_cached_read_with_the_new_one(self):
        ip = "198.18.0.25"
        with self.app.m.cache_lock:
            self.app.m.cache_units.clear()
            self.app.m.cache_units[ip] = {"ip": ip, "model": "HW-LUMA-E4211", "type": "encoder", "role": "Encoder",
                                          "mac": "02:00:5e:00:30:02", "hostname": "old",
                                          "net": {"linkspeed_mbps": 1000, "link_up": True, "lldp": copy.deepcopy(LLDP)}}
        fresh = {"ip": ip, "model": "HW-LUMA-E4211", "type": "encoder", "role": "Encoder", "mac": "02:00:5e:00:30:02",
                 "hostname": "new", "net": ns.parse(copy.deepcopy(NO_NEIGHBOUR))}
        self.app.patch("_scan_one", lambda target: copy.deepcopy(fresh))
        response = self.app.m.APP.test_client().post("/api/scan", json={"targets": ip})
        self.assertEqual(response.status_code, 200)
        with self.app.m.cache_lock:
            stored = copy.deepcopy(self.app.m.cache_units[ip])
        self.assertEqual(stored["hostname"], "new")                  # the ordinary merge ran
        self.assertIsNone(stored["net"]["lldp"])                      # and the neighbour that went away did not survive it
        self.assertEqual(stored["net"]["linkspeed_mbps"], 1000)

    def test_a_scan_that_could_not_read_the_link_keeps_the_last_verified_one(self):
        ip = "198.18.0.26"
        with self.app.m.cache_lock:
            self.app.m.cache_units.clear()
            self.app.m.cache_units[ip] = {"ip": ip, "model": "HW-LUMA-E4211", "type": "encoder", "role": "Encoder",
                                          "mac": "02:00:5e:00:30:03", "net": {"linkspeed_mbps": 1000, "link_up": True}}
        self.app.patch("_scan_one", lambda target: {"ip": ip, "model": "HW-LUMA-E4211", "type": "encoder", "role": "Encoder",
                                                    "mac": "02:00:5e:00:30:03"})
        self.assertEqual(self.app.m.APP.test_client().post("/api/scan", json={"targets": ip}).status_code, 200)
        with self.app.m.cache_lock:
            self.assertEqual(self.app.m.cache_units[ip]["net"], {"linkspeed_mbps": 1000, "link_up": True})

if __name__ == "__main__":
    unittest.main()
