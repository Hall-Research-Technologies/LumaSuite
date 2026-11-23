"""Multi-switch awareness: which switch each unit reports over LLDP, which one is primary, and the advisory.

The semantics are OmniSuite's and the scenarios are ported from its LLDP tests. Fixtures use values that
share nothing with real equipment or with each other: a different chassis ID, name and subnet per switch,
so a test that confuses subnet, name or address with the chassis identity cannot pass by accident.
Fixtures are deep-copied before they are handed to anything that could mutate them.
"""
import copy
import inspect
import os
import tempfile
import time
import unittest

import _support
from _support import RealApp

import luma_access
import luma_topology as lt

SW_A = "02:aa:00:00:00:a1"
SW_B = "02:bb:00:00:00:b2"
SW_C = "02:cc:00:00:00:c3"
SW_D = "02:dd:00:00:00:d4"


def unit(ip, chassis=SW_A, name="sw-alpha", role="Decoder", mac=None, **net_extra):
    n = int(ip.rsplit(".", 1)[1])
    net = {"link_up": True, "linkspeed_mbps": 1000}
    if chassis is not None:
        lldp = {"ChassisID": chassis, "PortDescr": "Port%d" % n, "MgmtIP": "203.0.113.250"}
        if name is not None:
            lldp["SysName"] = name
        net["lldp"] = lldp
    net.update(net_extra)
    return {"ip": ip, "mac": mac or "02:00:5e:30:%02x:%02x" % (n >> 8 & 255, n & 255), "role": role, "net": net}


def many(prefix, count, chassis, name, start=1, role="Decoder"):
    return [unit("%s.%d" % (prefix, start + i), chassis, name, role) for i in range(count)]


class NormalisationTests(unittest.TestCase):
    def test_cosmetic_differences_are_one_chassis(self):
        forms = ["02:AA:00:00:00:A1", "02-aa-00-00-00-a1", "02aa.0000.00a1", " 02aa000000a1 "]
        self.assertEqual({lt.normalize_chassis_id(f) for f in forms}, {"02aa000000a1"})

    def test_an_address_is_never_turned_into_a_mac(self):
        # Twelve decimal digits once the dots are stripped -- also a well-formed MAC.
        self.assertEqual(lt.normalize_chassis_id("192.168.100.100"), "192.168.100.100")
        self.assertNotEqual(lt.normalize_chassis_id("192.168.100.100"), lt.normalize_chassis_id("1921.6810.0100"))

    def test_a_placeholder_is_not_an_identity(self):
        for bad in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "", "   ", None, True, 7, [], {}):
            self.assertEqual(lt.normalize_chassis_id(bad), "", repr(bad))

    def test_a_name_style_chassis_keeps_its_shape(self):
        self.assertEqual(lt.normalize_chassis_id("  Core   Switch-01 "), "core switch-01")


