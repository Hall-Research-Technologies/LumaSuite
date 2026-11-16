"""What a stranger can make the sign-in remember is bounded.

A sign-in is the one request anybody on the network can make, and the name in it is
theirs to choose. Three things grew with it:

  * `locked_out` stored an empty record for every name it was asked about and never
    removed one, so a caller choosing names grew the throttle without limit;
  * the typed name was kept whole in the throttle key and in the audit, whose file is
    rewritten on every flush, so one request could put a very large entry in it;
  * nothing ever swept a record whose window had passed unless that exact name came
    back.

No password is correct and no device is involved; each store is a throwaway.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _support                                           # noqa: E402,F401  (the fence)
import luma_users as users                                # noqa: E402


#: The contract, written out rather than read from the constant under test: a test that
#: compares against the very name a mutation changes can never fail.
BOUND = 128


class ThrottleBounds(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="luma-throttle-"))
        self.store = users.UserStore(self.dir / "users.json", self.dir / "audit.json")

    def test_asking_about_names_nobody_failed_with_stores_nothing(self):
        for i in range(3000):
            self.assertFalse(self.store.locked_out("name-%d" % i, "198.51.100.7"))
        self.assertEqual(len(self.store._failures), 0)

    def test_a_name_that_has_failed_is_still_remembered_and_still_locks(self):
        for _ in range(users.MAX_FAILURES):
            self.store.authenticate("someone", "wrong", source="198.51.100.7")
        self.assertTrue(self.store.locked_out("someone", "198.51.100.7"))
        self.assertEqual(len(self.store._failures), 1)

    def test_a_very_long_name_is_bounded_in_the_throttle_and_the_audit(self):
        huge = "x" * 200_000
        self.store.authenticate(huge, "wrong", source="198.51.100.7")
        for name, source in self.store._failures:
            self.assertLessEqual(len(name), BOUND)
            self.assertLessEqual(len(source), BOUND)
        recorded = [e["detail"]["username"] for e in self.store._audit
                    if e["event"] == "login.failure"]
        self.assertEqual(len(recorded), 1)
        self.assertLessEqual(len(recorded[0]), BOUND)

    def test_a_throttled_attempt_is_bounded_in_the_audit_too(self):
        huge = "y" * 200_000
        for _ in range(users.MAX_FAILURES + 1):
            self.store.authenticate(huge, "wrong", source="198.51.100.7")
        throttled = [e for e in self.store._audit if e["event"] == "login.throttled"]
        self.assertTrue(throttled, "the precondition: the pair was throttled")
        for entry in throttled:
            self.assertLessEqual(len(entry["detail"]["username"]),
                                 BOUND)

    def test_a_flood_of_distinct_names_inside_the_window_is_capped(self):
        """Expiry bounds growth only by the attacker's rate; this bounds it outright."""
        for i in range(9_000):
            self.store._record_failure("flood-%d" % i, "198.51.100.%d" % (i % 250))
        self.assertLessEqual(len(self.store._failures), 8192)

    def test_a_locked_account_survives_the_flood(self):
        for _ in range(users.MAX_FAILURES):
            self.store.authenticate("victim", "wrong", source="198.51.100.7")
        self.assertTrue(self.store.locked_out("victim", "198.51.100.7"))
        for i in range(9_000):
            self.store._record_failure("flood-%d" % i, "198.51.100.%d" % (i % 250))
        self.assertTrue(self.store.locked_out("victim", "198.51.100.7"),
                        "a flood of junk names released a real lockout")
        self.assertLessEqual(len(self.store._failures), 8192)

    def test_when_every_record_is_a_lockout_a_new_name_is_not_remembered(self):
        with mock.patch.object(users, "THROTTLE_MAX_KEYS", 4):
            for i in range(4):
                for _ in range(users.MAX_FAILURES):
                    self.store._record_failure("locked-%d" % i, "198.51.100.7")
            self.store._record_failure("newcomer", "198.51.100.7")
            self.assertEqual(len(self.store._failures), 4)
            self.assertNotIn(("newcomer", "198.51.100.7"), self.store._failures)

    def test_expired_records_are_swept_without_that_name_returning(self):
        past = users._now() - users.LOCKOUT_SECONDS - 5
        for i in range(users.THROTTLE_SWEEP_AT + 10):
            self.store._failures[("old-%d" % i, "198.51.100.7")] = [past]
        self.store.authenticate("fresh", "wrong", source="198.51.100.8")
        self.assertEqual(list(self.store._failures), [("fresh", "198.51.100.8")])

    def test_the_sweep_leaves_recent_failures_alone(self):
        past = users._now() - users.LOCKOUT_SECONDS - 5
        for i in range(users.THROTTLE_SWEEP_AT):
            self.store._failures[("old-%d" % i, "s")] = [past]
        self.store._failures[("recent", "s")] = [users._now()]
        self.store.authenticate("fresh", "wrong", source="s2")
        self.assertIn(("recent", "s"), self.store._failures)


if __name__ == "__main__":
    unittest.main()
