"""The fence itself: what it blocks, what it allows, and that it is present however a test is run."""
import errno
import os
import socket
import subprocess
import sys
import unittest

import _support  # noqa: F401  (installs the fence)

import _fence

ROUTABLE = "198.51.100.5"          # documentation range: unroutable, but the fence treats it as a host
ROOT = _support.ROOT


def refused_before(count):
    return sum(_fence.attempts.values()) > count


class FenceTests(unittest.TestCase):
    def setUp(self):
        self.before = sum(_fence.attempts.values())

    def test_it_is_installed(self):
        self.assertTrue(_fence.installed())

    def test_a_tcp_connection_to_a_host_is_refused_and_counted(self):
        with self.assertRaises(_fence.FenceError):
            socket.create_connection((ROUTABLE, 80), timeout=1)
        self.assertTrue(refused_before(self.before))
        self.assertIn(("tcp", ROUTABLE), _fence.attempts)

    def test_the_refusal_is_an_oserror_so_callers_handle_it_as_a_network_failure(self):
        self.assertTrue(issubclass(_fence.FenceError, OSError))

    def test_a_probe_reads_as_refused_without_any_connection_being_made(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        self.assertEqual(sock.connect_ex((ROUTABLE, 80)), errno.ECONNREFUSED if os.name != "nt" else 111)

    def test_a_udp_datagram_to_a_host_is_refused(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        with self.assertRaises(_fence.FenceError):
            sock.sendto(b"\x00" * 16, (ROUTABLE, 5000))

    def test_a_websocket_connection_is_refused(self):
        import websocket
        with self.assertRaises(_fence.FenceError):
            websocket.create_connection("ws://%s/wsapp/" % ROUTABLE, timeout=1)

    def test_an_http_request_is_refused_through_the_socket_it_needs(self):
        import requests
        with self.assertRaises(requests.exceptions.ConnectionError):
            requests.get("http://%s/thumbnail/thumbnail1.jpg" % ROUTABLE, timeout=1)
        self.assertIn(("tcp", ROUTABLE), _fence.attempts)

    def test_resolving_a_name_is_refused_because_it_is_already_a_network_query(self):
        with self.assertRaises(_fence.FenceError):
            socket.getaddrinfo("device.example.test", 80)
        self.assertIn(("dns", "device.example.test"), _fence.attempts)

    def test_literal_addresses_and_the_loopback_name_still_resolve(self):
        self.assertTrue(socket.getaddrinfo("127.0.0.1", 80))
        self.assertTrue(socket.getaddrinfo("localhost", 80))
        self.assertTrue(socket.getaddrinfo(ROUTABLE, 80))            # a literal needs no query

    def test_programs_that_talk_to_the_network_are_refused_by_name(self):
        for program in ("ping", "ping.exe", "tracert", "nslookup", "curl"):
            with self.assertRaises(_fence.FenceError, msg=program):
                subprocess.Popen([program, ROUTABLE])
        with self.assertRaises(_fence.FenceError):
            subprocess.Popen("ping -c 1 " + ROUTABLE, shell=True)
        self.assertIn(("exec", "ping"), _fence.attempts)

    def test_ordinary_programs_still_run(self):
        done = subprocess.run([sys.executable, "-c", "print(7)"], capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "7")

    def test_loopback_stays_open_because_a_test_talking_to_it_is_talking_to_itself(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(("127.0.0.1", server.getsockname()[1]), timeout=2)
        self.addCleanup(client.close)
        peer, _ = server.accept()
        self.addCleanup(peer.close)
        client.sendall(b"hello")
        self.assertEqual(peer.recv(5), b"hello")
        self.assertEqual(sum(_fence.attempts.values()), self.before, "loopback must not be counted as a refusal")

    def test_installing_twice_does_not_stack_spies(self):
        before = socket.socket
        _fence.install()
        _fence.install()
        self.assertIs(socket.socket, before)

    def test_documentation_ranges_are_reported_as_unroutable_and_others_are_not(self):
        self.assertFalse(_fence.reachable("tcp 192.0.2.10"))
        self.assertFalse(_fence.reachable("ws://198.51.100.9/wsapp/"))
        self.assertFalse(_fence.reachable("203.0.113.7"))
        self.assertTrue(_fence.reachable("10.1.2.3"))
        self.assertTrue(_fence.reachable("no address here"))

    def test_the_preview_fetch_the_user_page_uses_cannot_reach_a_device_either(self):
        import lumaserver
        with self.assertRaises(Exception):
            lumaserver._preview_http_get("http://%s/thumbnail/thumbnail1.jpg" % ROUTABLE, timeout=1)


class PresentWhateverRunsTheTestsTests(unittest.TestCase):
    """The fence belongs to the tests, not to one runner remembering to install it."""

    def probe(self, code, *args):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=env, timeout=60)

    def test_importing_the_test_package_alone_installs_it(self):
        done = self.probe(
            "import tests, socket\n"
            "try:\n"
            "    socket.create_connection(('%s', 80), timeout=1)\n"
            "    print('REACHED')\n"
            "except OSError as e:\n"
            "    print(type(e).__name__)\n" % ROUTABLE)
        self.assertEqual(done.stdout.strip(), "FenceError", done.stdout + done.stderr)

    def test_the_package_also_points_the_application_at_a_scratch_folder_holding_no_real_data(self):
        done = self.probe(
            "import tests, os, json\n"
            "folder = os.environ['LUMA_DATA_DIR']\n"
            "print(os.path.abspath(folder) != os.path.abspath('.'))\n"
            "print(json.load(open(os.path.join(folder, 'units_cache.json'))))\n")
        self.assertEqual(done.stdout.split(), ["True", "[]"], done.stdout + done.stderr)

    def test_a_suite_run_through_python_dash_m_unittest_is_fenced_with_no_help_from_a_runner(self):
        # A fresh interpreter, started the way an IDE or a plain unittest invocation starts one.
        done = subprocess.run([sys.executable, "-m", "unittest", "tests.fence_probe"], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("OK", done.stderr)


if __name__ == "__main__":
    unittest.main()
