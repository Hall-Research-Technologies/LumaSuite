"""LUMA-MA-010 -- the local application listener's port: controller, file, routes.

Real loopback sockets throughout: a port is occupied by a real listener, and the
controller really binds, serves and releases. Ports come from the operating system's
ephemeral range (all >= 27000) and every server a test starts is stopped by that test.

Scope reminder: this is the LOCAL listener (127.0.0.1). The Remote Access listener is a
different listener and these tests assert it is not touched.
"""
import copy
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import _support  # noqa: F401  (installs the hardware fence and the scratch data dir first)

from flask import Flask, jsonify

import luma_access
import luma_portcontrol as pc
import luma_remote
import lumaserver

HOST = "127.0.0.1"


_next = [27000 + (os.getpid() * 13) % 6000]


def free_port():
    """A port >= 27000 nothing holds right now. Handed out once each, so tests do not collide."""
    while True:
        port = _next[0]
        _next[0] += 1
        if _next[0] > 60000:
            _next[0] = 27000
        try:
            with socket.socket() as s:
                s.bind((HOST, port))
            return port
        except OSError:
            continue


class Holder:
    """A real listener occupying a port, the way another program would."""

    def __init__(self, port=None):
        self.sock = socket.socket()
        self.sock.bind((HOST, port or free_port()))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def release(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def make_app():
    app = Flask("local-port-test")

    @app.get("/api/app_version")
    def version():
        return jsonify({"ok": True, "version": "test"})
    return app


def get_json(port, path="/api/app_version"):
    with urllib.request.urlopen("http://%s:%d%s" % (HOST, port, path), timeout=5) as r:
        return json.loads(r.read().decode())


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="luma-port-")
        self.holders = []
        self.controllers = []
        self.addCleanup(self._cleanup)
        # automatic mode starts at the default port; the tests use their own so a developer's
        # machine that really has 8090 busy cannot change the outcome
        self.default = free_port()
        original = pc.DEFAULT_PORT
        pc.DEFAULT_PORT = self.default
        self.addCleanup(setattr, pc, "DEFAULT_PORT", original)

    def _cleanup(self):
        for ctl in self.controllers:
            ctl.stop()
            ctl.join_background(10)
        for h in self.holders:
            h.release()

    def hold(self, port=None):
        h = Holder(port)
        self.holders.append(h)
        return h

    def controller(self, **kw):
        ctl = pc.PortController(make_app(), HOST, self.dir, **kw)
        self.controllers.append(ctl)
        return ctl

    @property
    def file(self):
        return os.path.join(self.dir, pc.FILE_NAME)

    def write_config(self, mode, port):
        with open(self.file, "w", encoding="utf-8") as h:
            json.dump({"local_port_mode": mode, "configured_local_fixed_port": port}, h)

    def file_bytes(self):
        with open(self.file, "rb") as h:
            return h.read()


class ValidationTests(unittest.TestCase):
    def test_valid_ports_are_accepted_as_numbers_or_digit_text(self):
        for value, expected in ((1024, 1024), (65535, 65535), ("8095", 8095), (" 27001 ", 27001)):
            self.assertEqual(pc.validate_port(value), expected)

    def test_5_invalid_port_values_are_rejected_with_a_sentence(self):
        for bad in ("", "   ", None, "abc", "80x", "-1", "1023", 1023, 0, 65536, "65536", "8090.5",
                    "٨٠٩٠", True, False, [], {}, "1e4", "0x1F90", "9" * 40):
            with self.assertRaises(pc.PortError, msg=repr(bad)) as ctx:
                pc.validate_port(bad)
            self.assertTrue(str(ctx.exception).endswith("."), repr(bad))

    def test_the_session_override_is_wider_but_still_bounded(self):
        self.assertEqual(pc.validate_session_port("80"), 80)
        for bad in ("", "x", "0", "65536", "-3"):
            with self.assertRaises(pc.PortError):
                pc.validate_session_port(bad)


