"""The multi-switch topology across the inventory's life: discovered, re-discovered, cleared, changed.

`test_switch_topology.py` proves the classification of a given inventory. This file proves that the
answer is *recomputed from the inventory as it is now* every time, through the real scan, clear and
remove paths, and that nothing from an earlier state survives where it should not (a stale warning
after the population changed, an acknowledgement outliving the inventory it was given for).

Fixtures use documentation-range addresses and locally administered MACs that share nothing with real
equipment, and are deep-copied before the server sees them (a scan mutates the cached rows in place).
No device is reachable: `_scan_one` is replaced by a table lookup and the fence blocks the rest.
"""
import copy
import os
import tempfile
import time
import unittest

import _support
from _support import RealApp

import luma_topology as lt

SW_A, SW_B, SW_C = "02:aa:00:00:00:a1", "02:bb:00:00:00:b2", "02:cc:00:00:00:c3"
NAME = {SW_A: "sw-alpha", SW_B: "sw-beta", SW_C: "sw-gamma"}


def scanned(ip, chassis, role="Decoder", link=True, name=None):
    """What `_scan_one` returns for one unit, as the scan would hand it to the cache."""
    a, b, c, n = (int(x) for x in ip.split("."))
    net = {"link_up": link, "linkspeed_mbps": 1000 if link else None}
    if chassis and link:
        net["lldp"] = {"ChassisID": chassis, "SysName": name or NAME.get(chassis, ""), "PortDescr": "P%d" % n,
                       "MgmtIP": "198.18.0.250"}
    return {"ip": ip, "mac": "02:00:5e:%02x:%02x:%02x" % (b, c, n), "role": role, "type": role.lower(),
            "model": "HW-LUMA-D4111" if role == "Decoder" else "HW-LUMA-E4211",
            "hostname": "unit-%s" % ip.replace(".", "-"), "net": net}