class UnknownIsNeverDifferentTests(unittest.TestCase):
    def reason(self, u):
        return lt.switch_of(u)

    def test_each_kind_of_missing_or_partial_information_is_unknown_with_its_own_reason(self):
        cases = [
            ({"ip": "198.51.100.1"}, lt.REASON_NOT_READ),
            ({"ip": "198.51.100.1", "net": None}, lt.REASON_NOT_READ),
            ({"ip": "198.51.100.1", "net": "x"}, lt.REASON_NOT_READ),
            ({"ip": "198.51.100.1", "net": {"link_up": False}}, lt.REASON_NO_LINK),
            ({"ip": "198.51.100.1", "net": {"link_up": False, "lldp": {"ChassisID": SW_A}}}, lt.REASON_NO_LINK),
            ({"ip": "198.51.100.1", "net": {"link_up": True}}, lt.REASON_NO_NEIGHBOUR),
            ({"ip": "198.51.100.1", "net": {"link_up": True, "lldp": None}}, lt.REASON_NO_NEIGHBOUR),
            ({"ip": "198.51.100.1", "net": {"link_up": True, "lldp": {}}}, lt.REASON_NO_NEIGHBOUR),
            ({"ip": "198.51.100.1", "net": {"lldp": []}}, lt.REASON_MALFORMED),
            ({"ip": "198.51.100.1", "net": {"lldp": "chassis"}}, lt.REASON_MALFORMED),
            ({"ip": "198.51.100.1", "net": {"lldp": {"SysName": "only-a-name"}}}, lt.REASON_MALFORMED),
            ({"ip": "198.51.100.1", "net": {"lldp": {"ChassisID": ""}}}, lt.REASON_MALFORMED),
            ({"ip": "198.51.100.1", "net": {"lldp": {"ChassisID": 12345}}}, lt.REASON_MALFORMED),
            ({"ip": "198.51.100.1", "net": {"lldp": {"ChassisID": ["x"]}}}, lt.REASON_MALFORMED),
            ({"ip": "198.51.100.1", "net": {"lldp": {"ChassisID": "00:00:00:00:00:00"}}}, lt.REASON_MALFORMED),
        ]
        for u, reason in cases:
            got = lt.switch_of(copy.deepcopy(u))
            self.assertFalse(got["known"], u)
            self.assertEqual(got["reason"], reason, u)

    def test_a_name_alone_never_forms_a_switch(self):
        units = many("198.51.100", 3, SW_A, "sw-alpha") + [unit("198.51.100.9", None, None)]
        units[-1]["net"]["lldp"] = {"SysName": "sw-beta"}
        topo = lt.topology(units)
        self.assertEqual(topo["switch_count"], 1)
        self.assertEqual(topo["devices"]["198.51.100.9"]["state"], lt.UNKNOWN)

    def test_malformed_records_never_raise_and_are_counted_as_unknown(self):
        junk = [None, "x", 5, [], {}, {"ip": ""}, {"ip": "198.51.100.4", "net": {"lldp": {"ChassisID": {"a": 1}}}},
                {"ip": "198.51.100.5", "net": [1, 2]}]
        topo = lt.topology(copy.deepcopy(junk) + many("198.51.100", 2, SW_A, "sw-alpha", start=20))
        self.assertEqual(topo["switch_count"], 1)
        self.assertEqual(topo["units_without_lldp"], 2)
        self.assertEqual(topo["minority_ips"], [])
        self.assertEqual(lt.topology(None)["switch_count"], 0)
        self.assertEqual(lt.topology([])["multi_switch"], False)


