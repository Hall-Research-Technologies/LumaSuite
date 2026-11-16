"""The mutation harness takes a per-tree lock, so a second run cannot measure its baseline against a tree the
first run is mutating at that moment. In the rc3 closeout three full sweeps were refused for a "failing baseline"
(LoginHardeningTests, LivePermissionTests, GenerationTests). Each failing assertion was the exact behaviour of a
deliberate defect in the sweep's own mutation list, and each refused run started while an earlier full sweep (about
three hours long) was still mutating the same files. It was not load and not an order dependency of the tests.

The lock path is redirected to a scratch file here: this test must never touch a real run's lock.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import mutate_security as ms  # noqa: E402


class RunLock(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mutlock-")
        self.path = os.path.join(self.dir, ".mutation-run.lock")
        patcher = mock.patch.object(ms, "LOCK", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_a_free_tree_is_locked_and_released(self):
        self.assertTrue(ms.take_run_lock())
        self.assertTrue(os.path.exists(self.path))
        ms.release_run_lock()
        self.assertFalse(os.path.exists(self.path))

    def test_a_live_holder_refuses_a_second_run(self):
        with open(self.path, "w") as handle:
            handle.write("%d started now\n" % os.getpid())          # this very process is alive
        self.assertFalse(ms.take_run_lock())
        self.assertTrue(os.path.exists(self.path), "the refused run must not remove the holder's lock")

    def test_a_dead_holder_does_not_block_for_ever(self):
        with open(self.path, "w") as handle:
            handle.write("4194303 started long ago\n")              # not a live process
        with mock.patch("psutil.pid_exists", return_value=False):
            self.assertTrue(ms.take_run_lock())

    def test_an_unreadable_lock_is_replaced(self):
        with open(self.path, "w") as handle:
            handle.write("not a pid")
        self.assertTrue(ms.take_run_lock())


if __name__ == "__main__":
    unittest.main()