class SettingsFileTests(Base):
    def test_1_automatic_mode_is_the_default(self):
        settings = pc.PortSettings(self.dir).load()
        self.assertEqual(settings.mode, pc.MODE_AUTOMATIC)
        self.assertIsNone(settings.fixed_port)
        self.assertFalse(os.path.exists(self.file), "reading the default writes nothing")

    def test_a_saved_fixed_port_round_trips(self):
        self.assertTrue(pc.PortSettings(self.dir).save(pc.MODE_FIXED, 27555))
        settings = pc.PortSettings(self.dir).load()
        self.assertEqual((settings.mode, settings.fixed_port), (pc.MODE_FIXED, 27555))

    def test_no_state_of_the_file_can_stop_a_start(self):
        """Every unusable file is quarantined and reads as automatic, and the server still starts."""
        bad_files = {
            "empty": b"",
            "whitespace": b"  \n",
            "not json": b"{nope",
            "binary": bytes(range(256)),
            "a list": b"[1, 2]",
            "a number": b"8090",
            "null": b"null",
            "unknown mode": json.dumps({"local_port_mode": "sometimes"}).encode(),
            "mode wrong type": json.dumps({"local_port_mode": 3}).encode(),
            "fixed without a port": json.dumps({"local_port_mode": "fixed"}).encode(),
            "port as text": json.dumps({"local_port_mode": "fixed", "configured_local_fixed_port": "8095"}).encode(),
            "port as bool": json.dumps({"local_port_mode": "fixed", "configured_local_fixed_port": True}).encode(),
            "port as float": json.dumps({"local_port_mode": "fixed", "configured_local_fixed_port": 8095.0}).encode(),
            "port too low": json.dumps({"local_port_mode": "fixed", "configured_local_fixed_port": 80}).encode(),
            "port too high": json.dumps({"local_port_mode": "fixed", "configured_local_fixed_port": 70000}).encode(),
            "automatic with a bad port": json.dumps({"local_port_mode": "automatic", "configured_local_fixed_port": -1}).encode(),
        }
        for label, content in bad_files.items():
            with self.subTest(label):
                path = self.file
                for leftover in (path, path + ".corrupt"):
                    if os.path.exists(leftover):
                        os.remove(leftover)
                with open(path, "wb") as h:
                    h.write(content)
                ctl = self.controller()
                outcome = ctl.startup()
                self.assertTrue(outcome["ok"], (label, outcome))
                self.assertEqual(ctl.mode, pc.MODE_AUTOMATIC, label)
                self.assertTrue(os.path.exists(path + ".corrupt"), "%s: kept for inspection" % label)
                self.assertFalse(os.path.exists(path), "%s: the unusable file is out of the way" % label)
                ctl.stop()

    def test_a_file_that_cannot_be_read_at_all_is_still_automatic(self):
        os.mkdir(self.file)                       # a directory where the file should be
        ctl = self.controller()
        self.assertTrue(ctl.startup()["ok"])
        self.assertEqual(ctl.mode, pc.MODE_AUTOMATIC)

    def test_a_byte_order_mark_from_an_editor_is_tolerated(self):
        with open(self.file, "wb") as h:
            h.write(b"\xef\xbb\xbf" + json.dumps({"local_port_mode": "fixed",
                                                  "configured_local_fixed_port": 27123}).encode())
        settings = pc.PortSettings(self.dir).load()
        self.assertEqual(settings.fixed_port, 27123)

    def test_unknown_extra_keys_are_ignored_not_fatal(self):
        with open(self.file, "w", encoding="utf-8") as h:
            json.dump({"local_port_mode": "fixed", "configured_local_fixed_port": 27124, "future": 1}, h)
        self.assertEqual(pc.PortSettings(self.dir).load().fixed_port, 27124)

    def test_concurrent_writers_never_corrupt_the_file(self):
        store = pc.PortSettings(self.dir)
        errors = []

        def writer(base):
            for i in range(25):
                try:
                    store.save(pc.MODE_FIXED, 27000 + base * 100 + i)
                    loaded = store.load()
                    if loaded.mode != pc.MODE_FIXED or not 27000 <= loaded.fixed_port < 28000:
                        errors.append(("torn", loaded.mode, loaded.fixed_port))
                except Exception as exc:
                    errors.append(repr(exc))
        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(store.load().mode, pc.MODE_FIXED)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [], "no temp file left behind")
        self.assertFalse(os.path.exists(self.file + ".corrupt"))

    def test_the_write_is_atomic_unique_temp_fsync_replace_under_one_lock(self):
        import inspect
        import luma_store
        body = inspect.getsource(luma_store._write_unlocked)
        for needle in ("os.fsync", "os.replace", "get_ident"):
            self.assertIn(needle, body)
        source = inspect.getsource(pc.PortSettings.save)
        self.assertIn("atomic_write_json", source)
        self.assertNotIn("open(", source, "the file is never truncated in place")

    def test_the_failure_to_write_reports_false_and_keeps_the_old_file(self):
        store = pc.PortSettings(self.dir)
        store.save(pc.MODE_FIXED, 27200)
        before = self.file_bytes()
        os.remove(self.file)
        os.mkdir(self.file)
        self.assertFalse(store.save(pc.MODE_FIXED, 27201))
        os.rmdir(self.file)
        with open(self.file, "wb") as h:
            h.write(before)
        self.assertEqual(self.file_bytes(), before)