class GroupingTests(unittest.TestCase):
    def test_all_units_on_one_switch(self):
        topo = lt.topology(many("198.51.100", 7, SW_A, "sw-alpha"))
        self.assertEqual(topo["switch_count"], 1)
        self.assertFalse(topo["multi_switch"])
        self.assertFalse(topo["tied"])
        self.assertEqual(topo["minority_ips"], [])
        self.assertEqual({d["state"] for d in topo["devices"].values()}, {lt.PRIMARY})
        self.assertEqual(topo["dominant_chassis_id"], lt.normalize_chassis_id(SW_A))

    def test_two_switches_ten_and_five(self):
        units = many("198.51.100", 10, SW_A, "sw-alpha") + many("203.0.113", 5, SW_B, "sw-beta")
        topo = lt.topology(units)
        self.assertEqual(topo["switch_count"], 2)
        self.assertTrue(topo["multi_switch"])
        self.assertEqual([g["count"] for g in topo["groups"]], [10, 5])
        self.assertEqual(topo["dominant_chassis_id"], lt.normalize_chassis_id(SW_A))
        self.assertEqual(topo["minority_ips"], ["203.0.113.%d" % i for i in range(1, 6)])
        self.assertEqual([d["state"] for ip, d in topo["devices"].items() if ip.startswith("198.")],
                         [lt.PRIMARY] * 10)
        self.assertEqual({d["state"] for ip, d in topo["devices"].items() if ip.startswith("203.")},
                         {lt.DIFFERENT})
        self.assertFalse(topo["tied"])

    def test_the_larger_group_is_primary_regardless_of_input_order(self):
        units = many("203.0.113", 2, SW_B, "sw-beta") + many("198.51.100", 4, SW_A, "sw-alpha")
        a = lt.topology(copy.deepcopy(units))
        b = lt.topology(copy.deepcopy(list(reversed(units))))
        self.assertEqual(a, b)
        self.assertEqual(a["dominant_chassis_id"], lt.normalize_chassis_id(SW_A))
        self.assertEqual(a["minority_ips"], ["203.0.113.1", "203.0.113.2"])

    def test_three_switches(self):
        units = (many("198.51.100", 6, SW_A, "sw-alpha") + many("203.0.113", 3, SW_B, "sw-beta")
                 + many("192.0.2", 2, SW_C, "sw-gamma"))
        topo = lt.topology(units)
        self.assertEqual(topo["switch_count"], 3)
        self.assertEqual([g["count"] for g in topo["groups"]], [6, 3, 2])
        self.assertEqual(len(topo["minority_ips"]), 5)
        self.assertNotIn("198.51.100.1", topo["minority_ips"])
        self.assertTrue(topo["multi_switch"])

    def test_a_tie_has_no_primary_and_no_different_unit(self):
        units = many("198.51.100", 3, SW_A, "sw-alpha") + many("203.0.113", 3, SW_B, "sw-beta")
        topo = lt.topology(units)
        self.assertTrue(topo["multi_switch"], "the operator is still told there are two")
        self.assertTrue(topo["tied"])
        self.assertEqual(topo["dominant_chassis_id"], "")
        self.assertEqual(topo["minority_ips"], [])
        self.assertEqual({d["state"] for d in topo["devices"].values()}, {lt.TIED})

    def test_a_tie_for_first_among_three_designates_nothing(self):
        units = (many("198.51.100", 4, SW_A, "a") + many("203.0.113", 4, SW_B, "b") + many("192.0.2", 1, SW_C, "c"))
        topo = lt.topology(units)
        self.assertTrue(topo["tied"])
        self.assertEqual(topo["minority_ips"], [])

    def test_a_single_extra_unit_breaks_the_tie(self):
        units = many("198.51.100", 3, SW_A, "a") + many("203.0.113", 3, SW_B, "b") + [unit("203.0.113.40", SW_B, "b")]
        topo = lt.topology(units)
        self.assertFalse(topo["tied"])
        self.assertEqual(topo["dominant_chassis_id"], lt.normalize_chassis_id(SW_B))
        self.assertEqual(len(topo["minority_ips"]), 3)

    def test_units_without_lldp_are_counted_and_never_influence_the_grouping(self):
        units = (many("198.51.100", 4, SW_A, "sw-alpha")
                 + [unit("198.51.100.%d" % (60 + i), None) for i in range(4)])
        topo = lt.topology(units)
        self.assertEqual(topo["switch_count"], 1)
        self.assertFalse(topo["multi_switch"])
        self.assertEqual(topo["units_without_lldp"], 4)
        self.assertEqual(topo["units_with_lldp"], 4)
        self.assertEqual([d["state"] for ip, d in topo["devices"].items() if ip.endswith(("60", "61"))],
                         [lt.UNKNOWN, lt.UNKNOWN])

    def test_unknown_units_cannot_swing_the_primary(self):
        # Three on A, two on B, and a crowd with no LLDP: A is still primary.
        units = (many("198.51.100", 3, SW_A, "a") + many("203.0.113", 2, SW_B, "b")
                 + [unit("192.0.2.%d" % i, None) for i in range(1, 30)])
        topo = lt.topology(units)
        self.assertEqual(topo["dominant_chassis_id"], lt.normalize_chassis_id(SW_A))
        self.assertEqual(topo["minority_ips"], ["203.0.113.1", "203.0.113.2"])

    def test_no_lldp_at_all_means_no_switches(self):
        topo = lt.topology([unit("198.51.100.%d" % i, None) for i in range(1, 6)])
        self.assertEqual((topo["switch_count"], topo["multi_switch"], topo["units_without_lldp"]), (0, False, 5))


class IdentityIsTheChassisNotTheNetworkTests(unittest.TestCase):
    def test_different_subnets_on_one_switch_are_one_switch(self):
        units = (many("198.51.100", 3, SW_A, "sw-alpha") + many("203.0.113", 3, SW_A, "sw-alpha")
                 + many("192.0.2", 3, SW_A, "sw-alpha"))
        topo = lt.topology(units)
        self.assertEqual(topo["switch_count"], 1)
        self.assertEqual(topo["minority_ips"], [])

    def test_one_subnet_across_two_switches_is_two_switches(self):
        units = many("198.51.100", 5, SW_A, "sw-alpha") + many("198.51.100", 2, SW_B, "sw-beta", start=100)
        topo = lt.topology(units)
        self.assertEqual(topo["switch_count"], 2)
        self.assertEqual(topo["minority_ips"], ["198.51.100.100", "198.51.100.101"])

    def test_the_system_name_decides_nothing(self):
        same_name = many("198.51.100", 3, SW_A, "stack") + many("203.0.113", 2, SW_B, "stack")
        self.assertEqual(lt.topology(same_name)["switch_count"], 2)
        renamed = many("198.51.100", 3, SW_A, "one") + many("203.0.113", 2, SW_A.upper(), "two")
        self.assertEqual(lt.topology(renamed)["switch_count"], 1)

    def test_the_management_address_decides_nothing(self):
        a = many("198.51.100", 2, SW_A, "x")
        b = many("203.0.113", 2, SW_B, "x")
        for u in a + b:
            u["net"]["lldp"]["MgmtIP"] = "192.0.2.77"
        self.assertEqual(lt.topology(a + b)["switch_count"], 2)

    def test_a_stack_member_port_does_not_split_a_switch(self):
        a = unit("198.51.100.1", SW_A, "stack")
        b = unit("198.51.100.2", SW_A, "stack")
        a["net"]["lldp"]["PortDescr"] = "GigabitEthernet1/0/30"
        b["net"]["lldp"]["PortDescr"] = "GigabitEthernet2/0/41"
        self.assertEqual(lt.topology([a, b])["switch_count"], 1)


