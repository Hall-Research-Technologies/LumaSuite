"""The hardware fence for the automated test suite.

Automated tests must never reach a real device. Until Phase 8B this was
enforced in `run_tests.py`, which meant a plain `python -m unittest`, an IDE
runner or a mutation harness ran the same tests with no protection at all --
the rule held only for as long as every future runner remembered to install it.

The fence now lives with the tests. `tests/__init__.py` installs it, and every
test module installs it too, so it is present however the suite is entered:
through `run_tests.py`, through `python -m unittest`, through discovery, by
`python tests/<file>.py` as the CI workflow runs it, or by a runner loading a
module file directly.

What it covers: WebSocket connections, raw TCP, UDP datagrams, and therefore
everything layered on a socket -- HTTP, the device preview fetches, and the
JSON-RPC-over-WebSocket transport, which all reach the network through one of
those.

Loopback is deliberately allowed. A Flask test client never opens a socket at
all, but a test that starts a local server and talks to 127.0.0.1 is talking to
itself, not to a device.

Live bench validation is a separate activity that does not import this module.
A test must not write a bench address into a test file; use a documentation
range (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24).
"""
import collections
import socket

# Every refused attempt, so a run can report what it tried to reach.
attempts: collections.Counter = collections.Counter()

# RFC 5737 reserves these for documentation and examples. They are not routed,
# so a datagram addressed to one cannot reach a device. A test that deliberately
# probes an unreachable address is legitimate: it is reported, still refused,
# and it does not fail the gate.
UNROUTABLE_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.")

# An attempt is recorded in whatever shape the caller used: a bare host, a URL,
# or a host with the command appended. Classifying the raw string would read
# "ws://192.0.2.10/wsapp/" as a routable host, so pull the address out first.
import re  # noqa: E402  (kept beside the pattern it exists for)

_ADDRESS = re.compile(r"(?<![0-9.])[0-9]{1,3}(?:[.][0-9]{1,3}){3}(?![0-9.])")

_installed = False
_real_socket = None


class FenceError(OSError):
    """Raised instead of reaching a device. An OSError, so callers handle it."""


def installed() -> bool:
    return _installed


def is_loopback(host) -> bool:
    text = str(host or "")
    return text.startswith("127.") or text in ("::1", "localhost", "")


def reachable(target) -> bool:
    """Would this target have addressed a host that really exists?"""
    found = _ADDRESS.search(str(target))
    if found is None:
        return True              # not an address we recognise: assume the worst
    return not any(found.group(0).startswith(prefix)
                   for prefix in UNROUTABLE_PREFIXES)


