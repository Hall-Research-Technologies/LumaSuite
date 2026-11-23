"""Measure routing on this tree: what the server costs, split into its parts.

Not a test and not collected by the suite (the name does not start with `test_`). It drives the real
routes -- `/api/user/route_multi`, `/api/user/state`, `/api/user/matrix` and, where the tree has it,
`/api/user/route_check` -- against `_devicesim.DeviceSim`, which behaves as the measured decoders do,
with a per-call round trip and a per-write time in the range measured on the bench. Nothing leaves
loopback and the hardware fence is on. Real-hardware timings are a separate, live step.

    python tests/bench_routing.py [--runs N] [--settle SECONDS]

Per scenario it prints, in milliseconds (median of the runs, min/max beside it):

  response        the POST as a client sees it: submitted -> answered (the server verifies inside it)
  command         the longest decoder write inside the request (the route command itself)
  verification    from the last write's end to the last read-back's end (the confirmation reads)
  state           `GET /api/user/state` taken straight after, which is the page's "refresh"

`--settle S` makes every display take S seconds after a write before it reports the new stream, which
is what a slow display does and what a route's three-read window can miss.
"""
import argparse
import json
import statistics
import sys
import time
import unittest

import _support                      # installs the fence and isolates the data dir first

from test_routing_torture import (A, B, C, D1, D2, D3, D4, Base)

ROUND_TRIP = 0.04       # seconds per device call (a LAN round trip plus the unit's own work)
WRITE_TIME = 0.25       # seconds per write: the middle of the 150-400 ms measured


class Bench(Base):
    runs = 5
    settle = 0.0

    def setUp(self):
        super().setUp()
        self.sim.write_pause = WRITE_TIME
        for ip in list(self.sim.decoders) + list(self.sim.encoders):
            self.sim.slow[ip] = ROUND_TRIP
        for decoder in self.sim.decoders.values():
            decoder.settle = self.settle
        self.addCleanup(lambda: setattr(self.sim, "write_pause", 0.0))

    # --- one measured request ------------------------------------------------------------
    def timed(self, client, body):
        mark = len(self.sim.call_log)
        began = time.monotonic()
        response = self.remote.post(client, "/api/user/route_multi", body)
        answered = time.monotonic()
        status = response.status_code
        calls = self.sim.call_log[mark:]
        writes = [c for c in calls if c[1].endswith(".Set")]
        reads = [c for c in calls if not c[1].endswith(".Set")]
        command = max((w[3] - w[2] for w in writes), default=0.0)
        last_write_end = max((w[3] for w in writes), default=began)
        verification = max(0.0, max((r[3] for r in reads if r[2] >= last_write_end - 0.02),
                                    default=last_write_end) - last_write_end)
        began_state = time.monotonic()
        state = client.get("/api/user/state")
        state_ms = (time.monotonic() - began_state) * 1000
        return {"status": status, "response": (answered - began) * 1000,
                "command": command * 1000, "verification": verification * 1000,
                "state": state_ms, "body": response.get_json()}

    def scenario(self, name, client, bodies):
        samples = []
        for run in range(self.runs):
            for index, body in enumerate(bodies):
                # Alternate the source so every route really changes a display.
                body = dict(body, source=[A, B, C][(run + index) % 3])
                samples.append(self.timed(client, body))
        def stat(key):
            values = [s[key] for s in samples]
            return {"median": round(statistics.median(values)), "min": round(min(values)),
                    "max": round(max(values))}
        ok = sum(1 for s in samples if s["status"] == 200 and (s["body"] or {}).get("ok"))
        return {"scenario": name, "requests": len(samples), "ok": ok,
                "response": stat("response"), "command": stat("command"),
                "verification": stat("verification"), "state": stat("state")}

    def test_measure(self):
        gid_a = self.group([D1, D2], name="Group A")
        gid_b = self.group([D3, D4], name="Group B")
        client = self.make_user(destinations=(D1, D2, D3, D4), groups=(gid_a, gid_b))
        std = lambda **kw: dict({"displays": [], "groups": [], "walls": [], "interface": "standard"}, **kw)
        mx = lambda **kw: dict({"displays": [], "groups": [], "walls": [], "interface": "matrix",
                                "mode": "av"}, **kw)
        results = [
            self.scenario("single route", client, [std(displays=[D1])]),
            self.scenario("five consecutive routes (one request each, one display each)", client,
                          [std(displays=[d]) for d in (D1, D2, D3, D4, D1)]),
            self.scenario("one source -> four displays", client, [std(displays=[D1, D2, D3, D4])]),
            self.scenario("one source -> two groups", client, [std(groups=[gid_a, gid_b])]),
            self.scenario("one source -> one display + one group", client,
                          [std(displays=[D3], groups=[gid_a])]),
            self.scenario("five consecutive matrix cells", client,
                          [mx(displays=[d]) for d in (D1, D2, D3, D4, D1)]),
        ]
        print("BENCH " + json.dumps({"settle_s": self.settle, "round_trip_ms": ROUND_TRIP * 1000,
                                     "write_ms": WRITE_TIME * 1000, "runs": self.runs,
                                     "results": results}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--settle", type=float, default=0.0)
    args, rest = parser.parse_known_args()
    Bench.runs, Bench.settle = args.runs, args.settle
    suite = unittest.TestSuite([Bench("test_measure")])
    outcome = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(suite)
    sys.exit(0 if outcome.wasSuccessful() else 1)