class NamingTests(unittest.TestCase):
    def test_the_different_switch_names_its_own_switch(self):
        topo = lt.topology(many("198.51.100", 3, SW_A, "sw-alpha") + many("203.0.113", 1, SW_B, "sw-beta"))
        self.assertEqual(topo["devices"]["203.0.113.1"]["switch_name"], "sw-beta")
        self.assertEqual(topo["devices"]["198.51.100.1"]["switch_name"], "sw-alpha")

    def test_a_missing_name_is_blank_and_never_an_identifier_or_address(self):
        topo = lt.topology(many("198.51.100", 3, SW_A, "sw-alpha") + many("203.0.113", 1, SW_B, None))
        rec = topo["devices"]["203.0.113.1"]
        self.assertEqual(rec["state"], lt.DIFFERENT)
        self.assertEqual(rec["switch_name"], "")

    def test_a_blank_name_on_one_unit_is_filled_from_its_switch_mates_deterministically(self):
        a = unit("203.0.113.1", SW_B, None)
        b = unit("203.0.113.2", SW_B, "sw-beta")
        topo = lt.topology(many("198.51.100", 3, SW_A, "sw-alpha") + [a, b])
        self.assertEqual(topo["devices"]["203.0.113.1"]["switch_name"], "sw-beta")

    def test_the_first_name_in_address_order_labels_a_group(self):
        a = unit("203.0.113.9", SW_B, "zzz-late")
        b = unit("203.0.113.2", SW_B, "aaa-early")
        topo = lt.topology([a, b] + many("198.51.100", 3, SW_A, "x"))
        self.assertEqual([g for g in topo["groups"] if g["chassis_id"] == lt.normalize_chassis_id(SW_B)][0]["chassis_name"],
                         "aaa-early")

    def test_addresses_sort_numerically(self):
        units = [unit("198.51.100.100", SW_B, "b"), unit("198.51.100.20", SW_B, "b"), unit("198.51.100.3", SW_B, "b")]
        topo = lt.topology(units + many("203.0.113", 5, SW_A, "a"))
        self.assertEqual(topo["minority_ips"], ["198.51.100.3", "198.51.100.20", "198.51.100.100"])


class PurityTests(unittest.TestCase):
    def test_the_input_is_never_mutated(self):
        units = many("198.51.100", 3, SW_A, "a") + many("203.0.113", 1, SW_B, "b")
        before = copy.deepcopy(units)
        lt.topology(units)
        self.assertEqual(units, before)

    def test_the_module_reaches_no_network(self):
        source = inspect.getsource(lt)
        for word in ("socket", "requests", "websocket", "urllib", "subprocess", "threading"):
            self.assertNotIn("import " + word, source)


class AckStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = lt.AckStore(os.path.join(self.dir.name, "topology_ack.json"))

    def test_absent_means_not_acknowledged(self):
        self.assertFalse(self.store.acknowledged())

    def test_set_and_read_back(self):
        self.assertTrue(self.store.set(True, 1.0))
        self.assertTrue(self.store.acknowledged())
        self.assertTrue(self.store.set(False, 2.0))
        self.assertFalse(self.store.acknowledged())

    def test_reset_clears_it(self):
        self.store.set(True, 1.0)
        self.store.reset()
        self.assertFalse(self.store.acknowledged())

    def test_a_damaged_file_degrades_to_not_acknowledged_and_is_kept_aside(self):
        with open(self.store.path, "w") as handle:
            handle.write("{not json")
        self.assertFalse(self.store.acknowledged())
        self.assertTrue([n for n in os.listdir(self.dir.name) if ".corrupt" in n])

    def test_only_a_true_boolean_counts(self):
        for value in ('{"acknowledged": "yes"}', '{"acknowledged": 1}', '[]', '"x"'):
            with open(self.store.path, "w") as handle:
                handle.write(value)
            self.assertFalse(self.store.acknowledged(), value)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app.patch("TOPOLOGY_ACK", lt.AckStore(os.path.join(tmp.name, "topology_ack.json")))
        # No background hostname sweep: this test is about the inventory, not about device traffic.
        verify = dict(self.m._cache_verify_state)
        self.m._cache_verify_state.update(running=False, finished=time.monotonic())
        self.addCleanup(lambda: self.m._cache_verify_state.update(verify))
        self.client = self.m.APP.test_client()
        self.one_switch = many("198.51.100", 4, SW_A, "sw-alpha")
        self.second = many("203.0.113", 2, SW_B, "sw-beta")

    def seed(self, units):
        with self.m.cache_lock:
            self.m.cache_units.clear()
            for u in copy.deepcopy(units):
                self.m.cache_units[u["ip"]] = u

    def cache(self):
        return self.client.get("/api/cache").get_json()

    def test_the_inventory_response_carries_the_topology_beside_the_units(self):
        self.seed(self.one_switch + self.second)
        body = self.cache()
        self.assertEqual(len(body["units"]), 6)
        topo = body["lldp_topology"]
        self.assertTrue(topo["multi_switch"])
        self.assertEqual(topo["minority_ips"], ["203.0.113.1", "203.0.113.2"])
        self.assertIs(body["topology_warning_acknowledged"], False)

    def test_unit_rows_are_exactly_what_they_were(self):
        self.seed(self.one_switch + self.second)
        served = {u["ip"]: u for u in self.cache()["units"]}
        for original in self.one_switch + self.second:
            row = served[original["ip"]]
            self.assertEqual({k: v for k, v in row.items() if k != "online"}, original)

    def test_the_endpoint_reads_the_same_derivation(self):
        self.seed(self.one_switch + self.second)
        body = self.client.get("/api/lldp_topology").get_json()
        self.assertEqual(body["topology"], self.cache()["lldp_topology"])
        self.assertFalse(body["acknowledged"])

    def test_deriving_the_topology_sends_nothing_to_any_unit(self):
        self.seed(self.one_switch + self.second)
        self.cache()
        self.client.get("/api/lldp_topology")
        self.client.post("/api/lldp_topology/acknowledge", json={"acknowledged": True})
        self.assertEqual(self.app.devices.calls, [])

    def test_acknowledging_validates_the_body(self):
        for bad in ({}, {"acknowledged": "true"}, {"acknowledged": 1}, {"ack": True}, {"acknowledged": None}):
            r = self.client.post("/api/lldp_topology/acknowledge", json=bad)
            self.assertEqual(r.status_code, 400, bad)
        self.assertFalse(self.m.TOPOLOGY_ACK.acknowledged())

    def test_topology_refresh_then_dismissal_then_return_per_omni_semantics(self):
        # 1 switch -> no advisory condition.
        self.seed(self.one_switch)
        self.assertFalse(self.cache()["lldp_topology"]["multi_switch"])
        # A second switch appears (a rescan, no restart): the condition is true and unacknowledged.
        self.seed(self.one_switch + self.second)
        body = self.cache()
        self.assertTrue(body["lldp_topology"]["multi_switch"])
        self.assertFalse(body["topology_warning_acknowledged"])
        # Dismiss.
        self.assertTrue(self.client.post("/api/lldp_topology/acknowledge", json={"acknowledged": True}).get_json()["ok"])
        self.assertTrue(self.cache()["topology_warning_acknowledged"])
        # The second switch disappears: no condition, but the acknowledgement stands.
        self.seed(self.one_switch)
        body = self.cache()
        self.assertFalse(body["lldp_topology"]["multi_switch"])
        self.assertTrue(body["topology_warning_acknowledged"])
        # It returns: still the same inventory's life, so still dismissed. Different-switch marking is unaffected.
        self.seed(self.one_switch + self.second)
        body = self.cache()
        self.assertTrue(body["lldp_topology"]["multi_switch"])
        self.assertTrue(body["topology_warning_acknowledged"])
        self.assertEqual(body["lldp_topology"]["minority_ips"], ["203.0.113.1", "203.0.113.2"])
        # A third switch is not a new acknowledgement either.
        self.seed(self.one_switch + self.second + many("192.0.2", 1, SW_C, "sw-gamma"))
        self.assertTrue(self.cache()["topology_warning_acknowledged"])

    def test_clearing_the_inventory_is_what_resets_the_acknowledgement(self):
        self.seed(self.one_switch + self.second)
        self.client.post("/api/lldp_topology/acknowledge", json={"acknowledged": True})
        r = self.client.post("/api/clear_units", json={})
        self.assertTrue(r.get_json()["ok"], r.get_json())
        body = self.cache()
        self.assertEqual(body["units"], [])
        self.assertFalse(body["topology_warning_acknowledged"])
        self.assertEqual(body["lldp_topology"]["switch_count"], 0)
        # And it comes back as a fresh instance for a fresh inventory.
        self.seed(self.one_switch + self.second)
        self.assertFalse(self.cache()["topology_warning_acknowledged"])

    def test_a_scan_never_resets_the_acknowledgement(self):
        self.seed(self.one_switch + self.second)
        self.client.post("/api/lldp_topology/acknowledge", json={"acknowledged": True})
        source = inspect.getsource(self.m.api_scan)
        self.assertNotIn("TOPOLOGY_ACK.reset", source)
        self.assertNotIn("topology_ack", source.lower())
        self.assertEqual(source.count("TOPOLOGY_ACK"), 0)

    def test_the_reset_is_called_only_where_the_inventory_ends(self):
        src = inspect.getsource(self.m)
        # full clear, the clear that empties it, and the one helper Remove Selected uses when it empties it
        self.assertEqual(src.count("TOPOLOGY_ACK.reset()"), 3)
        for name in ("_clear_full", "_clear_unassigned", "_end_inventory_life_if_empty"):
            self.assertIn("TOPOLOGY_ACK.reset()", inspect.getsource(getattr(self.m, name)))
        self.assertEqual(src.count("_end_inventory_life_if_empty()"), 3)  # def line + two removal paths

    def test_a_malformed_cached_record_never_takes_the_inventory_down(self):
        self.seed(self.one_switch)
        with self.m.cache_lock:
            self.m.cache_units["198.51.100.77"] = {"ip": "198.51.100.77", "net": {"lldp": [1, {"x": 2}]}}
            self.m.cache_units["198.51.100.78"] = {"ip": "198.51.100.78", "net": 5}
        body = self.cache()
        self.assertEqual(len(body["units"]), 6)
        self.assertEqual(body["lldp_topology"]["switch_count"], 1)
        self.assertEqual(body["lldp_topology"]["units_without_lldp"], 2)

    def test_the_routes_are_administrative(self):
        self.assertEqual(luma_access.classify("/api/lldp_topology", "GET"), luma_access.Access.ADMIN)
        self.assertEqual(luma_access.classify("/api/lldp_topology/acknowledge", "POST"), luma_access.Access.ADMIN)


