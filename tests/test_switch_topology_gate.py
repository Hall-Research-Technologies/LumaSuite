"""The switch-topology learner obeys the back-off the offline-polling work put on the cache sweep.

`_learn_net_status_if_never_read` runs inside the page-load verify sweep, one unit at a time. That sweep
skips a unit that stopped answering until its back-off has elapsed (`luma_liveness.is_due`); the learner
sits behind the same gate, so a silent unit costs no neighbour read. The two features were built apart
and merged, and nothing else holds them together: if the learner were reached before the gate, every page
load would spend a read (up to two seconds on a silent unit) on each unit the back-off exists to leave alone.

Fixtures use documentation-range addresses and a locally administered MAC; no device is reachable.
"""
import copy
import time
import unittest

import _support
from _support import RealApp

IP = "198.51.100.9"
NET = {"link_up": True, "linkspeed_mbps": 1000,
       "lldp": {"ChassisID": "02:aa:00:00:00:a1", "SysName": "sw-alpha", "MgmtIP": "198.18.0.250"}}


class LearnerIsBehindThePollingGate(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.reads = []

        def fake(call, ip, role, timeout):
            self.reads.append(ip)
            return copy.deepcopy(NET)
        patcher = self.app.mock.patch.object(self.m.luma_netstatus, "read", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = dict(self.m._net_learn_attempts)
        self.addCleanup(lambda: (self.m._net_learn_attempts.clear(), self.m._net_learn_attempts.update(saved)))
        self.m._net_learn_attempts.clear()
        for name, value in (("_ws_get_hostname", lambda ip, t: ""), ("_ws_get_dante_info", lambda ip, t: {}),
                            ("_ws_get_dante_power_status", lambda ip, t: None),
                            ("_ws_get_hdcp_encoder", lambda ip, t: ""), ("_ws_get_hdcp_decoder", lambda ip, t: "")):
            self.app.patch(name, value)

    def sweep(self, unit):
        with self.m.cache_lock:
            self.m.cache_units.clear()
            self.m.cache_units[IP] = dict({"ip": IP, "mac": "02:00:5e:01:00:09", "role": "Decoder"}, **unit)
        self.m._cache_verify_state.update(running=False, finished=-1e9)
        self.m.APP.test_client().get("/api/cache")
        deadline = time.time() + 5
        while self.m._cache_verify_state["running"] and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.m._cache_verify_state["running"], "the sweep did not finish; nothing below means anything")

    def test_an_answering_unit_is_learned_by_the_sweep(self):
        # The control: without it, "no read" below could mean the sweep never reached the unit at all.
        self.sweep({})
        self.assertEqual(self.reads, [IP])

    def test_a_silent_unit_inside_its_back_off_is_not_read(self):
        self.sweep({"reach": {"misses": 6, "ts": int(time.time())}})
        self.assertEqual(self.reads, [], "the learner read a unit the polling back-off says to leave alone")
        self.assertNotIn("net", self.m.cache_units[IP])

    def test_a_silent_unit_whose_back_off_has_elapsed_is_read(self):
        self.sweep({"reach": {"misses": 6, "ts": int(time.time()) - 100000}})
        self.assertEqual(self.reads, [IP])
        self.assertEqual(self.m.cache_units[IP]["net"]["lldp"]["ChassisID"], NET["lldp"]["ChassisID"])


if __name__ == "__main__":
    unittest.main()