class StartupTests(Base):
    def test_2_automatic_fallback_is_unchanged(self):
        busy = self.hold(self.default)
        ctl = self.controller()
        outcome = ctl.startup()
        self.assertTrue(outcome["ok"], outcome)
        self.assertNotEqual(ctl.current_port, busy.port)
        self.assertGreater(ctl.current_port, self.default)
        self.assertFalse(ctl.temporary, "automatic mode has no 'configured' port to be temporary against")
        self.assertFalse(os.path.exists(self.file), "drifting in automatic mode persists nothing")
        self.assertEqual(get_json(ctl.current_port)["version"], "test")

    def test_automatic_mode_starts_on_the_default_port_when_it_is_free(self):
        ctl = self.controller()
        self.assertTrue(ctl.startup()["ok"])
        self.assertEqual(ctl.current_port, self.default)

    def test_the_session_override_replaces_the_configuration_and_is_not_saved(self):
        self.write_config("fixed", free_port())
        before = self.file_bytes()
        override = free_port()
        ctl = self.controller()
        self.assertTrue(ctl.startup(override)["ok"])
        self.assertEqual(ctl.current_port, override)
        self.assertEqual(self.file_bytes(), before)

    def test_6_fixed_port_startup_binds_exactly_that_port(self):
        port = free_port()
        self.write_config("fixed", port)
        ctl = self.controller()
        outcome = ctl.startup()
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(ctl.current_port, port)
        self.assertFalse(ctl.temporary)
        self.assertEqual(get_json(port)["version"], "test")

    def test_7_fixed_port_occupied_before_startup(self):
        holder = self.hold()
        self.write_config("fixed", holder.port)
        ctl = self.controller()
        outcome = ctl.startup()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "in_use")
        self.assertEqual(outcome["message"], "Configured TCP port %d is already in use." % holder.port)
        self.assertFalse(ctl.running)
        self.assertIsNone(ctl.url)

    def test_8_no_silent_fallback_in_fixed_mode(self):
        holder = self.hold()
        self.write_config("fixed", holder.port)
        ctl = self.controller()
        ctl.startup()
        self.assertIsNone(ctl.current_port)
        for neighbour in range(holder.port + 1, holder.port + 4):
            try:
                socket.create_connection((HOST, neighbour), timeout=0.3).close()
                self.fail("something is serving on %d: the start drifted" % neighbour)
            except OSError:
                pass

    def test_21_the_url_always_matches_the_bound_listener(self):
        ctl = self.controller()
        ctl.startup()
        self.assertEqual(ctl.url, "http://%s:%d/" % (HOST, ctl._server.server_port))
        self.assertEqual(get_json(ctl._server.server_port)["version"], "test")

    def test_23_race_a_port_that_passes_the_preflight_but_is_taken_before_the_bind(self):
        """The preflight is advisory: here it says free while the port is in fact occupied."""
        holder = self.hold()
        self.write_config("fixed", holder.port)
        ctl = self.controller(probe=lambda host, port: (True, 0))
        outcome = ctl.startup()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "in_use", "the real bind refused it and the result says so")
        self.assertFalse(ctl.running)
        holder.release()
        self.assertTrue(ctl.retry_fixed()["ok"], "and recovery works afterwards")

    def test_a_busy_looking_preflight_does_not_stop_a_free_port_from_binding(self):
        port = free_port()
        self.write_config("fixed", port)
        ctl = self.controller(probe=lambda host, p: (False, 10048))
        self.assertTrue(ctl.startup()["ok"], "the bind, not the preflight, decides")

    def test_a_non_busy_bind_failure_is_not_reported_as_in_use(self):
        def refuse(host, port, app):
            raise OSError(99, "cannot assign requested address")
        write = self.write_config("fixed", free_port())
        ctl = self.controller(factory=refuse)
        outcome = ctl.startup()
        self.assertEqual(outcome["reason"], "bind_failed")
        self.assertIn("could not be opened", outcome["message"])


