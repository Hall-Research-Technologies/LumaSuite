"""Display Groups at every size, with the writes counted rather than assumed.

A group route is a sequence of ordinary verified routes, one display after another,
and the claim worth proving is about the sequence: that no two writes ever overlap,
that the order is the membership order, that a failure anywhere changes nothing about
the rest, and that the aggregate is exactly what the members report. The instrumented
router counts the writes in flight and records the order; nothing here touches a
device, and every address is in the RFC 2544 benchmarking range.
"""
import threading
import time
import unittest

import _support
from _support import SOURCE_A, USER_PASSWORD, System, identity

import luma_groups
import luma_users

A = identity(SOURCE_A)
SIZES = (0, 1, 2, 4, 8, 16, 32, 64, 128)


def displays(count, prefix=0):
    out = []
    for i in range(count):
        out.append({"mac": "02:00:5e:%02x:%02x:%02x" % (0x40 + prefix, i // 250, i % 250 + 1),
                    "ip": "198.18.%d.%d" % (prefix * 2 + i // 250, i % 250 + 1),
                    "type": "decoder", "hostname": "Display %d.%d" % (prefix, i), "model": "TEST-D1",
                    "status": "idle", "dec": {"video": {"streamname": ""}}})
    return out


class Scaled(unittest.TestCase):
    def build(self, count):
        self.units = displays(count)
        self.s = System(units=[SOURCE_A] + self.units)
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        members = [identity(u) for u in self.units]
        made = self.s.remote.groups.create("Everyone", members)
        self.assertTrue(made.get("ok"), made)
        self.group = made["group"]
        self.members = members
        self.s.user("alex", sources=[A], groups=[self.group["id"]])
        self.client = self.s.sign_in("alex", USER_PASSWORD)

    def route(self):
        return self.s.post(self.client, "/api/user/route_group",
                           {"source": A, "group": self.group["id"]})


class SizeTests(Scaled):
    def test_every_size_routes_every_member_once_within_the_parallel_bound(self):
        for count in SIZES:
            with self.subTest(size=count):
                self.build(count)
                response = self.route()
                body = response.get_json()
                if count == 0:
                    self.assertFalse(body["ok"], "an empty group is not a success")
                    self.assertEqual(self.s.router.calls, [])
                    continue
                self.assertEqual(response.status_code, 200)
                self.assertEqual((body["routed"], body["total"], body["ok"]),
                                 (count, count, True))
                self.assertEqual(sorted(c[0] for c in self.s.router.calls),
                                 sorted(u["ip"] for u in self.units), "every member, exactly once")
                self.assertEqual([r["index"] for r in body["results"]], list(range(count)),
                                 "results stay in membership order")
                self.assertLessEqual(self.s.router.max_active, luma_groups.DISPATCH_WORKERS,
                                     "more writes in flight than the bound")
                self.assertEqual(len(set(c[0] for c in self.s.router.calls)), count,
                                 "a display was written twice")

    def test_a_failure_at_any_position_changes_nothing_about_the_others(self):
        for count in (2, 8, 33):
            for position in (0, count // 2, count - 1):
                with self.subTest(size=count, failing=position):
                    self.build(count)
                    self.s.router.fail[self.units[position]["ip"]] = "unreachable"
                    body = self.route().get_json()
                    self.assertFalse(body["ok"])
                    self.assertEqual((body["routed"], body["total"]), (count - 1, count))
                    self.assertEqual([r["ok"] for r in body["results"]],
                                     [i != position for i in range(count)])
                    self.assertEqual(len(self.s.router.calls), count,
                                     "none skipped, none rolled back")
                    self.assertLessEqual(self.s.router.max_active, luma_groups.DISPATCH_WORKERS)

    def test_every_member_failing_is_a_failure_with_an_exact_count(self):
        self.build(16)
        for unit in self.units:
            self.s.router.fail[unit["ip"]] = "not_verified"
        body = self.route().get_json()
        self.assertEqual((body["ok"], body["routed"], body["total"]), (False, 0, 16))

    def test_a_member_that_times_out_or_raises_is_that_members_failure_only(self):
        self.build(8)
        bad = self.units[3]["ip"]
        original = self.s.router.on_call

        def hook(decoder_ip, encoder_ip):
            if decoder_ip == bad:
                raise TimeoutError("the display did not answer")

        self.s.router.on_call = hook
        body = self.route().get_json()
        self.assertEqual((body["routed"], body["total"]), (7, 8))
        self.assertEqual([r["ok"] for r in body["results"]], [i != 3 for i in range(8)])
        self.assertLessEqual(self.s.router.max_active, luma_groups.DISPATCH_WORKERS)
        self.s.router.on_call = original


class ConcurrencyTests(Scaled):
    def test_overlapping_group_routes_never_write_one_display_twice_at_once(self):
        self.build(12)
        # A second group sharing six displays with the first, and a second user of it.
        shared = self.members[6:] + [identity(u) for u in displays(6, prefix=1)]
        other = self.s.remote.groups.create("Overlap", shared)["group"]
        self.s.user("sam", sources=[A], groups=[other["id"]])
        sam = self.s.sign_in("sam", USER_PASSWORD)

        active, worst, lock = {}, [0], threading.Lock()

        def watch(decoder_ip, encoder_ip):
            with lock:
                active[decoder_ip] = active.get(decoder_ip, 0) + 1
                worst[0] = max(worst[0], active[decoder_ip])
            time.sleep(0.01)
            with lock:
                active[decoder_ip] -= 1

        self.s.router.on_call = watch
        results = {}

        def go(name, client, group_id):
            results[name] = self.s.post(client, "/api/user/route_group",
                                        {"source": A, "group": group_id})

        threads = [threading.Thread(target=go, args=("alex", self.client, self.group["id"])),
                   threading.Thread(target=go, args=("sam", sam, other["id"]))]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertFalse(any(t.is_alive() for t in threads), "a group route never finished (deadlock)")
        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(worst[0], 1, "one display was written by two routes at the same moment")
        for name, response in results.items():
            self.assertIn(response.status_code, (200, 409), name)


if __name__ == "__main__":
    unittest.main()