def group(prefix, count, chassis, start=1, role="Decoder"):
    return [scanned("%s.%d" % (prefix, start + i), chassis, role) for i in range(count)]


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app.patch("TOPOLOGY_ACK", lt.AckStore(os.path.join(tmp.name, "topology_ack.json")))
        self.m._cache_verify_state.update(running=False, finished=time.monotonic())   # no background sweep
        self.client = self.m.APP.test_client()
        self.world = {}
        self.app.patch("_scan_one", lambda ip: copy.deepcopy(self.world.get(ip)))
        with self.m.cache_lock:
            self.m.cache_units.clear()

    def place(self, units):
        """The units that answer, by address (replaces what was there at that address)."""
        for u in copy.deepcopy(units):
            self.world[u["ip"]] = u

    def scan(self, units):
        self.place(units)
        targets = ",".join(u["ip"] for u in units)
        reply = self.client.post("/api/scan", json={"targets": targets}).get_json()
        self.assertTrue(reply["ok"], reply)
        return reply

    def topo(self):
        body = self.client.get("/api/cache").get_json()
        return body["lldp_topology"], body["topology_warning_acknowledged"], body

    def ack(self):
        self.assertTrue(self.client.post("/api/lldp_topology/acknowledge", json={"acknowledged": True}).get_json()["ok"])

    # (1) ------------------------------------------------------------------------------------------
    def test_1_devices_spanning_multiple_switches_raise_the_warning(self):
        reply = self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 2, SW_B))
        self.assertTrue(reply["lldp_topology"]["multi_switch"], "the scan reply itself carries it")
        topo, acked, _ = self.topo()
        self.assertTrue(topo["multi_switch"])
        self.assertEqual(topo["switch_count"], 2)
        self.assertEqual(sorted(topo["minority_ips"]), ["203.0.113.1", "203.0.113.2"])
        self.assertFalse(acked)

    # (2) ------------------------------------------------------------------------------------------
    def test_2_clear_units_resets_the_topology_and_the_acknowledgement(self):
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 2, SW_B))
        self.ack()
        self.assertTrue(self.topo()[1])
        self.assertTrue(self.client.post("/api/clear_units", json={}).get_json()["ok"])
        topo, acked, body = self.topo()
        self.assertEqual((body["units"], topo["switch_count"], topo["multi_switch"], acked), ([], 0, False, False))
        self.assertEqual(topo["devices"], {})

    def test_2b_remove_selected_that_empties_the_inventory_resets_the_acknowledgement(self):
        self.scan(group("198.51.100", 2, SW_A) + group("203.0.113", 1, SW_B))
        self.ack()
        # Removing some units is not the end of the inventory: the acknowledgement stands.
        self.client.post("/api/remove_units", json={"ips": ["203.0.113.1"]})
        self.assertTrue(self.topo()[1], "a partial removal keeps the acknowledgement")
        # Removing the rest is.
        self.client.post("/api/remove_units", json={"ips": ["198.51.100.1", "198.51.100.2"]})
        topo, acked, body = self.topo()
        self.assertEqual(body["units"], [])
        self.assertFalse(acked, "the acknowledgement of an inventory that is gone must not outlive it")

    # (3) ------------------------------------------------------------------------------------------
    def test_3_rediscovering_one_switch_only_leaves_no_stale_warning(self):
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 2, SW_B))
        self.assertTrue(self.topo()[0]["multi_switch"])
        self.client.post("/api/clear_units", json={})
        self.scan(group("198.51.100", 3, SW_A))                      # only the A-side answers now
        topo, acked, _ = self.topo()
        self.assertEqual((topo["switch_count"], topo["multi_switch"], topo["minority_ips"]), (1, False, []))
        self.assertFalse(acked)
        self.assertTrue(all(d["state"] == lt.PRIMARY for d in topo["devices"].values()))

    def test_3b_a_unit_that_moved_to_the_other_switch_is_reread_not_remembered(self):
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 1, SW_B))
        self.assertTrue(self.topo()[0]["multi_switch"])
        self.scan([scanned("203.0.113.1", SW_A)])                     # the same unit, now behind A
        topo, _, _ = self.topo()
        self.assertEqual((topo["switch_count"], topo["multi_switch"]), (1, False))

    def test_3c_a_unit_whose_link_went_down_is_unknown_not_still_on_its_old_switch(self):
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 1, SW_B))
        self.scan([scanned("203.0.113.1", SW_B, link=False)])
        topo, _, _ = self.topo()
        self.assertFalse(topo["multi_switch"])
        self.assertEqual(topo["devices"]["203.0.113.1"]["state"], lt.UNKNOWN)
        self.assertEqual(topo["devices"]["203.0.113.1"]["reason"], lt.REASON_NO_LINK)

    def test_3d_a_neighbour_that_is_gone_is_forgotten_not_kept_from_the_last_read(self):
        # Link still up, but the switch no longer reports a neighbour (LLDP off, port moved to a dumb
        # switch). A merge that ignores "nothing here" would keep the old chassis and call it current.
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 1, SW_B))
        self.assertTrue(self.topo()[0]["multi_switch"])
        self.scan([scanned("203.0.113.1", None)])
        topo, _, _ = self.topo()
        self.assertEqual(topo["devices"]["203.0.113.1"]["state"], lt.UNKNOWN)
        self.assertEqual(topo["devices"]["203.0.113.1"]["reason"], lt.REASON_NO_NEIGHBOUR)
        self.assertFalse(topo["multi_switch"])

    # (4) ------------------------------------------------------------------------------------------
    def test_4_discovering_more_on_another_switch_brings_the_warning_back(self):
        self.scan(group("198.51.100", 3, SW_A))
        self.assertFalse(self.topo()[0]["multi_switch"])
        reply = self.scan(group("203.0.113", 2, SW_B))
        self.assertTrue(reply["lldp_topology"]["multi_switch"])
        topo, acked, _ = self.topo()
        self.assertTrue(topo["multi_switch"])
        self.assertFalse(acked)

    def test_4b_after_clear_and_rediscovery_the_old_dismissal_does_not_hide_the_new_instance(self):
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 2, SW_B))
        self.ack()
        self.client.post("/api/clear_units", json={})
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 2, SW_B))
        topo, acked, _ = self.topo()
        self.assertTrue(topo["multi_switch"])
        self.assertFalse(acked, "a fresh inventory is a fresh instance")

    def test_4c_a_scan_never_resets_a_dismissal_inside_one_inventory(self):
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 2, SW_B))
        self.ack()
        self.scan(group("192.0.2", 1, SW_C))                          # a third switch, same inventory
        topo, acked, _ = self.topo()
        self.assertEqual(topo["switch_count"], 3)
        self.assertTrue(acked)
        # ... and what the dismissal covered is recorded beside it.
        self.assertEqual(self.m.TOPOLOGY_ACK.covered(),
                         sorted([lt.normalize_chassis_id(SW_A), lt.normalize_chassis_id(SW_B)]))

    # (5) ------------------------------------------------------------------------------------------
    def test_5_the_primary_follows_the_population_not_the_first_discovery(self):
        # A is discovered first and is the primary while it has the most units.
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 2, SW_B))
        topo, _, _ = self.topo()
        self.assertEqual(topo["dominant_chassis_id"], lt.normalize_chassis_id(SW_A))
        self.assertEqual({d["state"] for ip, d in topo["devices"].items() if ip.startswith("203.")}, {lt.DIFFERENT})
        # B gains units until it has the most: it is now primary and A's units are the different ones.
        self.scan(group("203.0.113", 3, SW_B, start=3))
        topo, _, _ = self.topo()
        self.assertEqual(topo["dominant_chassis_id"], lt.normalize_chassis_id(SW_B))
        self.assertEqual({d["state"] for ip, d in topo["devices"].items() if ip.startswith("198.")}, {lt.DIFFERENT})
        self.assertEqual({d["state"] for ip, d in topo["devices"].items() if ip.startswith("203.")}, {lt.PRIMARY})

    def test_5b_a_tie_designates_nothing_and_a_single_unit_breaks_it(self):
        self.scan(group("198.51.100", 3, SW_A) + group("203.0.113", 3, SW_B))
        topo, _, _ = self.topo()
        self.assertEqual((topo["dominant_chassis_id"], topo["tied"], topo["minority_ips"]), ("", True, []))
        self.assertEqual({d["state"] for d in topo["devices"].values()}, {lt.TIED})
        self.scan(group("203.0.113", 1, SW_B, start=4))
        topo, _, _ = self.topo()
        self.assertEqual((topo["dominant_chassis_id"], topo["tied"]), (lt.normalize_chassis_id(SW_B), False))
        self.assertEqual(sorted(topo["minority_ips"]), ["198.51.100.1", "198.51.100.2", "198.51.100.3"])

    def test_5c_discovery_order_never_decides_the_primary(self):
        forward = group("198.51.100", 2, SW_A) + group("203.0.113", 4, SW_B)
        for order in (forward, list(reversed(forward))):
            self.client.post("/api/clear_units", json={})
            self.world.clear()
            for u in order:
                self.scan([u])                                         # one unit per scan, in this order
            topo, _, _ = self.topo()
            self.assertEqual(topo["dominant_chassis_id"], lt.normalize_chassis_id(SW_B))

    def test_5d_unknown_units_neither_swing_the_primary_nor_become_a_second_switch(self):
        self.scan(group("198.51.100", 2, SW_A) + group("203.0.113", 1, SW_B))
        self.scan([scanned("192.0.2.%d" % i, None) for i in range(1, 8)])   # seven units with no neighbour
        topo, _, _ = self.topo()
        self.assertEqual((topo["switch_count"], topo["dominant_chassis_id"]), (2, lt.normalize_chassis_id(SW_A)))
        self.assertEqual(topo["units_without_lldp"], 7)
        self.assertEqual({d["state"] for ip, d in topo["devices"].items() if ip.startswith("192.0.2.")}, {lt.UNKNOWN})

    # (6) ------------------------------------------------------------------------------------------
    def test_6_later_units_on_a_third_switch_update_primary_and_different(self):
        self.scan(group("198.51.100", 4, SW_A) + group("203.0.113", 2, SW_B))
        self.assertEqual(self.topo()[0]["dominant_chassis_id"], lt.normalize_chassis_id(SW_A))
        self.scan(group("192.0.2", 5, SW_C))
        topo, _, _ = self.topo()
        self.assertEqual(topo["switch_count"], 3)
        self.assertEqual(topo["dominant_chassis_id"], lt.normalize_chassis_id(SW_C))
        states = {ip: d["state"] for ip, d in topo["devices"].items()}
        self.assertEqual({s for ip, s in states.items() if ip.startswith("192.0.2.")}, {lt.PRIMARY})
        self.assertEqual({s for ip, s in states.items() if not ip.startswith("192.0.2.")}, {lt.DIFFERENT})
        self.assertEqual(len(topo["minority_ips"]), 6)

    # a chain of our own units is not a switch --------------------------------------------------------
    def test_a_unit_cabled_through_another_unit_does_not_invent_a_switch(self):
        front = scanned("198.51.100.1", SW_A)
        behind = scanned("198.51.100.2", "02:00:5e:77:00:01")          # reports the unit in front of it
        behind["net"]["lldp"]["MgmtIP"] = "198.51.100.1"
        front["mac"] = "02:00:5e:77:00:01"
        self.scan([front, behind, scanned("198.51.100.3", SW_A)])
        topo, _, _ = self.topo()
        self.assertEqual(topo["switch_count"], 1)
        self.assertFalse(topo["multi_switch"])
        record = topo["devices"]["198.51.100.2"]
        self.assertEqual((record["state"], record["chained"], record["via_ip"]), (lt.PRIMARY, True, "198.51.100.1"))
        self.assertEqual(record["port"], "", "the port in a chain belongs to the unit in front")

    def test_a_chain_that_cannot_be_resolved_is_unknown_never_different(self):
        a = scanned("198.51.100.1", "02:00:5e:77:00:02")
        b = scanned("198.51.100.2", "02:00:5e:77:00:01")
        a["mac"], b["mac"] = "02:00:5e:77:00:01", "02:00:5e:77:00:02"   # each names the other: a loop
        a["net"]["lldp"]["MgmtIP"], b["net"]["lldp"]["MgmtIP"] = "198.51.100.2", "198.51.100.1"
        self.scan([a, b] + group("203.0.113", 2, SW_B))
        topo, _, _ = self.topo()
        self.assertEqual(topo["devices"]["198.51.100.1"]["state"], lt.UNKNOWN)
        self.assertEqual(topo["devices"]["198.51.100.1"]["reason"], lt.REASON_UNRESOLVED)
        self.assertEqual(topo["minority_ips"], [])
        self.assertFalse(topo["multi_switch"])