class RecoveryTests(Base):
    def conflict(self):
        holder = self.hold()
        self.write_config("fixed", holder.port)
        ctl = self.controller()
        self.assertFalse(ctl.startup()["ok"])
        return ctl, holder

    def test_10_retry_fails_while_occupied_and_works_after_release(self):
        ctl, holder = self.conflict()
        before = self.file_bytes()
        for _ in range(3):                                   # repeated cycles keep working
            self.assertEqual(ctl.retry_fixed()["reason"], "in_use")
        holder.release()
        outcome = ctl.retry_fixed()
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(ctl.current_port, holder.port)
        self.assertFalse(ctl.temporary)
        self.assertEqual(self.file_bytes(), before, "retry changes no configuration")

    def test_11_12_alternate_starts_the_complete_application_on_a_free_port(self):
        ctl, holder = self.conflict()
        before = self.file_bytes()
        outcome = ctl.start_alternate()
        self.assertTrue(outcome["ok"], outcome)
        self.assertNotEqual(ctl.current_port, holder.port)
        self.assertTrue(ctl.temporary)
        self.assertEqual(get_json(ctl.current_port)["version"], "test", "a real endpoint answers")
        self.assertEqual(outcome["url"], "http://%s:%d/" % (HOST, ctl.current_port))

    def test_14_the_temporary_alternate_does_not_overwrite_the_fixed_configuration(self):
        ctl, holder = self.conflict()
        before = self.file_bytes()
        ctl.start_alternate()
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(pc.PortSettings(self.dir).load().fixed_port, holder.port)
        self.assertEqual(ctl.snapshot()["configured_port"], holder.port)
        self.assertEqual(ctl.snapshot()["current_port"], ctl.current_port)
        self.assertTrue(ctl.snapshot()["temporary"])

    def test_15_the_next_startup_retries_the_configured_fixed_port(self):
        ctl, holder = self.conflict()
        ctl.start_alternate()
        ctl.stop()
        again = self.controller()                              # a "restart"
        self.assertEqual(again.startup()["reason"], "in_use", "the configured port is tried again")
        holder.release()
        third = self.controller()
        self.assertTrue(third.startup()["ok"])
        self.assertEqual(third.current_port, holder.port)

    def test_retry_from_a_temporary_port_moves_to_the_fixed_port_and_releases_the_temporary_one(self):
        ctl, holder = self.conflict()
        ctl.start_alternate()
        temp = ctl.current_port
        holder.release()
        outcome = ctl.retry_fixed()
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(ctl.current_port, holder.port)
        ctl.join_background()
        with self.assertRaises(OSError):
            socket.create_connection((HOST, temp), timeout=0.5).close()

    def test_a_failed_retry_from_a_temporary_port_keeps_the_working_server(self):
        ctl, holder = self.conflict()
        ctl.start_alternate()
        temp = ctl.current_port
        self.assertEqual(ctl.retry_fixed()["reason"], "in_use")
        self.assertEqual(ctl.current_port, temp)
        self.assertEqual(get_json(temp)["version"], "test")

    def test_19_make_the_temporary_port_fixed(self):
        ctl, holder = self.conflict()
        ctl.start_alternate()
        temp = ctl.current_port
        outcome = ctl.make_current_fixed()
        self.assertTrue(outcome["ok"], outcome)
        settings = pc.PortSettings(self.dir).load()
        self.assertEqual((settings.mode, settings.fixed_port), (pc.MODE_FIXED, temp))
        self.assertFalse(ctl.temporary)
        ctl.stop()
        restarted = self.controller()
        self.assertTrue(restarted.startup()["ok"])
        self.assertEqual(restarted.current_port, temp)

    def test_make_fixed_needs_a_running_server(self):
        ctl, _holder = self.conflict()
        before = self.file_bytes()
        self.assertEqual(ctl.make_current_fixed()["reason"], "not_running")
        self.assertEqual(self.file_bytes(), before)

    def test_16_change_fixed_port_from_a_conflict_completes_without_a_browser(self):
        ctl, holder = self.conflict()
        new = free_port()
        outcome = ctl.change_fixed(str(new))
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(ctl.current_port, new)
        self.assertFalse(ctl.temporary)
        settings = pc.PortSettings(self.dir).load()
        self.assertEqual((settings.mode, settings.fixed_port), (pc.MODE_FIXED, new))
        self.assertEqual(get_json(new)["version"], "test")

    def test_17_a_failed_change_preserves_the_old_configuration_byte_for_byte(self):
        ctl, holder = self.conflict()
        before = self.file_bytes()
        other = self.hold()
        outcome = ctl.change_fixed(other.port)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "in_use")
        self.assertEqual(outcome["message"], "TCP port %d is unavailable." % other.port)
        self.assertEqual(self.file_bytes(), before)
        for bad in ("", "abc", "80", "99999"):
            self.assertEqual(ctl.change_fixed(bad)["reason"], "invalid_port")
            self.assertEqual(self.file_bytes(), before)
        self.assertTrue(ctl.change_fixed(free_port())["ok"], "another attempt is allowed")

    def test_18_a_change_is_persisted_only_after_the_bind_succeeded(self):
        ctl, holder = self.conflict()
        new = free_port()
        seen = {}
        real_factory = ctl.factory

        def watching(host, port, app):
            seen["file_at_bind"] = self.file_bytes()
            server = real_factory(host, port, app)
            seen["bound"] = True
            return server
        ctl.factory = watching
        self.assertTrue(ctl.change_fixed(new)["ok"])
        self.assertEqual(json.loads(seen["file_at_bind"])["configured_local_fixed_port"], holder.port,
                         "at the moment of the bind the old value was still the saved one")
        self.assertEqual(pc.PortSettings(self.dir).load().fixed_port, new)

    def test_a_failed_bind_never_even_calls_save(self):
        ctl, holder = self.conflict()
        calls = []
        real = ctl.settings.save
        ctl.settings.save = lambda *a, **k: calls.append(a) or real(*a, **k)
        ctl.change_fixed(self.hold().port)
        self.assertEqual(calls, [])

    def test_a_save_failure_after_the_bind_leaves_the_old_server_and_value(self):
        ctl, holder = self.conflict()
        ctl.start_alternate()
        temp = ctl.current_port
        before = self.file_bytes()
        ctl.settings.save = lambda *a, **k: False
        outcome = ctl.change_fixed(free_port())
        self.assertEqual(outcome["reason"], "persist_failed")
        self.assertEqual(ctl.current_port, temp, "nothing moved")
        self.assertEqual(self.file_bytes(), before)

    def test_changing_to_the_port_already_running_just_saves_it(self):
        ctl, holder = self.conflict()
        ctl.start_alternate()
        temp = ctl.current_port
        self.assertTrue(ctl.change_fixed(temp)["ok"])
        self.assertEqual(ctl.current_port, temp)
        self.assertEqual(pc.PortSettings(self.dir).load().fixed_port, temp)

    def test_operations_are_single_flight(self):
        ctl, _holder = self.conflict()
        ctl._op.acquire()
        try:
            self.assertEqual(ctl.retry_fixed()["reason"], "busy")
            self.assertEqual(ctl.change_fixed(free_port())["reason"], "busy")
        finally:
            ctl._op.release()

    def test_an_operation_never_raises(self):
        ctl, _holder = self.conflict()
        ctl.factory = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        for outcome in (ctl.retry_fixed(), ctl.start_alternate(), ctl.change_fixed(27999)):
            self.assertFalse(outcome["ok"])


