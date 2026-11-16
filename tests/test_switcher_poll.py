"""A CS31 switcher is read by the shared matrix refresh, so it is online while it answers.

Found by packaged acceptance: the refresh chose how to read a unit from the word "decoder" in its
role, and a switcher's type is "switcher", so the branch written for it never ran. Nothing was read,
every sweep counted a miss, and after two sweeps the matrix called a switcher that answered every
read offline -- which also made a User's route to it a 409.

What is pinned here:

  * a switcher is read once per refresh, through the switcher transport (no password, never the
    decoder call that carries one);
  * whether it ANSWERED is a fact about the reply. An idle switcher with no stream selected answers
    with an empty name (or none) and is perfectly healthy; a stream name is recorded when there is one;
  * a switcher that truly stops answering goes offline only after the tolerated number of misses,
    and one answer brings it back;
  * a User's route to a switcher that answers is not refused as offline;
  * other unit types read exactly as before.
"""
import re
import unittest

import _support  # noqa: F401  (installs the fence and isolates the data dir first)
from _support import DISPLAY_1, DISPLAY_2, SOURCE_A
from test_routing_torture import Base, A, D1

import luma_liveness
import luma_projection

SW = DISPLAY_1["ip"]
READ = "SelectVideoStream.Get"


class SwitcherBase(Base):
    def setUp(self):
        super().setUp()
        with self.m.cache_lock:
            self.m.cache_units[SW]["type"] = "switcher"
            self.m.cache_units[SW]["role"] = "Switcher"
        self.dev = self.app.devices

    def sweeps(self, n):
        for _ in range(n):
            self.m._refresh_matrix_state_core()

    def reads(self):
        return [m for (ip, m) in self.dev.calls if ip == SW and m == READ]

    def online(self):
        with self.m.cache_lock:
            return luma_projection.is_online(self.m.cache_units[SW])

    def misses(self):
        with self.m.cache_lock:
            return self.m.cache_units[SW][luma_liveness.FIELD]["misses"]


class ReadOnceTests(SwitcherBase):
    def test_a_switcher_is_read_at_least_once_per_refresh_and_not_more(self):
        for i in range(1, 4):
            self.m._refresh_matrix_state_core()
            self.assertEqual(len(self.reads()), i, "one read per refresh, none missing")

    def test_it_is_read_through_the_switcher_transport_never_the_password_call(self):
        self.sweeps(2)
        self.assertEqual(self.sim.methods(SW), [], "a switcher was sent the decoder call that carries a password")

    def test_an_encoder_and_an_ordinary_decoder_are_still_read_as_before(self):
        before = len(self.sim.methods())
        self.sweeps(1)
        self.assertGreater(len(self.sim.methods()), before)
        self.assertIn("SelectVideoStream.Get", self.sim.methods(DISPLAY_2["ip"]))
        self.assertIn("VideoMultiIpStream.Get", self.sim.methods(SOURCE_A["ip"]))


class AnsweringSwitcherTests(SwitcherBase):
    def test_it_stays_online_across_many_refreshes_with_a_stream_selected(self):
        self.dev.decoder_stream[SW] = "stream-alpha"
        for i in range(6):
            self.m._refresh_matrix_state_core()
            self.assertTrue(self.online(), "offline after refresh %d although it answers" % (i + 1))
        self.assertEqual(self.misses(), 0)
        with self.m.cache_lock:
            self.assertEqual(self.m.cache_units[SW]["dec"]["video"]["streamname"], "stream-alpha")

    def test_an_idle_switcher_with_an_empty_stream_name_is_online(self):
        self.dev.decoder_stream[SW] = ""
        self.sweeps(6)
        self.assertTrue(self.online(), "an idle switcher answered every read and was called offline")
        self.assertEqual(self.misses(), 0)

    def test_an_idle_switcher_whose_reply_has_no_stream_name_at_all_is_online(self):
        self.dev.decoder_stream.pop(SW, None)
        self.sweeps(6)
        self.assertTrue(self.online())

    def test_an_empty_name_is_not_recorded_over_a_known_one(self):
        self.dev.decoder_stream[SW] = "stream-alpha"
        self.sweeps(1)
        self.dev.decoder_stream[SW] = ""
        self.sweeps(1)
        with self.m.cache_lock:
            self.assertEqual(self.m.cache_units[SW]["dec"]["video"]["streamname"], "stream-alpha")

    def test_a_reply_that_is_not_an_object_is_a_miss_not_an_answer(self):
        self.app.patch("_ws_call_switcher_no_password", lambda ip, method, params=None, timeout=None: {"result": "???"})
        self.sweeps(luma_liveness.MISS_LIMIT)
        self.assertFalse(self.online())


class SilentSwitcherTests(SwitcherBase):
    def test_it_goes_offline_only_after_the_tolerated_number_of_misses(self):
        self.sweeps(1)
        self.assertTrue(self.online())
        self.dev.unreachable.add(SW)
        for missed in range(1, luma_liveness.MISS_LIMIT):
            self.sweeps(1)
            self.assertTrue(self.online(), "called offline after %d miss(es); the limit is %d" % (missed, luma_liveness.MISS_LIMIT))
        self.sweeps(1)
        self.assertFalse(self.online())

    def test_one_answer_brings_it_back_at_once(self):
        self.dev.unreachable.add(SW)
        self.sweeps(luma_liveness.MISS_LIMIT)
        self.assertFalse(self.online())
        self.dev.unreachable.discard(SW)
        self.sweeps(1)
        self.assertTrue(self.online())
        self.assertEqual(self.misses(), 0)


class UserRouteTests(SwitcherBase):
    def test_a_user_route_to_a_switcher_that_answers_is_not_refused_as_offline(self):
        client = self.make_user()
        self.dev.decoder_stream[SW] = ""            # idle: the case that read as unanswered
        self.sweeps(luma_liveness.MISS_LIMIT + 3)   # more refreshes than the miss limit tolerates
        response = self.route(client, A, displays=[D1], interface="standard")
        body = response.get_json()
        self.assertNotEqual(response.status_code, 409, body)
        self.assertEqual([r["result"] for r in body["results"]], ["verified"], body)
        self.assertEqual(self.dev.decoder_stream[SW], "stream-alpha")

    def test_the_same_route_is_refused_as_offline_once_it_truly_stops_answering(self):
        client = self.make_user()
        self.dev.unreachable.add(SW)
        self.sweeps(luma_liveness.MISS_LIMIT)
        response = self.route(client, A, displays=[D1], interface="standard")
        self.assertEqual([r["result"] for r in response.get_json()["results"]], ["offline"])


class SourceShapeTests(unittest.TestCase):
    """The refresh is one function; its switcher read is asserted on its source, comments stripped."""

    @classmethod
    def setUpClass(cls):
        import lumaserver
        import inspect
        src = inspect.getsource(lumaserver._refresh_matrix_state_core)
        cls.src = re.sub(r"(?m)^\s*#.*$", "", src)

    def test_the_branch_is_chosen_by_type_as_well_as_by_role(self):
        self.assertRegex(self.src, r"device_type\s*==\s*'switcher'\s+or\s+'decoder'\s+in\s+role")

    def test_answered_is_set_from_the_reply_not_from_the_stream_name(self):
        self.assertNotRegex(self.src, r"device_type\s*==\s*'switcher'\s+and\s+streamname")


if __name__ == "__main__":
    unittest.main()
