"""The local application listener's port: automatic by default, fixed on request.

Scope. This module is about ONE listener: the local application listener the
launcher starts on 127.0.0.1. The Remote Access listener (bound to a network
address the administrator chose) is a different listener with its
own port, transport and certificate, and nothing here reads or changes any of it.

Modes (persisted in `local_port.json`, additive and machine-specific):

  automatic   the default and exactly what the product always did: start at the
              normal port (8090) and take the next free one when it is busy.
  fixed       bind exactly the configured port and nothing else. A busy port is
              never drifted away from silently; the launcher shows a recovery
              state instead (Retry / Start on Alternate Port / Change Fixed Port).

A temporary alternate port is session-only. It is persisted only when the
operator explicitly makes it the fixed port, or when Change Fixed Port succeeds.

Rules this module keeps:
  * No state of the file can stop the launcher. A missing, empty, corrupt,
    wrong-typed, out-of-range or unreadable file is quarantined and read as
    "automatic".
  * Every write is atomic and serialised (`luma_store`): unique temp file, fsync,
    os.replace, under one lock.
  * The bind is authoritative. `probe_bind` is advisory: a port that passes it can
    still be taken before the real bind, and that case recovers like any other.
  * A new port is persisted only after the server is up on it.
  * Operations never raise; each returns a structured result and is single-flight.
  * Nothing here is imported by discovery or the scan path.
"""
from __future__ import annotations

import errno as _errno
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import luma_store

log = logging.getLogger(__name__)

DEFAULT_PORT = 8090
MIN_PORT = 1024
MAX_PORT = 65535
DISCOVERY_SPAN = 100                 # how far automatic discovery looks, as it always has
FILE_NAME = "local_port.json"

MODE_AUTOMATIC = "automatic"
MODE_FIXED = "fixed"
MODES = (MODE_AUTOMATIC, MODE_FIXED)

#: errnos that mean "something else holds that port". 10013 is how Windows reports a bind
#: refused by another listener's exclusive hold (and by a reserved range), so it reads the same.
_IN_USE = {98, 48, 10048, 10013, getattr(_errno, "EADDRINUSE", 98), getattr(_errno, "EACCES", 13)}


class PortError(ValueError):
    """An unusable port, with a sentence an operator can read."""


def validate_port(value) -> int:
    """The port as an int, or PortError.

    Accepts an int, or text of digits (what a field gives). Rejects a bool (it is an int),
    blank, anything non-numeric, and anything outside 1024-65535. There are no product
    reservations: the Remote Access listener binds a network address, not this one, so no
    port is unusable *because of* the product; whether a port is free is the bind's answer.
    """
    if isinstance(value, bool):
        raise PortError("Enter a TCP port number between %d and %d." % (MIN_PORT, MAX_PORT))
    if isinstance(value, int):
        number = value
    else:
        text = str(value if value is not None else "").strip()
        if not text:
            raise PortError("Enter a TCP port number.")
        if not text.isascii() or not text.isdigit() or len(text) > 6:
            raise PortError("A TCP port is a whole number between %d and %d." % (MIN_PORT, MAX_PORT))
        number = int(text)
    if number < MIN_PORT or number > MAX_PORT:
        raise PortError("Use a TCP port between %d and %d." % (MIN_PORT, MAX_PORT))
    return number


def validate_session_port(value) -> int:
    """The command-line port override: an explicit one-run override. Wider than the setting (any usable TCP port),
    because it has always been, and never persisted."""
    text = str(value if value is not None else "").strip()
    if not text.isascii() or not text.isdigit() or not (1 <= int(text) <= MAX_PORT):
        raise PortError("--port needs a whole number between 1 and %d." % MAX_PORT)
    return int(text)


def url_for(port, host="127.0.0.1") -> str:
    return "http://%s:%d/" % (host, int(port))