class SettingsActionTests(Base):
    def test_20_disabling_fixed_mode_restores_automatic_behaviour(self):
        port = free_port()
        self.write_config("fixed", port)
        ctl = self.controller()
        ctl.startup()
        self.assertTrue(ctl.disable_fixed()["ok"])
        self.assertEqual(pc.PortSettings(self.dir).load().mode, pc.MODE_AUTOMATIC)
        ctl.stop()
        busy = self.hold(self.default)                        # automatic: the default is busy, so it drifts
        again = self.controller()
        outcome = again.startup()
        self.assertTrue(outcome["ok"], outcome)
        self.assertGreater(again.current_port, busy.port)

    def test_3_enabling_fixed_mode_with_the_current_actual_port_restarts_on_it(self):
        ctl = self.controller(restart_delay=0)
        ctl.startup()
        actual = ctl.current_port
        spawned = []
        ctl.spawn = lambda cmd: spawned.append(cmd)
        ctl.command = ["launcher", "--x"]
        outcome = ctl.enable_fixed_and_restart(actual)
        self.assertTrue(outcome["ok"], outcome)
        self.assertTrue(outcome["restarting"])
        ctl.join_background()
        self.assertEqual(pc.PortSettings(self.dir).load().fixed_port, actual)
        self.assertEqual(spawned, [["launcher", "--x"]])

    def test_4_enabling_fixed_mode_with_another_valid_port(self):
        ctl = self.controller(restart_delay=0)
        ctl.startup()
        other = free_port()
        ctl.spawn = lambda cmd: None
        ctl.command = ["launcher"]
        outcome = ctl.enable_fixed_and_restart(str(other))
        self.assertTrue(outcome["ok"])
        ctl.join_background()
        self.assertEqual(pc.PortSettings(self.dir).load().fixed_port, other)
        self.assertEqual(outcome["url"], "http://%s:%d/" % (HOST, other))

    def test_enabling_with_a_bad_or_busy_port_changes_nothing(self):
        ctl = self.controller(restart_delay=0)
        ctl.startup()
        ctl.spawn = lambda cmd: self.fail("must not restart")
        for bad in ("", "x", "10", "70000"):
            self.assertEqual(ctl.enable_fixed_and_restart(bad)["reason"], "invalid_port")
        self.assertEqual(ctl.enable_fixed_and_restart(self.hold().port)["reason"], "in_use")
        self.assertFalse(os.path.exists(self.file))

    def test_the_restart_releases_the_port_before_the_new_instance_is_started(self):
        ctl = self.controller(restart_delay=0)
        ctl.startup()
        actual = ctl.current_port
        order = []

        def spawn(command):
            # the old instance's port must already be free: the new one binds exactly this port
            with socket.socket() as probe:
                probe.bind((HOST, actual))
            order.append("spawned-with-port-free")
        ctl.spawn = spawn
        ctl.command = ["launcher"]
        ctl.exit_callback = lambda: order.append("exit")
        self.assertTrue(ctl.enable_fixed_and_restart(actual)["ok"])
        ctl.join_background()
        self.assertEqual(order, ["spawned-with-port-free", "exit"])
        self.assertFalse(ctl.running)

    def test_a_failed_relaunch_restores_the_server_on_the_same_port(self):
        ctl = self.controller(restart_delay=0)
        ctl.startup()
        actual = ctl.current_port
        ctl.spawn = lambda cmd: (_ for _ in ()).throw(OSError("cannot start"))
        ctl.command = ["launcher"]
        ctl.exit_callback = lambda: self.fail("must not exit when the relaunch failed")
        ctl.enable_fixed_and_restart(actual)
        ctl.join_background()
        self.assertTrue(ctl.running)
        self.assertEqual(get_json(actual)["version"], "test")

    def test_the_relaunch_command_drops_a_port_override_and_handles_frozen_and_source_runs(self):
        argv = ["C:/app/app_launcher.py", "--port=9000", "--host=127.0.0.1"]
        self.assertEqual(pc.build_relaunch_command(argv, frozen=False, executable="py", script="S"),
                         ["py", "S", "--host=127.0.0.1"])
        self.assertEqual(pc.build_relaunch_command(["C:/LumaSuite.exe", "--port=1"], frozen=True,
                                                   executable="C:/LumaSuite.exe"), ["C:/LumaSuite.exe"])


