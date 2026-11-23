"""LUMA-REL-001: the Device Manager's detail refresh with more than 32 units.

The page used to post every visible address to /api/scan in one request. A scan expression names at most
`luma_scanplan.MAX_RANGES` (32) ranges, so with 33 or more units the server answered HTTP 400 (too_many_ranges) to every
refresh. The server's limit is deliberate and stays: these tests hold both halves, that the limit is unchanged and
that the page's batch size can never exceed it, and that the scan reply names the units that answered so the page can
leave silent ones alone. The page's own behaviour is held in tests/detail_batch_test.js.

Fixtures are synthetic documentation addresses; the transport is stubbed and nothing leaves loopback.
"""
import copy
import os
import re
import unittest

import _support
from _support import RealApp

import luma_scanplan

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8").read()


def addresses(count, start=1):
    return ["198.51.100.%d" % (start + i) if start + i < 255 else "198.51.101.%d" % (start + i - 254) for i in range(count)]


def unit_for(ip):
    last = int(ip.rsplit(".", 1)[1])
    return {"ip": ip, "mac": "02:00:5e:31:%02x:%02x" % (int(ip.split(".")[2]) & 255, last), "type": "decoder", "role": "Decoder",
            "model": "TEST-D1", "hostname": "unit-" + ip, "status": "idle"}


class ServerLimitIsUnchangedTests(unittest.TestCase):
    def test_the_range_limit_is_still_32(self):
        self.assertEqual(luma_scanplan.MAX_RANGES, 32)

    def test_the_page_batches_within_the_limit(self):
        size = int(re.search(r"const DETAIL_BATCH_SIZE = (\d+);", PAGE).group(1))
        self.assertGreaterEqual(size, 1)
        self.assertLessEqual(size, luma_scanplan.MAX_RANGES,
                             "a batch larger than the server accepts brings back the 400 that this fixed")

    def test_the_limit_check_still_refuses_33_and_accepts_32(self):
        self.assertEqual(luma_scanplan.check(",".join(addresses(32))), 32)
        with self.assertRaises(luma_scanplan.ScanRangeError) as caught:
            luma_scanplan.check(",".join(addresses(33)))
        self.assertEqual(caught.exception.reason, "too_many_ranges")


class ScanEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.client = self.m.APP.test_client()
        self.silent = set()
        self.refusing = set()
        with self.m.cache_lock:
            self.m.cache_units.clear()

        def scan_one(ip):
            if ip in self.silent:
                return None
            if ip in self.refusing:
                return {"_error": "auth", "_error_details": {}}
            return unit_for(ip)
        self.app.patch("_scan_one", scan_one)

    def post(self, ips):
        return self.client.post("/api/scan", json={"targets": ",".join(ips)})

    def test_33_addresses_in_one_request_are_still_refused_by_the_server(self):
        response = self.post(addresses(33))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason"], "too_many_ranges")

    def test_32_addresses_are_accepted_and_every_unit_is_ingested(self):
        response = self.post(addresses(32))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["answered"]), 32)
        with self.m.cache_lock:
            self.assertEqual(len(self.m.cache_units), 32)

    def test_consecutive_batches_cover_an_inventory_larger_than_the_limit(self):
        ips = addresses(100)
        for at in range(0, len(ips), 32):
            self.assertEqual(self.post(ips[at:at + 32]).status_code, 200)
        with self.m.cache_lock:
            self.assertEqual(sorted(self.m.cache_units), sorted(ips))

    def test_the_reply_names_the_units_that_answered_and_not_the_silent_ones(self):
        ips = addresses(6)
        self.silent = {ips[1], ips[4]}
        answered = self.post(ips).get_json()["answered"]
        self.assertEqual(answered, [ips[0], ips[2], ips[3], ips[5]])

    def test_a_unit_that_refused_our_credentials_still_answered(self):
        ips = addresses(3)
        self.refusing = {ips[1]}
        self.assertIn(ips[1], self.post(ips).get_json()["answered"])

    def test_the_answered_list_is_in_numeric_order_whatever_order_workers_finish(self):
        ips = ["198.51.100.9", "198.51.100.10", "198.51.100.100", "198.51.100.2"]
        answered = self.post(ips).get_json()["answered"]
        self.assertEqual(answered, ["198.51.100.2", "198.51.100.9", "198.51.100.10", "198.51.100.100"])

    def test_nothing_answering_gives_an_empty_list_not_a_missing_one(self):
        ips = addresses(3)
        self.silent = set(ips)
        body = self.post(ips).get_json()
        self.assertEqual(body["answered"], [])
        self.assertTrue(body["ok"])


if __name__ == "__main__":
    unittest.main()