# ----------------------------------------------------------------------------- the file
class Settings:
    __slots__ = ("mode", "fixed_port")

    def __init__(self, mode=MODE_AUTOMATIC, fixed_port=None):
        self.mode = mode
        self.fixed_port = fixed_port

    @property
    def fixed(self) -> bool:
        return self.mode == MODE_FIXED and self.fixed_port is not None


class PortSettings:
    """`local_port.json` in the application data folder."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / FILE_NAME

    def load(self) -> Settings:
        """The settings; anything unusable is quarantined and reads as automatic. Never raises."""
        try:
            with luma_store.lock_for(self.path):
                if not self.path.exists():
                    return Settings()
                try:
                    data = json.loads(self.path.read_bytes().decode("utf-8-sig"))
                    return self._parse(data)
                except OSError as exc:
                    # Could not be read; says nothing about the contents. Leave the file alone.
                    log.warning("[PORT] could not read %s (%s); using automatic port selection "
                                "and leaving the file in place", FILE_NAME, type(exc).__name__)
                    return Settings()
                except Exception as exc:
                    log.warning("[PORT] %s is unusable (%s); quarantining it and using automatic "
                                "port selection", FILE_NAME, type(exc).__name__)
                    luma_store.quarantine(self.path)
                    return Settings()
        except Exception as exc:                     # even the lock or the stat failing must not stop a start
            log.warning("[PORT] could not read %s (%s); using automatic port selection",
                        FILE_NAME, type(exc).__name__)
            return Settings()

    @staticmethod
    def _parse(data) -> Settings:
        if not isinstance(data, dict):
            raise ValueError("not an object")
        mode = data.get("local_port_mode", MODE_AUTOMATIC)
        if mode not in MODES:
            raise ValueError("unknown mode")
        port = None
        if data.get("configured_local_fixed_port") is not None:
            raw = data["configured_local_fixed_port"]
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ValueError("port is not an integer")
            port = validate_port(raw)
        if mode == MODE_FIXED and port is None:
            raise ValueError("fixed mode without a port")
        return Settings(mode, port)

    def save(self, mode, fixed_port=None) -> bool:
        """Persist atomically. False when it could not be written (the old file is untouched)."""
        payload = {"local_port_mode": mode}
        if fixed_port is not None:
            payload["configured_local_fixed_port"] = validate_port(fixed_port)
        return bool(luma_store.atomic_write_json(self.path, payload))


# ----------------------------------------------------------------------------- the probe
def probe_bind(host, port):
    """(ok, errno) for a plain bind, the way automatic discovery has always tested a port.

    ADVISORY. It says whether the port looked free a moment ago; the real bind decides.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, int(port)))
        return True, 0
    except OSError as exc:
        return False, getattr(exc, "errno", None) or 0


def _reason_for(exc) -> str:
    if isinstance(exc, SystemExit):
        # werkzeug reports a failed bind by exiting the thread that made the server.
        return "in_use"
    if getattr(exc, "errno", None) in _IN_USE or "address already in use" in str(exc).lower():
        return "in_use"
    return "bind_failed"


def result(ok, reason="", message="", **extra) -> dict:
    out = {"ok": bool(ok), "reason": reason if not ok else "", "message": message}
    out.update(extra)
    return out


def _default_factory(host, port, app):
    import luma_remote
    return luma_remote.serve_tagged(host, port, app, threaded=True)


def build_relaunch_command(argv=None, frozen=None, executable=None, script=None) -> list:
    """The command that starts a fresh instance. A `--port=` override is dropped: the operator
    just changed the setting that override would defeat."""
    argv = list(sys.argv if argv is None else argv)
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable = executable or sys.executable
    rest = [a for a in argv[1:] if not str(a).startswith("--port=")]
    if frozen:
        return [executable] + rest
    return [executable, script or os.path.abspath(argv[0])] + rest


