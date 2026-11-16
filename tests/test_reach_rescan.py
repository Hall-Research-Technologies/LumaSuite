"""A reachable group member is not refused as offline because of a stale miss count.

Soak finding L-03: a member went unanswered for two matrix refreshes (so `reach.misses`
reached the limit), came back, and a rescan found it -- yet an immediate group route
answered "offline" and wrote nothing, until the next matrix refresh cleared the count.

The rule held here: a scan that has just read a device and read *its own hardware address*
off it, and found that address to be the unit already on record, is a fresh authoritative
answer from that unit and clears its miss count. A reply from a different hardware address
clears nothing for the unit on record, and a device that really is down is still refused.
Fake devices only; nothing leaves loopback.
"""
import copy
import unittest

import _support
from _support import DISPLAY_1, DISPLAY_2

from test_group_integrity import RouteTimeBase, D1, D2, DISPLAY_5
import luma_liveness
import luma_projection

NEW_IP = "198.51.100.121"
IMPOSTOR_MAC = "02:00:5e:20:00:99"


class StaleMissAfterRescanTests(RouteTimeBase):
    def setUp(self):
        super().setUp()
        self.gid = self.group([D1, D2])

    # ---- helpers -----------------------------------------------------------
    def refresh(self):
        self.m._refresh_matrix_state_core()

    def reach(self, identity):
        with self.m.cache_lock:
            for ip, unit in self.m.cache_units.items():
                if luma_projection.unit_identity(unit) == identity:
                    return ip, copy.deepcopy(unit.get(luma_liveness.FIELD))
        return None, None

    def take_down_for_two_refreshes(self, ip):
        self.sim.down.add(ip)
        self.refresh()
        self.refresh()
        _, reach = self.reach(D2)
        self.assertGreaterEqual(reach["misses"], luma_liveness.MISS_LIMIT,
                                "precondition: the unit is determined offline")

    def rescan_finds(self, ip, mac):
        """What api_scan does for a unit it read: fold the scan's row into the cache."""
        with self.m.cache_lock:
            index = self.m._mac_index_of_cache()
            return self.m._ingest_scanned_unit(
                ip, {"ip": ip, "type": "decoder", "status": "idle", "mac": mac,
                     "hostname": "Hall Two", "model": "TEST-D1"}, index)

    def route(self):
        response, _ = self.route_group(self.gid)
        return response.get_json()

    # ---- the defect --------------------------------------------------------
    def test_member_back_at_its_old_address_and_rescanned_is_routed_not_refused(self):
        self.take_down_for_two_refreshes(DISPLAY_2["ip"])
        self.sim.down.discard(DISPLAY_2["ip"])
        self.rescan_finds(DISPLAY_2["ip"], DISPLAY_2["mac"])
        ip, reach = self.reach(D2)
        body = self.route()
        second = body["results"][1]
        self.assertNotEqual(second["result"], "offline",
                            "refused as offline with reach=%r at %s after a rescan found it" % (reach, ip))
        self.assertEqual((body["routed"], body["total"], body["ok"]), (2, 2, True), body)
        self.assertIn(DISPLAY_2["mac"], self.written())

    def test_member_back_at_a_new_address_and_rescanned_is_routed_not_refused(self):
        self.take_down_for_two_refreshes(DISPLAY_2["ip"])
        self.sim.down.discard(DISPLAY_2["ip"])
        self.sim.decoders[NEW_IP] = self.sim.decoders.pop(DISPLAY_2["ip"])
        self.sim.macs[NEW_IP] = self.sim.macs.pop(DISPLAY_2["ip"])
        self.rescan_finds(NEW_IP, DISPLAY_2["mac"])
        ip, reach = self.reach(D2)
        self.assertEqual(ip, NEW_IP)
        body = self.route()
        second = body["results"][1]
        self.assertNotEqual(second["result"], "offline",
                            "refused as offline with reach=%r at %s after a rescan found it" % (reach, ip))
        self.assertTrue(body["ok"], body)

    def test_the_clear_is_recorded_as_a_fresh_answer(self):
        self.take_down_for_two_refreshes(DISPLAY_2["ip"])
        self.sim.down.discard(DISPLAY_2["ip"])
        self.rescan_finds(DISPLAY_2["ip"], DISPLAY_2["mac"])
        _, reach = self.reach(D2)
        self.assertEqual(reach["misses"], 0, reach)

    # ---- what must not change ---------------------------------------------
    def test_a_unit_that_is_really_down_is_still_refused(self):
        self.take_down_for_two_refreshes(DISPLAY_2["ip"])
        body = self.route()                      # no rescan, still silent
        second = body["results"][1]
        self.assertEqual(second["result"], "offline")
        self.assertNotIn(DISPLAY_2["mac"], self.written())

    def test_a_scan_that_read_nothing_clears_nothing(self):
        self.take_down_for_two_refreshes(DISPLAY_2["ip"])
        with self.m.cache_lock:
            index = self.m._mac_index_of_cache()
            outcome = self.m._ingest_scanned_unit(
                DISPLAY_2["ip"], {"ip": DISPLAY_2["ip"], "type": "decoder", "mac": ""}, index)
        self.assertEqual(outcome["action"], "skipped")
        _, reach = self.reach(D2)
        self.assertGreaterEqual(reach["misses"], luma_liveness.MISS_LIMIT)

    def test_a_scan_with_no_mac_but_the_same_serial_does_not_clear_the_count(self):
        # Same serial is enough to keep the row's identity (ingest rules) but not proof that
        # the unit answered as itself: the exact hardware address is the only proof.
        with self.m.cache_lock:
            self.m.cache_units[DISPLAY_2["ip"]]["serialnumber"] = "SN-TWO"
        self.take_down_for_two_refreshes(DISPLAY_2["ip"])
        with self.m.cache_lock:
            index = self.m._mac_index_of_cache()
            outcome = self.m._ingest_scanned_unit(
                DISPLAY_2["ip"], {"ip": DISPLAY_2["ip"], "type": "decoder", "status": "idle",
                                  "mac": "", "serialnumber": "SN-TWO"}, index)
        self.assertEqual(outcome["action"], "updated")
        _, reach = self.reach(D2)
        self.assertGreaterEqual(reach["misses"], luma_liveness.MISS_LIMIT, reach)

    def test_a_row_with_no_mac_on_record_is_not_proven_by_a_scan_that_reports_one(self):
        # The row at this address was never tied to a hardware address, so a scan reporting
        # one cannot show that it is the same unit that missed the refreshes.
        with self.m.cache_lock:
            self.m.cache_units[DISPLAY_2["ip"]].pop("mac")
            self.m.cache_units[DISPLAY_2["ip"]][luma_liveness.FIELD] = {"misses": 5, "ts": 1}
            index = self.m._mac_index_of_cache()
            self.m._ingest_scanned_unit(
                DISPLAY_2["ip"], {"ip": DISPLAY_2["ip"], "type": "decoder", "status": "idle",
                                  "mac": DISPLAY_2["mac"]}, index)
            reach = copy.deepcopy(self.m.cache_units[DISPLAY_2["ip"]][luma_liveness.FIELD])
        self.assertEqual(reach["misses"], 5, reach)

    def test_a_reply_from_another_hardware_address_does_not_clear_the_unit_on_record(self):
        # An impostor answers at D2's old address; D2 itself is silent and on record offline.
        # The impostor's row is its own: it carries none of D2's liveness record, and a route
        # for D2 is neither sent to it nor reported as a success.
        self.take_down_for_two_refreshes(DISPLAY_2["ip"])
        self.sim.down.discard(DISPLAY_2["ip"])
        self.sim.macs[DISPLAY_2["ip"]] = IMPOSTOR_MAC
        self.rescan_finds(DISPLAY_2["ip"], IMPOSTOR_MAC)
        with self.m.cache_lock:
            held = {luma_projection.unit_identity(u): u for u in self.m.cache_units.values()}
        stranger = held[luma_projection.canonical_identity(IMPOSTOR_MAC)]
        self.assertNotIn(luma_liveness.FIELD, stranger,
                         "the stranger's row inherited the unit's liveness record")
        self.assertNotIn(D2, held, "the departed unit's record must not ride on the stranger's row")
        second = self.route()["results"][1]
        self.assertFalse(second["ok"], second)
        self.assertNotIn(IMPOSTOR_MAC, self.written())

    def test_a_different_mac_at_a_new_address_does_not_revive_the_offline_unit(self):
        # D2 is silent and on record offline at its address. A *different* device is found
        # elsewhere: that is not D2, so D2's record stays offline and the route still refuses.
        self.take_down_for_two_refreshes(DISPLAY_2["ip"])
        self.rescan_finds(NEW_IP, IMPOSTOR_MAC)
        ip, reach = self.reach(D2)
        self.assertEqual(ip, DISPLAY_2["ip"])
        self.assertGreaterEqual(reach["misses"], luma_liveness.MISS_LIMIT)
        self.assertEqual(self.route()["results"][1]["result"], "offline")


if __name__ == "__main__":
    unittest.main()
