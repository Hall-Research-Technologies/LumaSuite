"""Used only by test_fence: a test that tries to reach a device, run as `python -m unittest`.

It passes only if the fence refused the attempt, so a run in which the fence was absent fails.
Not named test_*.py, so the runner and CI do not collect it on their own.
"""
import socket
import unittest


class ReachForADevice(unittest.TestCase):
    def test_the_attempt_is_refused(self):
        with self.assertRaises(OSError) as caught:
            socket.create_connection(("198.51.100.5", 80), timeout=1)
        self.assertEqual(type(caught.exception).__name__, "FenceError")