def default_spawn(command, frozen=None):
    """Start the new instance detached from this one. Raises OSError when it cannot."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    env = dict(os.environ)
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True, "env": env}
    if frozen:
        # A one-file build extracts itself; the child must not reuse this instance's extraction.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        env.pop("_MEIPASS2", None)
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


# ----------------------------------------------------------------------------- the controller
class PortController:
    """Owns the running local listener and every transition between ports.

    The launcher and the Settings endpoints are thin layers over this, so the rules are
    testable without a window or a browser.
    """

    def __init__(self, app, host, data_dir, factory=None, probe=None, spawn=None,
                 exit_callback=None, retire_delay=0.0, restart_delay=0.8, ready_timeout=3.0,
                 command=None):
        self.app = app
        self.host = host
        self.settings = PortSettings(data_dir)
        self.factory = factory or _default_factory
        self.probe = probe or probe_bind
        self.spawn = spawn or default_spawn
        self.exit_callback = exit_callback
        self.retire_delay = retire_delay
        self.restart_delay = restart_delay
        self.ready_timeout = ready_timeout
        self.command = command                     # None: build it when needed
        self._op = threading.Lock()                # single-flight for operations
        self._state = threading.Lock()
        self._server = None
        self._thread = None
        self.current_port = None
        self.running = False
        self._threads = []                         # background work a caller can wait on
        self.configured_port = None
        self.mode = MODE_AUTOMATIC
        self.session_override = None
        self._observers = []                       # callables told after every transition
        self._observer_lock = threading.Lock()

    # ---- observers ------------------------------------------------------------------
    def add_observer(self, callback):
        """Register `callback(view)`; it is called (on whichever thread made the change) after every
        transition. It must not block. A raising observer is logged and never breaks the controller."""
        with self._observer_lock:
            if callback not in self._observers:
                self._observers.append(callback)

    def remove_observer(self, callback):
        with self._observer_lock:
            if callback in self._observers:
                self._observers.remove(callback)

    def view(self) -> dict:
        """The one derived description of the listener, from in-memory state (no file read)."""
        with self._state:
            return {"running": self.running, "port": self.current_port, "url": self.url,
                    "host": self.host, "mode": self.mode, "configured_port": self.configured_port,
                    "temporary": self.temporary}

    def _notify(self):
        with self._observer_lock:
            observers = list(self._observers)
        if not observers:
            return
        view = self.view()
        for callback in observers:
            try:
                callback(dict(view))
            except Exception as exc:
                log.warning("[PORT] a state observer failed: %s", type(exc).__name__)

    # ---- reading --------------------------------------------------------------------
    @property
    def temporary(self) -> bool:
        return bool(self.running and self.mode == MODE_FIXED and self.configured_port is not None
                    and self.current_port != self.configured_port)

    @property
    def url(self):
        return url_for(self.current_port, self.host) if self.running and self.current_port else None

    def snapshot(self) -> dict:
        saved = self.settings.load()
        with self._state:
            return {"ok": True, "available": True, "mode": saved.mode, "fixed": saved.fixed,
                    "configured_port": saved.fixed_port, "current_port": self.current_port,
                    "temporary": bool(self.running and saved.fixed and self.current_port != saved.fixed_port),
                    "running": self.running, "url": self.url, "host": self.host,
                    "restart_supported": True, "default_port": DEFAULT_PORT,
                    "session_override": self.session_override}

    def _refresh_mode(self):
        saved = self.settings.load()
        self.mode, self.configured_port = saved.mode, saved.fixed_port
        return saved

    # ---- the one place a server is started -----------------------------------------
    def _start(self, port):
        """Bind `port` and serve on it. (ok, server, thread, reason)."""
        try:
            server = self.factory(self.host, port, self.app)
        except (OSError, SystemExit) as exc:
            return False, None, None, _reason_for(exc)
        except Exception as exc:                   # never raises
            log.warning("[PORT] could not start the server on %d: %s", port, type(exc).__name__)
            return False, None, None, "bind_failed"
        thread = threading.Thread(target=server.serve_forever, name="luma-local-server", daemon=True)
        thread.start()
        if not self._wait_ready(port):
            self._retire(server, thread, 0)
            return False, None, None, "not_serving"
        return True, server, thread, ""

    def _wait_ready(self, port) -> bool:
        deadline = time.time() + self.ready_timeout
        while True:
            try:
                socket.create_connection((self.host, port), timeout=1).close()
                return True
            except OSError:
                if time.time() >= deadline:
                    return False
                time.sleep(0.05)

    def _retire(self, server, thread, delay):
        """Stop a server and release its port. Delayed for one that is answering this very request."""
        def work():
            if delay:
                time.sleep(delay)
            try:
                server.shutdown()
                server.server_close()
            except Exception as exc:
                log.warning("[PORT] stopping a server: %s", type(exc).__name__)
            if thread is not None:
                thread.join(3)
        if delay:
            t = threading.Thread(target=work, name="luma-port-retire", daemon=True)
            self._threads.append(t)
            t.start()
        else:
            work()

    def _adopt(self, server, thread, port, delay=None):
        with self._state:
            old = (self._server, self._thread)
            self._server, self._thread = server, thread
            self.current_port, self.running = port, True
        if old[0] is not None:
            self._retire(old[0], old[1], self.retire_delay if delay is None else delay)

    def stop(self):
        """Stop the listener and release its port. Safe to call twice."""
        with self._state:
            server, thread = self._server, self._thread
            self._server = self._thread = None
            self.running = False
        if server is not None:
            self._retire(server, thread, 0)
        self._notify()

    def join_background(self, timeout=10):
        for t in list(self._threads):
            t.join(timeout)
        self._threads = [t for t in self._threads if t.is_alive()]

    # ---- single-flight --------------------------------------------------------------
    def _guard(self, fn, *args):
        if not self._op.acquire(blocking=False):
            return result(False, "busy", "Another port change is in progress. Try again in a moment.")
        try:
            return fn(*args)
        except Exception as exc:                   # an operation never raises
            log.warning("[PORT] operation failed: %s", type(exc).__name__)
            return result(False, "error", "The port change could not be completed.")
        finally:
            self._op.release()
            self._notify()                         # every transition, after the lock is released

    def _ok(self, message="", **extra):
        return result(True, message=message, port=self.current_port, url=self.url,
                      configured_port=self.configured_port, temporary=self.temporary, **extra)

    def _fail(self, reason, port, message):
        return result(False, reason, message, port=port, configured_port=self.configured_port,
                      temporary=self.temporary, url=self.url)

    # ---- startup --------------------------------------------------------------------
    def startup(self, override_port=None):
        return self._guard(self._startup, override_port)

    def _startup(self, override_port):
        saved = self._refresh_mode()
        self.session_override = override_port
        if override_port is not None:
            # An explicit session override replaces the configured value for this run only and is
            # never persisted: discovery from that port, as it always did.
            self.mode = MODE_AUTOMATIC
            return self._automatic(override_port)
        if saved.fixed:
            return self._exact(saved.fixed_port, "Configured TCP port %d is already in use." % saved.fixed_port)
        return self._automatic(DEFAULT_PORT)

    def _exact(self, port, busy_message):
        advisory, _ = self.probe(self.host, port)
        if not advisory:
            log.info("[PORT] preflight: TCP port %d looks busy (advisory; the bind decides)", port)
        ok, server, thread, reason = self._start(port)
        if not ok:
            log.info("[PORT] could not bind the configured TCP port %d (%s)", port, reason)
            return self._fail(reason, port, busy_message if reason == "in_use"
                              else "TCP port %d could not be opened." % port)
        self._adopt(server, thread, port)
        log.info("[PORT] local server on the configured TCP port %d", port)
        return self._ok("Server running.")

    def _automatic(self, start, retired_delay=None):
        """Today's behaviour: probe forward from `start`, and take the next port when a bind fails."""
        port = start
        while port < start + DISCOVERY_SPAN:
            free, _ = self.probe(self.host, port)
            if free:
                ok, server, thread, reason = self._start(port)
                if ok:
                    self._adopt(server, thread, port, retired_delay)
                    if port != start:
                        log.info("[PORT] TCP port %d busy; local server on %d", start, port)
                    return self._ok("Server running.")
                if reason != "in_use":
                    return self._fail(reason, port, "The local server could not be opened on TCP port %d." % port)
            port += 1
        return self._fail("no_free_port", start, "No free TCP port was found near %d." % start)

    # ---- recovery and change --------------------------------------------------------
    def retry_fixed(self):
        return self._guard(self._retry_fixed)

    def _retry_fixed(self):
        saved = self._refresh_mode()
        if not saved.fixed:
            return self._fail("not_fixed", None, "No fixed port is configured.")
        port = saved.fixed_port
        if self.running and self.current_port == port:
            return self._ok("Already running on the configured port.")
        ok, server, thread, reason = self._start(port)
        if not ok:
            return self._fail(reason, port, "Configured TCP port %d is already in use." % port
                              if reason == "in_use" else "TCP port %d could not be opened." % port)
        self._adopt(server, thread, port)
        log.info("[PORT] retry succeeded on the configured TCP port %d", port)
        return self._ok("Running on the configured port.")

    def start_alternate(self):
        return self._guard(self._start_alternate)

    def _start_alternate(self):
        saved = self._refresh_mode()
        start = saved.fixed_port if saved.fixed else DEFAULT_PORT
        outcome = self._automatic(start)
        if outcome["ok"]:
            # Session only: the configured value is deliberately not touched.
            log.info("[PORT] alternate TCP port %s chosen for this session (configured %s)",
                     self.current_port, saved.fixed_port)
        return outcome

    def change_fixed(self, value):
        return self._guard(self._change_fixed, value)

    def _change_fixed(self, value):
        try:
            port = validate_port(value)
        except PortError as exc:
            return self._fail("invalid_port", None, str(exc))
        with self._state:
            have_old = self.running
            already = self.running and self.current_port == port
        server = thread = None
        if not already:
            ok, server, thread, reason = self._start(port)
            if not ok:
                return self._fail(reason, port, "TCP port %d is unavailable." % port
                                  if reason == "in_use" else "TCP port %d could not be opened." % port)
        # Only now, with the server up on it, is the value written.
        if not self.settings.save(MODE_FIXED, port):
            if server is not None and have_old:
                self._retire(server, thread, 0)    # nothing changed: the old server stays
            elif server is not None:
                self._adopt(server, thread, port)  # no old server: keep the working one, unsaved
            return self._fail("persist_failed", port, "The new port could not be saved. "
                              "The configured port is unchanged.")
        self._refresh_mode()
        if server is not None:
            self._adopt(server, thread, port)
        log.info("[PORT] fixed TCP port changed to %d", port)
        return self._ok("The fixed port is now %d." % port)

    def make_current_fixed(self):
        return self._guard(self._make_current_fixed)

    def _make_current_fixed(self):
        if not self.running or not self.current_port:
            return self._fail("not_running", None, "The local server is not running.")
        port = self.current_port
        if not self.settings.save(MODE_FIXED, port):
            return self._fail("persist_failed", port, "The port could not be saved. The configured port is unchanged.")
        self._refresh_mode()
        log.info("[PORT] TCP port %d made the fixed port", port)
        return self._ok("TCP port %d is now the fixed port." % port)

    def disable_fixed(self):
        return self._guard(self._disable_fixed)

    def _disable_fixed(self):
        saved = self._refresh_mode()
        if not saved.fixed:
            return self._ok("Automatic port selection is already on.")
        if not self.settings.save(MODE_AUTOMATIC, saved.fixed_port):
            return self._fail("persist_failed", None, "The setting could not be saved.")
        self._refresh_mode()
        log.info("[PORT] automatic local port selection restored")
        return self._ok("LumaSuite will select an available port at the next start.")

    def enable_fixed_and_restart(self, value):
        return self._guard(self._enable_fixed_and_restart, value)

    def _enable_fixed_and_restart(self, value):
        try:
            port = validate_port(value)
        except PortError as exc:
            return self._fail("invalid_port", None, str(exc))
        if not (self.running and self.current_port == port):
            free, _ = self.probe(self.host, port)
            if not free:
                # Advisory, but persisting a port that is plainly taken only buys a recovery screen.
                return self._fail("in_use", port, "TCP port %d appears to be in use. Choose another." % port)
        if not self.settings.save(MODE_FIXED, port):
            return self._fail("persist_failed", port, "The port could not be saved.")
        self._refresh_mode()
        log.info("[PORT] fixed TCP port %d saved; restarting", port)
        return self._restart(port)

    # ---- restart --------------------------------------------------------------------
    def _restart(self, port):
        """Schedule the restart after the response has been delivered. The port is released first."""
        t = threading.Thread(target=self._restart_now, name="luma-restart",
                             args=(self.restart_delay,), daemon=True)
        self._threads.append(t)
        t.start()
        return result(True, message="LumaSuite is restarting.", port=port, url=url_for(port, self.host),
                      restarting=True, configured_port=port, temporary=False)

    def _restart_now(self, delay=0.0):
        if delay:
            time.sleep(delay)
        previous = self.current_port
        with self._op:                             # nothing else changes ports mid-restart
            self.stop()                            # 1. release the port the old instance holds
            try:
                command = self.command or build_relaunch_command()
                self.spawn(command)                # 2. only then start the new instance
            except Exception as exc:
                log.warning("[PORT] relaunch failed: %s", type(exc).__name__)
                if previous:                       # never leave the operator without a server
                    ok, server, thread, _reason = self._start(previous)
                    if ok:
                        self._adopt(server, thread, previous)
                self._notify()
                return False
            if self.exit_callback:
                self.exit_callback()               # 3. and this instance leaves
        return True

    def restart_now(self):
        """Synchronous form of the restart, for tests and for a caller that has already waited."""
        return self._restart_now(0.0)