class LearningUnreadUnits(unittest.TestCase):
    """An inventory carried over from before switch awareness has no neighbour recorded for any unit.
    The sweep that already reads every unit learns it, once, without touching what is already known."""

    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.reads = []
        self.net = {"link_up": True, "linkspeed_mbps": 1000,
                    "lldp": {"ChassisID": SW_A, "SysName": "sw-alpha", "MgmtIP": "198.18.0.250"}}
        self.answer = True

        def fake(call, ip, role, timeout):
            self.reads.append(ip)
            return copy.deepcopy(self.net) if self.answer else None
        patcher = self.app.mock.patch.object(self.m.luma_netstatus, "read", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = dict(self.m._net_learn_attempts)
        self.addCleanup(lambda: (self.m._net_learn_attempts.clear(), self.m._net_learn_attempts.update(saved)))
        self.m._net_learn_attempts.clear()
        with self.m.cache_lock:
            self.m.cache_units.clear()
            self.m.cache_units["198.51.100.9"] = {"ip": "198.51.100.9", "mac": "02:00:5e:01:00:09", "role": "Decoder"}

    def unit(self):
        return self.m.cache_units["198.51.100.9"]

    def test_a_never_read_unit_learns_its_neighbour(self):
        self.assertTrue(self.m._learn_net_status_if_never_read("198.51.100.9", self.unit()))
        self.assertEqual(self.unit()["net"]["lldp"]["ChassisID"], SW_A)

    def test_a_known_value_is_never_read_again_or_replaced(self):
        self.unit()["net"] = {"link_up": True, "lldp": {"ChassisID": SW_B}}
        self.assertFalse(self.m._learn_net_status_if_never_read("198.51.100.9", self.unit()))
        self.assertEqual((self.reads, self.unit()["net"]["lldp"]["ChassisID"]), ([], SW_B))

    def test_an_unanswered_read_leaves_the_unit_unread_and_is_not_repeated_at_once(self):
        self.answer = False
        self.assertFalse(self.m._learn_net_status_if_never_read("198.51.100.9", self.unit()))
        self.assertNotIn("net", self.unit())
        self.assertFalse(self.m._learn_net_status_if_never_read("198.51.100.9", self.unit()))
        self.assertEqual(len(self.reads), 1, "paced: a unit that did not answer is not asked again straight away")

    def test_the_cache_sweep_learns_it_and_the_topology_then_sees_it(self):
        for name, value in (("_ws_get_hostname", lambda ip, t: ""), ("_ws_get_dante_info", lambda ip, t: {}),
                            ("_ws_get_dante_power_status", lambda ip, t: None),
                            ("_ws_get_hdcp_encoder", lambda ip, t: ""), ("_ws_get_hdcp_decoder", lambda ip, t: "")):
            self.app.patch(name, value)
        self.m._cache_verify_state.update(running=False, finished=-1e9)
        client = self.m.APP.test_client()
        first = client.get("/api/cache").get_json()["lldp_topology"]
        self.assertEqual(first["units_not_read"], 1)
        deadline = time.time() + 5
        while self.m._cache_verify_state["running"] and time.time() < deadline:
            time.sleep(0.01)
        later = client.get("/api/cache").get_json()["lldp_topology"]
        self.assertEqual((later["units_not_read"], later["switch_count"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