class NoSideEffectsOnRoutingTests(unittest.TestCase):
    """Switch awareness is informational: it changes no routing, discovery or eligibility."""

    def test_no_routing_or_discovery_code_reads_the_topology(self):
        import lumaserver
        for name in ("api_route_matrix", "api_scan", "_scan_one", "_route_breakaway"):
            fn = getattr(lumaserver, name, None)
            if fn is None:
                continue
            body = inspect.getsource(fn)
            body = body.replace("TOPOLOGY_ACK", "")  # api_scan must not even mention the store
            self.assertNotIn("luma_topology", body, name)
            self.assertNotIn("minority", body, name)
        self.assertGreaterEqual(sum(1 for n in ("api_route_matrix", "api_scan") if getattr(lumaserver, n, None)), 2)

    def test_the_same_route_does_the_same_thing_with_units_on_different_switches(self):
        calls = []
        for chassis_for_b in (SW_A, SW_B):
            case = unittest.TestCase()
            app = RealApp(case)
            app.seed()
            for key, u in list(app.m.cache_units.items()):
                n = int(key.rsplit(".", 1)[1])
                u["net"] = {"link_up": True, "lldp": {"ChassisID": SW_A if n % 2 else chassis_for_b, "SysName": "x"}}
            client = app.m.APP.test_client()
            r = client.post("/api/route_matrix", json={"decoder_ip": _support.DISPLAY_1["ip"],
                                                      "encoder_ip": _support.SOURCE_A["ip"]})
            calls.append((r.status_code, r.get_json()["ok"], list(app.devices.calls)))
            app.restore()
        self.assertEqual(calls[0], calls[1])
        self.assertTrue(calls[0][1])


if __name__ == "__main__":
    unittest.main()