# ----------------------------------------------------------------------------- registry
_registered = None


def register(controller):
    global _registered
    _registered = controller


def registered():
    return _registered


STATUS_FOR = {"invalid_port": 400, "in_use": 409, "bind_failed": 409, "not_serving": 409, "busy": 409,
              "not_fixed": 409, "not_running": 409, "no_free_port": 409, "persist_failed": 500,
              "error": 500, "not_available": 409}

NOT_AVAILABLE = ("The local application port is managed by the LumaSuite launcher and is not "
                 "available in this run.")


def api(action, body=None, controller=None):
    """(payload, http status) for one Settings request. Never raises."""
    ctl = controller or _registered
    if ctl is None:
        if action == "state":
            return {"ok": True, "available": False, "message": NOT_AVAILABLE}, 200
        return result(False, "not_available", NOT_AVAILABLE), 409
    body = body if isinstance(body, dict) else {}
    if action == "state":
        return ctl.snapshot(), 200
    table = {"retry": lambda: ctl.retry_fixed(),
             "make_fixed": lambda: ctl.make_current_fixed(),
             "change": lambda: ctl.change_fixed(body.get("port")),
             "enable": lambda: ctl.enable_fixed_and_restart(body.get("port")),
             "disable": lambda: ctl.disable_fixed()}
    run = table.get(action)
    if run is None:
        return result(False, "unknown_action", "Unknown action."), 404
    outcome = run()
    return outcome, 200 if outcome.get("ok") else STATUS_FOR.get(outcome.get("reason"), 409)