def install() -> None:
    """Refuse every connection to anything that is not loopback.

    Idempotent: importing several test modules, or a runner installing it as
    well, must not stack spies on top of each other.
    """
    global _installed, _real_socket
    if _installed:
        return

    try:
        import websocket
    except ImportError:                     # pragma: no cover - websocket is a dep
        websocket = None

    if websocket is not None:
        def ws_spy(url, *args, **kwargs):
            attempts[("websocket", str(url))] += 1
            raise FenceError(
                "the test fence blocked a WebSocket connection to %s" % url)

        websocket.create_connection = ws_spy

    _real_socket = socket.socket

    class FencedSocket(_real_socket):
        def sendto(self, data, address, *args, **kwargs):  # type: ignore[override]
            host = address[0] if isinstance(address, tuple) else address
            if is_loopback(host):
                return super().sendto(data, address, *args, **kwargs)
            command = int.from_bytes(data[8:10], "big") if len(data) >= 10 else -1
            attempts[("udp", "%s cmd=%s" % (host, hex(command)))] += 1
            raise FenceError("the test fence blocked a UDP datagram to %s" % host)

        def connect(self, address, *args, **kwargs):  # type: ignore[override]
            host = address[0] if isinstance(address, tuple) else ""
            if is_loopback(host):
                return super().connect(address, *args, **kwargs)
            attempts[("tcp", str(host))] += 1
            raise FenceError("the test fence blocked a TCP connection to %s" % host)

        def connect_ex(self, address, *args, **kwargs):  # type: ignore[override]
            host = address[0] if isinstance(address, tuple) else ""
            if is_loopback(host):
                return super().connect_ex(address, *args, **kwargs)
            attempts[("tcp", str(host))] += 1
            # ECONNREFUSED, the way a probe reads it. 111 is the Linux value and is
            # kept on Windows; elsewhere the platform's own constant is used (it is
            # 61 on macOS), so a caller comparing against errno.ECONNREFUSED agrees.
            import errno as _errno
            import os as _os
            return 111 if _os.name == "nt" else _errno.ECONNREFUSED

    socket.socket = FencedSocket

    # A name is resolved by a real query before any connection is made, so resolving one is
    # already a network operation. Literal addresses and the loopback name are not.
    _real_getaddrinfo = socket.getaddrinfo

    def fenced_getaddrinfo(host, *args, **kwargs):
        text = str(host or "")
        if is_loopback(text) or _ADDRESS.fullmatch(text) or ":" in text:
            return _real_getaddrinfo(host, *args, **kwargs)
        attempts[("dns", text)] += 1
        raise FenceError("the test fence blocked resolving the name %s" % text)

    socket.getaddrinfo = fenced_getaddrinfo

    # `/api/ping` and the ARP fallback shell out. An ICMP echo to a device is a connection that
    # no socket patch can see, so the programs that talk to the network are refused by name.
    import os
    import subprocess

    _real_popen = subprocess.Popen
    _NETWORK_TOOLS = {"ping", "traceroute", "tracert", "nslookup", "nmap", "arping", "curl", "wget"}

    class FencedPopen(_real_popen):
        def __init__(self, args, *a, **kw):
            program = args if isinstance(args, str) else (args[0] if args else "")
            name = os.path.basename(str(program).strip().split(" ")[0]).lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if name in _NETWORK_TOOLS:
                attempts[("exec", name)] += 1
                raise FenceError("the test fence blocked running %s" % name)
            super().__init__(args, *a, **kw)

    subprocess.Popen = FencedPopen
    _installed = True


def isolate_data_dir(prefix="lumasuite-tests-"):
    """Point the application at a scratch data folder, pre-seeded with empty state.

    Not optional. Starting the application with a fresh but *unseeded* data folder
    still migrates whatever runtime files sit beside `lumaserver.py` -- the
    developer's real device cache and credentials from prior use -- into it. The
    network fence would stop any traffic, but a test must never even see real
    equipment, so every runtime file the migration looks for is created empty
    first. (A seeded file is newer than the legacy one, so it is not replaced.)
    """
    import json
    import os
    import tempfile
    path = os.environ.get("LUMA_DATA_DIR") or tempfile.mkdtemp(prefix=prefix)
    os.environ["LUMA_DATA_DIR"] = path
    os.makedirs(path, exist_ok=True)
    seeds = {"units_cache.json": [],
             "video_walls.json": {"kind": "lumasuite.video_walls", "version": 1, "walls": []},
             "producer_config.json": {},
             "producer_state.json": {"ticker": {"dir": "rtl", "rows": []}, "streams": []},
             "producer2_state.json": {"streams": [], "ticker": {}}}
    for name, body in seeds.items():
        target = os.path.join(path, name)
        if not os.path.exists(target):
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(body, handle)
    return path


def report(stream=None):
    """Print what the run tried to reach. Returns True when nothing routable was."""
    import sys
    stream = stream or sys.stdout
    blocked = sum(attempts.values())
    live = sum(count for (_kind, target), count in attempts.items()
               if reachable(target))
    print("\nHardware isolation: %d connection attempt(s), all blocked at the "
          "socket" % blocked, file=stream)
    for (kind, target), count in attempts.most_common(20):
        note = "" if reachable(target) else "   (RFC 5737 documentation range, unroutable)"
        print("  %5d  %s  %s%s" % (count, kind, target, note), file=stream)
    if live:
        print("  -> %d of these addressed a routable host, which is a failure"
              % live, file=stream)
    return live == 0


# This module is reached two ways: as `tests._fence` when the package is
# imported, and as a top-level `_fence` when a runner loads a test module by
# path with no package. Without this, those are two module objects with two
# counters and two installs -- which showed up as a run reporting zero
# connection attempts while the tests were merrily recording them elsewhere.
import sys as _sys

_sys.modules.setdefault("tests._fence", _sys.modules[__name__])
_sys.modules.setdefault("_fence", _sys.modules[__name__])
