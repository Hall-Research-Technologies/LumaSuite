"""Shared fixtures for the remote-access tests.

Importing this module installs the hardware fence and points the application at a
scratch data directory *before* anything imports `lumaserver`, so a test can never
reach a device or touch the operator's real files, whichever way it is run
(`python tests/<file>.py`, `python -m unittest`, discovery, an IDE).

The fixtures use synthetic addresses and hardware identifiers that share nothing
with any real equipment: documentation ranges (RFC 5737) and locally administered
MACs. Tests must deep-copy anything they hand to server code, because background
work mutates cached device dicts in place.
"""
import copy
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (HERE, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import _fence  # noqa: E402

_fence.install()
_fence.isolate_data_dir()

from flask import Flask  # noqa: E402

import luma_projection  # noqa: E402
import luma_users  # noqa: E402
import luma_walls  # noqa: E402
import luma_wiring  # noqa: E402

#: Synthetic equipment. Locally administered MACs (02:...), documentation addresses.
SOURCE_A = {"mac": "02:00:5e:10:00:0a", "ip": "192.0.2.10", "type": "encoder",
            "hostname": "Alpha Camera", "model": "TEST-E1", "status": "idle",
            "enc": {"video": {"streamname": "stream-alpha"}}}
SOURCE_B = {"mac": "02:00:5e:10:00:0b", "ip": "192.0.2.11", "type": "encoder",
            "hostname": "Bravo Camera", "model": "TEST-E1", "status": "idle",
            "enc": {"video": {"streamname": "stream-bravo"}}}
SOURCE_C = {"mac": "02:00:5e:10:00:0c", "ip": "192.0.2.12", "type": "encoder",
            "hostname": "Charlie Camera", "model": "TEST-E1", "status": "idle",
            "enc": {"video": {"streamname": "stream-charlie"}}}
DISPLAY_1 = {"mac": "02:00:5e:20:00:01", "ip": "198.51.100.21", "type": "decoder",
             "hostname": "Lobby One", "model": "TEST-D1", "status": "idle",
             "dec": {"video": {"streamname": ""}}}
DISPLAY_2 = {"mac": "02:00:5e:20:00:02", "ip": "198.51.100.22", "type": "decoder",
             "hostname": "Lobby Two", "model": "TEST-D1", "status": "idle",
             "dec": {"video": {"streamname": ""}}}
DISPLAY_3 = {"mac": "02:00:5e:20:00:03", "ip": "198.51.100.23", "type": "decoder",
             "hostname": "Lobby Three", "model": "TEST-D1", "status": "idle",
             "dec": {"video": {"streamname": ""}}}
DISPLAY_4 = {"mac": "02:00:5e:20:00:04", "ip": "198.51.100.24", "type": "decoder",
             "hostname": "Lobby Four", "model": "TEST-D1", "status": "idle",
             "dec": {"video": {"streamname": ""}}}

ALL_UNITS = (SOURCE_A, SOURCE_B, SOURCE_C, DISPLAY_1, DISPLAY_2, DISPLAY_3, DISPLAY_4)

ADMIN_PASSWORD = "correct horse battery"
USER_PASSWORD = "another long passphrase"


class FakeRouter:
    """Stands in for the verified A/V route. Records calls; never touches a device."""

    def __init__(self):
        self.calls = []                 # (decoder_ip, encoder_ip) in call order
        self.fail = {}                  # decoder_ip -> status to return instead of success
        self.active = 0
        self.max_active = 0
        self.on_call = None             # optional hook, called inside the route

    def __call__(self, decoder_ip, encoder_ip):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((decoder_ip, encoder_ip))
            if self.on_call:
                self.on_call(decoder_ip, encoder_ip)
            status = self.fail.get(decoder_ip)
            if status:
                return {"ok": False, "status": status, "reason": status, "audio": "unknown"}
            return {"ok": True, "status": "verified", "audio": "follows"}
        finally:
            self.active -= 1


class FakeModeRouter:
    """Stands in for the video-only / audio-only engine. Records calls; touches no device."""

    def __init__(self):
        self.calls = []                 # (decoder_ip, mode, encoder_ip)
        self.fail = {}                  # decoder_ip -> status to return instead of success

    def __call__(self, decoder_ip, mode, encoder_ip):
        self.calls.append((decoder_ip, mode, encoder_ip))
        status = self.fail.get(decoder_ip)
        if status:
            return {"ok": False, "status": status, "reason": status}
        return {"ok": True, "status": "verified"}


def wall_device_id(unit):
    """The application's own device id for a unit, as the saved walls spell it."""
    return "mac:" + str(unit.get("mac") or "").strip().lower()


def saved_wall(wall_id, name, members, rows=1, columns=None, source_id=""):
    """A saved wall as video_walls.json holds it: one cell per member decoder, in a row."""
    columns = columns or max(1, len(members))
    return {"id": wall_id, "name": name, "rows": rows, "columns": columns, "source_id": source_id,
            "cells": [{"row": 1 + index // columns, "column": 1 + index % columns,
                       "decoder_id": wall_device_id(unit), "source_mode": "wall"}
                      for index, unit in enumerate(members)],
            "regions": []}


class FakeWallRouter:
    """Stands in for routing one source to a saved wall. Records calls; touches no device."""

    def __init__(self):
        self.calls = []                 # (wall id, source identity)
        self.planes = []                # the plane each call was asked for ("av", "video", "audio")
        self.result = None              # optional fixed outcome
        self.on_call = None

    def __call__(self, wall, source_identity, plane="av"):
        self.calls.append((wall.get("id"), source_identity))
        self.planes.append(plane)
        if self.on_call:
            self.on_call(wall, source_identity)
        total = len([c for c in wall.get("cells") or [] if c.get("decoder_id")])
        if self.result is not None:
            return dict(self.result)
        return {"ok": True, "status": "verified", "routed": total, "total": total}


class System:
    """A fresh RemoteSystem on its own Flask app, with stubbed device callables."""

    def __init__(self, units=None, walls=None):
        self.dir = tempfile.mkdtemp(prefix="lumasuite-system-")
        self.units = copy.deepcopy(list(units if units is not None else ALL_UNITS))
        self.router = FakeRouter()
        self.wall_defs = copy.deepcopy(list(walls or []))     # the saved walls, as stored now
        self.wall_router = FakeWallRouter()
        self.mode_router = FakeModeRouter()
        self.previews = []              # (kind, address) requested
        self.physical = {}              # ip -> the MAC the device answering there reports ("" = silent)
        self.identity_reads = []        # addresses asked who they are
        self.observed = 0
        self.app = Flask("luma-test")
        self.app.config["TESTING"] = True
        ui = os.path.join(self.dir, "ui")
        os.makedirs(ui)
        for page in ("login.html", "user.html", "admin_users.html"):
            with open(os.path.join(ui, page), "w", encoding="utf-8") as handle:
                handle.write("<html>%s</html>" % page)
        self.remote = luma_wiring.RemoteSystem(
            self.app, data_dir=self.dir, ui_dir=ui, default_port=18089,
            load_cache=lambda: copy.deepcopy(self.units), route_av=self.router,
            observe=self._observe, preview_fetch=self._preview,
            walls=luma_walls.WallDirectory(lambda: self.wall_defs, wall_device_id),
            route_wall=self.wall_router, route_mode=self.mode_router,
            identify=self._identify)
        self.store = self.remote.store
        self.hits = []                  # handler names that actually ran
        self._add_stub_routes()

    def _add_stub_routes(self):
        """Routes the real application has, with the same rule strings, so the gate
        classifies them exactly as it would in production. They do nothing."""
        app = self.app

        @app.route("/ui/<path:filename>")
        def _ui(filename):
            self.hits.append("ui:" + filename)
            return "file " + filename

        @app.route("/", endpoint="_root")
        def _root():
            self.hits.append("root")
            return "home"

        @app.route("/api/config", methods=["GET", "POST"])
        def _config():
            self.hits.append("config")
            return {"ok": True}

        @app.route("/api/list_dir")
        def _list_dir():
            self.hits.append("list_dir")
            return {"ok": True}

        @app.route("/api/route_matrix", methods=["POST"])
        def _route_matrix():
            self.hits.append("route_matrix")
            return {"ok": True}

    # -- the stubbed device-facing callables ------------------------------
    def _identify(self, ip):
        """Who the device at `ip` says it is: `physical` if the test set it, else the cache's own
        row for that address (a world in which nothing has moved)."""
        self.identity_reads.append(ip)
        if ip in self.physical:
            return self.physical[ip]
        return next((u.get("mac", "") for u in self.units if u.get("ip") == ip), "")

    def _observe(self):
        self.observed += 1

    def _preview(self, kind, address, fresh=False):
        self.previews.append((kind, address))
        return {"image": b"\xff\xd8\xff\xd9", "type": "image/jpeg"}

    # -- helpers ----------------------------------------------------------
    def turn_remote_on(self):
        """Make the gate treat remote requests as admitted, without opening a socket."""
        self.remote.listener._server = object()
        self.remote.listener._bound = ("192.0.2.200", 18089)

    def client(self, remote=True):
        client = self.app.test_client()
        if remote:
            client.environ_base["luma.listener"] = "remote"
        return client

    def admin(self, name="root"):
        result = self.store.create(name, ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        assert result["ok"], result
        return result["user"]

    def user(self, name="alex", sources=(), destinations=(), groups=(), walls=(),
             standard=True, matrix=False):
        perms = luma_users.Permissions(sources, destinations, groups, walls, standard, matrix)
        result = self.store.create(name, USER_PASSWORD, luma_users.ROLE_USER, perms)
        assert result["ok"], result
        return result["user"]

    def sign_in(self, name, password, remote=True):
        """A client carrying a real session cookie and its CSRF token."""
        client = self.client(remote=remote)
        response = client.post("/api/auth/login", json={"username": name, "password": password})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        client.csrf = body["csrf_token"]
        return client

    def post(self, client, path, body=None, csrf=True):
        headers = {"X-LumaSuite-CSRF": client.csrf} if csrf and hasattr(client, "csrf") else {}
        return client.post(path, json=body if body is not None else {}, headers=headers)

    def close(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


def identity(unit):
    return luma_projection.unit_identity(unit)


# ----------------------------------------------------------------------------------
# The real application, isolated
# ----------------------------------------------------------------------------------

class FakeDevices:
    """An in-memory stand-in for the JSON-RPC device transport.

    Installed in place of `lumaserver._ws_call_auth` (and the switcher helpers), so
    the real route code runs against a model with no socket involved. The fence
    blocks the real transport regardless; this is what lets a test say what a
    device *reports*, including a device that accepts a command and does not hold it.
    """

    def __init__(self):
        self.calls = []                    # (ip, method)
        self.encoder_stream = {}           # encoder ip -> stream name it reports
        self.decoder_stream = {}           # decoder ip -> stream the decoder currently holds
        self.ignores_set = set()           # decoder ips that accept a Set and do not hold it
        self.follows_video = {}            # decoder ip -> followvideoenable
        self.readback_script = {}          # decoder ip -> list of values returned by Get, in order
        self.unreachable = set()
        self.on_set = None                 # hook called with (ip, streamname) at the moment of the write
        self.wall_info = {}                # decoder ip -> {"vmenable": bool, "vwmatrix": "r_c_row_col"}
        self.wall_ignores = set()          # decoder ips that accept a wall Set and do not hold it
        self.mac_at = {}                   # ip -> the hardware address the device there reports
        self.cache_mac = None              # fallback: what the application's own cache says

    def call(self, ip, method, params=None, timeout=None):
        self.calls.append((ip, method))
        if ip in self.unreachable:
            return None
        if method == "Network.Get":
            # The device's own hardware address: what a route confirms before it writes.
            mac = self.mac_at[ip] if ip in self.mac_at else (
                self.cache_mac(ip) if self.cache_mac else "")
            return {"result": {"mac": mac}} if mac else {"result": {}}
        if method == "StandardVideoWallInfo.Get":
            held = self.wall_info.get(ip) or {"vmenable": False, "vwmatrix": "1_1_1_1"}
            return {"result": dict(held)}
        if method == "StandardVideoWallInfo.Set":
            if ip not in self.wall_ignores:
                self.wall_info[ip] = {"vmenable": bool((params or {}).get("vmenable")),
                                      "vwmatrix": (params or {}).get("vwmatrix")}
            return {"result": {}}
        if method == "VideoMultiIpStream.Get":
            name = self.encoder_stream.get(ip)
            return {"result": {"streamname": name}} if name else {"result": {}}
        if method == "SelectVideoStream.Set":
            if self.on_set:
                self.on_set(ip, (params or {}).get("streamname"))
            if ip not in self.ignores_set:
                self.decoder_stream[ip] = (params or {}).get("streamname")
            return {"result": {"selecttype": 0}}
        if method == "SelectVideoStream.Get":
            script = self.readback_script.get(ip)
            if script:
                return {"result": {"streamname": script.pop(0)}}
            return {"result": {"streamname": self.decoder_stream.get(ip, "")}}
        if method == "SelectAudioStream.Get":
            return {"result": {"followvideoenable": self.follows_video.get(ip, True)}}
        return {"result": {}}

    def methods(self, ip=None):
        return [m for (i, m) in self.calls if ip is None or i == ip]

    def writes(self):
        return [(i, m) for (i, m) in self.calls if m.endswith(".Set")]


class RealApp:
    """The real lumaserver, isolated: state is snapshotted and put back afterwards.

    Tests that import the application share one process-wide instance, so each one
    saves what it will change and restores it (a test class that replaces a
    module-level name must put it back, or the damage lands in an unrelated test).
    """

    def __init__(self, testcase):
        import lumaserver
        from unittest import mock
        self.m = lumaserver
        self.mock = mock
        self.case = testcase
        self.devices = FakeDevices()
        self.devices.cache_mac = self._cache_mac
        with lumaserver.cache_lock:
            self._cache = copy.deepcopy(dict(lumaserver.cache_units))
            self._generation = lumaserver.cache_generation
        self._config = copy.deepcopy(dict(lumaserver.CONFIG))
        self._verify = dict(lumaserver._cache_verify_state)
        testcase.addCleanup(self.restore)
        lumaserver.ROUTE_READBACK_PAUSE_SECONDS = 0
        self.patch("_ws_call_auth", self.devices.call)
        self.patch("_ws_get_selected_stream_switcher",
                   lambda ip, timeout: self.devices.decoder_stream.get(ip))
        self.patch("_ws_call_switcher_no_password", self._switcher_call)
        self.patch("_ws_set_selected_stream_switcher",
                   lambda ip, name, timeout: self._switch(ip, name))
        self.patch("_persist_units_cache", lambda: None)

    def _cache_mac(self, ip):
        with self.m.cache_lock:
            unit = self.m.cache_units.get(ip)
            return (unit or {}).get("mac", "") if isinstance(unit, dict) else ""

    def _switcher_call(self, ip, method, params=None, timeout=None):
        """The CS31 transport (JSON-RPC, no password), answered from the same model.

        A read of an idle switcher answers with an empty stream name (or none at all) -- an answer,
        not a failure; an address in `devices.unreachable` gives no reply.
        """
        self.devices.calls.append((ip, method))
        if ip in self.devices.unreachable:
            return None
        if method == "SelectVideoStream.Get":
            name = self.devices.decoder_stream.get(ip)
            if name is None:
                return {"result": {}}
            return {"result": {"streamname": name, "selecttype": 0}}
        return {"result": {}}

    def _switch(self, ip, name):
        self.devices.calls.append((ip, "switcher.Set"))
        if ip not in self.devices.ignores_set:
            self.devices.decoder_stream[ip] = name
        return name

    def patch(self, name, replacement):
        patcher = self.mock.patch.object(self.m, name, replacement)
        patcher.start()
        self.case.addCleanup(patcher.stop)

    def seed(self, units=None):
        with self.m.cache_lock:
            self.m.cache_units.clear()
            for unit in copy.deepcopy(list(units if units is not None else ALL_UNITS)):
                self.m.cache_units[unit["ip"]] = unit
        for unit in (units if units is not None else ALL_UNITS):
            if unit["type"] == "encoder":
                self.devices.encoder_stream[unit["ip"]] = unit["enc"]["video"]["streamname"]
            else:
                self.devices.decoder_stream[unit["ip"]] = unit["dec"]["video"]["streamname"]

    def unit(self, ip):
        with self.m.cache_lock:
            return copy.deepcopy(self.m.cache_units.get(ip))

    def restore(self):
        with self.m.cache_lock:
            self.m.cache_units.clear()
            self.m.cache_units.update(self._cache)
            self.m.cache_generation = self._generation
        self.m.CONFIG.clear()
        self.m.CONFIG.update(self._config)
        self.m._cache_verify_state.update(self._verify)
        self.m.ROUTE_READBACK_PAUSE_SECONDS = 0.25


class RealRemote:
    """Turn remote access 'on' for the real application and put it back afterwards."""

    def __init__(self, testcase, app):
        import lumaserver
        self.m = lumaserver
        self.remote = lumaserver.REMOTE
        self.store = self.remote.store
        testcase.addCleanup(self.restore)
        self.remote.listener._server = object()
        self.remote.listener._bound = ("192.0.2.200", 18089)

    def sign_in(self, name, password):
        client = self.m.APP.test_client()
        client.environ_base["luma.listener"] = "remote"
        response = client.post("/api/auth/login", json={"username": name, "password": password})
        assert response.status_code == 200, response.get_json()
        client.csrf = response.get_json()["csrf_token"]
        return client

    def post(self, client, path, body=None):
        return client.post(path, json=body if body is not None else {},
                           headers={"X-LumaSuite-CSRF": client.csrf})

    def restore(self):
        self.remote.listener._server = None
        self.remote.listener._bound = None
        with self.store._lock:
            self.store._accounts.clear()
            self.store._sessions.clear()
            self.store._audit.clear()
        with self.remote.groups._lock:
            self.remote.groups._groups.clear()
        with self.remote.aliases._lock:
            self.remote.aliases._aliases.clear()
        for path in (self.remote.users_file, self.remote.groups_file, self.remote.aliases_file):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
