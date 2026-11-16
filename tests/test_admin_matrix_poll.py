"""The administrator's Matrix page asks the devices once for every page, and not at all while hidden.

The page polled every ten seconds whether or not anyone was looking, and every request ran its own
sweep of every device: three open tabs cost three sweeps each interval, and a tab left in the
background kept sweeping the lab until it was closed. A measurement in a real browser showed it
(two refreshes in 26 s hidden, seven in 26 s with three tabs open).

Now the endpoint shares one sweep -- concurrent callers wait for the one in flight, a sweep a few
seconds old is shared too -- `force` (the Refresh button, a route just made) reads again, an
administrator's route drops what was remembered so the next read sees what it did, and the page
does not poll while hidden.
"""
import re
import threading
import time
import unittest

import _support
from test_routing_torture import Base, A, B, D1, D2, VIDEO, AUDIO, DISPLAYS, SOURCES
from _support import DISPLAY_1, DISPLAY_2, SOURCE_A

import luma_statecache

REFRESH = "/api/refresh_matrix_state"


class Sweeps(Base):
    """A counted stand-in for the sweep, behind a cache that remembers for a long time."""

    def setUp(self):
        super().setUp()
        self.sweeps = []
        self.app.patch("_observe_cache", luma_statecache.StateCache(ttl=30))

        def sweep():
            self.sweeps.append(time.monotonic())
            time.sleep(0.25)
            return len(self.sweeps)

        self.app.patch("_refresh_matrix_state_core", sweep)

    def refresh(self, client=None, **body):
        return (client or self.local).post(REFRESH, json=body)


class SharedSweepTests(Sweeps):
    def test_pages_asking_at_the_same_moment_share_one_sweep(self):
        statuses = []

        def ask():
            statuses.append(self.refresh(self.m.APP.test_client()).status_code)

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(statuses, [200] * 8)
        self.assertEqual(len(self.sweeps), 1, "eight pages cost eight sweeps")

    def test_a_page_asking_just_after_another_shares_its_sweep(self):
        self.refresh()
        self.refresh()
        self.assertEqual(len(self.sweeps), 1)

    def test_force_reads_again(self):
        self.refresh()
        self.refresh(force=True)
        self.assertEqual(len(self.sweeps), 2)

    def test_a_force_that_is_not_the_boolean_true_is_not_a_force(self):
        self.refresh()
        for junk in ("true", 1, "yes", [True]):
            self.refresh(force=junk)
        self.assertEqual(len(self.sweeps), 1)

    def test_the_answer_still_carries_the_units_and_whether_each_is_online(self):
        body = self.refresh().get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["units"]), 7)
        self.assertTrue(all("online" in u for u in body["units"]))


class AdministratorRouteTests(Sweeps):
    def route(self, **extra):
        body = {"decoder_ip": DISPLAY_1["ip"], "encoder_ip": SOURCE_A["ip"]}
        body.update(extra)
        return self.local.post("/api/route_matrix", json=body)

    def test_a_plain_route_drops_what_was_remembered(self):
        self.refresh()
        self.assertEqual(self.route().status_code, 200)
        self.refresh()
        self.assertEqual(len(self.sweeps), 2, "the read after a route was served from before it")

    def test_a_named_mode_route_drops_what_was_remembered_too(self):
        self.refresh()
        self.assertEqual(self.route(mode="av").status_code, 200)
        self.refresh()
        self.assertEqual(len(self.sweeps), 2)

    def test_a_route_that_fails_drops_it_as_well(self):
        self.refresh()
        self.sim.down.add(DISPLAY_1["ip"])
        self.assertNotEqual(self.route().status_code, 200)
        self.refresh()
        self.assertEqual(len(self.sweeps), 2, "a failed write may still have changed the device")


def strip_js_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?<![:'\"\w])//.*$", "", text, flags=re.M)


class PageHiddenTests(unittest.TestCase):
    """Source assertions, comments stripped first. The behaviour itself is measured in a real browser."""

    @classmethod
    def setUpClass(cls):
        import os
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "ui", "matrix.js"), encoding="utf-8") as handle:
            cls.source = strip_js_comments(handle.read())

    def interval_body(self):
        start = self.source.index("setInterval(() => {")
        return self.source[start:self.source.index("}, 10000);", start)]

    def test_the_periodic_poll_does_nothing_while_the_page_is_hidden(self):
        body = self.interval_body()
        self.assertIn("document.hidden", body)
        self.assertLess(body.index("document.hidden"), body.index("refreshAndRenderMatrix"),
                        "the guard comes before the read, not after it")

    def test_coming_back_to_the_page_reads_once(self):
        self.assertRegex(self.source, r"visibilitychange'?\s*,\s*\(\)\s*=>\s*\{\s*if \(!document\.hidden\)\s*refreshAndRenderMatrix\(\)")

    def test_the_refresh_button_and_a_confirmed_route_ask_for_a_new_read(self):
        self.assertIn("refreshAndRenderMatrix(true)", self.source)
        self.assertGreaterEqual(self.source.count("JSON.stringify({ force: true })"), 1)
        self.assertIn("force === true ? { force: true } : {}", self.source)

    def test_the_stripper_leaves_a_url_alone(self):
        self.assertIn("http://${ip}", strip_js_comments("const u = `http://${ip}/stream`; // a comment"))
        self.assertNotIn("a comment", strip_js_comments("const u = 1; // a comment"))


if __name__ == "__main__":
    unittest.main()