class ApiAndRouteTests(Base):
    """The Settings endpoints: local only, validated, and honest when there is no launcher."""

    def setUp(self):
        super().setUp()
        self.previous = pc.registered()
        self.addCleanup(pc.register, self.previous)
        self.client = lumaserver.APP.test_client()

    def post(self, action, body=None, **kw):
        return self.client.post("/api/local_port/" + action, json=body or {}, **kw)

    def test_both_routes_are_classified_local_only(self):
        for rule in ("/api/local_port", "/api/local_port/<action>"):
            self.assertIs(luma_access.ROUTES[rule], luma_access.Access.LOCAL_ONLY, rule)

    def test_without_a_launcher_the_answer_says_it_is_not_available_in_this_run(self):
        pc.register(None)
        state = self.client.get("/api/local_port")
        self.assertEqual(state.status_code, 200)
        self.assertFalse(state.get_json()["available"])
        self.assertIn("not available in this run", state.get_json()["message"])
        for action in ("retry", "make_fixed", "change", "enable", "disable"):
            r = self.post(action, {"port": 27000})
            self.assertEqual(r.status_code, 409, action)
            self.assertEqual(r.get_json()["reason"], "not_available", action)

    def test_with_a_controller_the_state_is_real_data(self):
        ctl = self.controller()
        ctl.startup()
        pc.register(ctl)
        body = self.client.get("/api/local_port").get_json()
        self.assertTrue(body["available"])
        self.assertEqual(body["current_port"], ctl.current_port)
        self.assertEqual(body["url"], "http://127.0.0.1:%d/" % ctl.current_port)
        self.assertFalse(body["fixed"])

    def test_validation_is_a_400_with_a_sentence(self):
        ctl = self.controller()
        ctl.startup()
        pc.register(ctl)
        for bad in ("", "abc", 80, 99999, None):
            r = self.post("change", {"port": bad})
            self.assertEqual(r.status_code, 400, bad)
            self.assertTrue(r.get_json()["message"].endswith("."))
        self.assertEqual(self.post("enable", {"port": "zz"}).status_code, 400)
        self.assertFalse(os.path.exists(self.file))

    def test_a_busy_port_is_a_409_and_an_unknown_action_a_404(self):
        ctl = self.controller()
        ctl.startup()
        pc.register(ctl)
        self.assertEqual(self.post("change", {"port": self.hold().port}).status_code, 409)
        self.assertEqual(self.post("nonsense").status_code, 404)

    def test_change_through_the_endpoint_moves_the_server_and_returns_the_url(self):
        ctl = self.controller()
        ctl.startup()
        pc.register(ctl)
        new = free_port()
        r = self.post("change", {"port": new})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["url"], "http://127.0.0.1:%d/" % new)
        self.assertEqual(get_json(new)["version"], "test")

    def test_a_cross_origin_browser_write_is_refused_by_the_existing_origin_check(self):
        ctl = self.controller()
        ctl.startup()
        pc.register(ctl)
        r = self.post("change", {"port": free_port()}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(os.path.exists(self.file))

    def test_a_request_on_a_network_listener_never_reaches_the_endpoint(self):
        """A real listener bound to every interface is a remote listener, whatever the peer."""
        ctl = self.controller()
        ctl.startup()
        pc.register(ctl)
        server = luma_remote.serve_tagged("0.0.0.0", 0, lumaserver.APP, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(5)))
        port = server.server_port
        for method, path in (("GET", "/api/local_port"), ("POST", "/api/local_port/change"),
                             ("POST", "/api/local_port/enable"), ("POST", "/api/local_port/make_fixed")):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method,
                                         data=b'{"port": 27001}' if method == "POST" else None,
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError, msg=path) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 404, path)
        self.assertFalse(os.path.exists(self.file), "nothing was changed by a remote request")

    def test_the_handler_checks_locality_itself_when_the_gate_is_taken_out_of_the_way(self):
        """A second line of defence is tested by disabling the first (the gate runs in before_request)."""
        ctl = self.controller()
        ctl.startup()
        pc.register(ctl)
        remote = {luma_remote.LISTENER_KEY: luma_remote.REMOTE}
        with lumaserver.APP.test_request_context("/api/local_port", environ_base=remote):
            response, status = lumaserver.api_local_port_state()
        self.assertEqual(status, 404)
        with lumaserver.APP.test_request_context("/api/local_port/disable", method="POST", json={},
                                                 environ_base=remote):
            response, status = lumaserver.api_local_port_action("disable")
        self.assertEqual(status, 404)
        self.assertFalse(os.path.exists(self.file))

    def test_the_backup_does_not_carry_the_local_port_file(self):
        import luma_backup
        self.assertIn(pc.FILE_NAME, luma_backup.EXCLUDED)
        self.assertNotIn(pc.FILE_NAME, {s.filename for s in luma_backup.SECTIONS})
        pc.PortSettings(self.dir).save(pc.MODE_FIXED, 27333)
        payload = luma_backup.build_payload(self.dir, [], "t")
        self.assertNotIn("27333", json.dumps(payload))


class RemoteAccessUntouchedTests(unittest.TestCase):
    """Test 22: the new setting neither reads nor changes the Remote Access listener."""

    ROOT = _support.ROOT

    def source(self, name):
        with open(os.path.join(self.ROOT, name), encoding="utf-8") as h:
            return h.read()

    def test_the_port_module_does_not_touch_the_remote_listener_or_its_settings(self):
        text = self.source("luma_portcontrol.py")
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        for forbidden in ("RemoteListener", "luma_wiring", "luma_tls", "remote_access", "adapter", "RemoteAccess"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_remote_access_code_does_not_know_about_the_local_port_setting(self):
        for name in ("luma_remote.py", "luma_wiring.py", "luma_tls.py", "luma_remote_api.py", "luma_adapters.py"):
            text = self.source(name)
            for needle in ("luma_portcontrol", "local_port", "local_port_mode"):
                self.assertNotIn(needle, text, "%s mentions %s" % (name, needle))

    def test_remote_access_still_restores_on_its_own_saved_port_not_the_local_one(self):
        """Representative remote start: its port comes from its own file and default, untouched by a fixed local port."""
        import luma_wiring
        data = tempfile.mkdtemp(prefix="luma-remote-")
        try:
            self.assertIn("default_port", self.source("luma_wiring.py"))
            pc.PortSettings(data).save(pc.MODE_FIXED, 27444)
            self.assertFalse(os.path.exists(os.path.join(data, "remote_access.json")),
                             "setting the local port writes nothing of Remote Access's")
        finally:
            import shutil
            shutil.rmtree(data, ignore_errors=True)

    def test_the_local_listener_is_still_created_through_the_tagging_factory(self):
        self.assertIn("luma_remote.serve_tagged(host, port, app, threaded=True)",
                      self.source("luma_portcontrol.py"))


class ObserverTests(Base):
    """The launcher window follows the controller through these notifications (D1)."""

    def watched(self, ctl):
        seen = []
        ctl.add_observer(lambda view: seen.append(view))
        return seen

    def test_every_transition_notifies_with_the_state_it_produced(self):
        holder = self.hold()
        self.write_config("fixed", holder.port)
        ctl = self.controller(retire_delay=0)
        seen = self.watched(ctl)
        self.assertFalse(ctl.startup()["ok"])
        self.assertTrue(seen and seen[-1]["running"] is False)
        ctl.start_alternate()
        alt = ctl.current_port
        self.assertEqual((seen[-1]["port"], seen[-1]["temporary"], seen[-1]["configured_port"]),
                         (alt, True, holder.port))
        holder.release()
        ctl.retry_fixed()
        self.assertEqual((seen[-1]["port"], seen[-1]["temporary"], seen[-1]["url"]),
                         (holder.port, False, pc.url_for(holder.port)))
        n = len(seen)
        free = free_port()
        ctl.change_fixed(free)
        self.assertGreater(len(seen), n)
        self.assertEqual((seen[-1]["port"], seen[-1]["configured_port"]), (free, free))
        ctl.disable_fixed()
        self.assertEqual(seen[-1]["mode"], pc.MODE_AUTOMATIC)

    def test_make_current_fixed_notifies_with_the_new_configured_port(self):
        holder = self.hold()
        self.write_config("fixed", holder.port)
        ctl = self.controller(retire_delay=0)
        seen = self.watched(ctl)
        ctl.startup()
        ctl.start_alternate()
        self.assertTrue(seen[-1]["temporary"])
        ctl.make_current_fixed()
        self.assertEqual((seen[-1]["configured_port"], seen[-1]["temporary"]), (ctl.current_port, False))

    def test_the_settings_endpoint_path_notifies(self):
        holder = self.hold()
        self.write_config("fixed", holder.port)
        ctl = self.controller(retire_delay=0)
        ctl.startup()
        ctl.start_alternate()
        seen = self.watched(ctl)
        holder.release()
        payload, status = pc.api("retry", {}, controller=ctl)
        self.assertEqual(status, 200)
        self.assertEqual(seen[-1]["port"], holder.port)

    def test_a_failed_operation_still_reports_the_truth(self):
        holder = self.hold()
        self.write_config("fixed", holder.port)
        ctl = self.controller()
        seen = self.watched(ctl)
        ctl.startup()
        n = len(seen)
        self.assertFalse(ctl.retry_fixed()["ok"])
        self.assertGreater(len(seen), n)
        self.assertFalse(seen[-1]["running"])

    def test_restart_failure_restore_notifies(self):
        ctl = self.controller(restart_delay=0)
        ctl.startup()
        seen = self.watched(ctl)

        def boom(cmd):
            raise OSError("no")
        ctl.spawn, ctl.command = boom, ["x"]
        self.assertFalse(ctl.restart_now())
        self.assertTrue(any(v["running"] is False for v in seen), "stop is announced")
        self.assertTrue(seen[-1]["running"] and seen[-1]["port"] == ctl.current_port)

    def test_a_raising_observer_never_breaks_the_controller_or_the_others(self):
        ctl = self.controller()
        good = self.watched(ctl)

        def bad(view):
            raise RuntimeError("observer bug")
        ctl.add_observer(bad)
        self.assertTrue(ctl.startup()["ok"])
        self.assertTrue(good)
        self.assertEqual(ctl.retry_fixed()["reason"], "not_fixed")

    def test_unregister_stops_notifications_and_is_idempotent(self):
        ctl = self.controller()
        seen = []
        cb = seen.append
        ctl.add_observer(cb)
        ctl.add_observer(cb)
        ctl.startup()
        self.assertEqual(len(seen), 1)
        ctl.remove_observer(cb)
        ctl.remove_observer(cb)
        ctl.retry_fixed()
        self.assertEqual(len(seen), 1)

    def test_notifications_from_many_threads_are_all_delivered(self):
        ctl = self.controller()
        ctl.startup()
        seen, lock = [], threading.Lock()

        def cb(view):
            with lock:
                seen.append(view)
        ctl.add_observer(cb)
        threads = [threading.Thread(target=ctl.disable_fixed) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        # a call refused as busy changed nothing and announces nothing; every operation that ran did
        self.assertTrue(1 <= len(seen) <= 12)
        self.assertTrue(all(v["running"] and v["port"] for v in seen))

    def test_an_observer_may_read_the_controller_without_deadlock(self):
        ctl = self.controller()
        out = []
        ctl.add_observer(lambda v: out.append(ctl.view()))
        ctl.startup()
        self.assertTrue(out and out[0]["running"])


if __name__ == "__main__":
    unittest.main()
