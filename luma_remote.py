"""Optional remote access: a second listener, and the trust signal it carries.

LumaSuite has always served loopback only, and every one of its routes is
anonymous because of it. Remote access cannot simply rebind that socket: the
moment the application answers on a routable address, the one control it had is
gone. So this module does not move the listener. It adds a second one.

    listener LOCAL   127.0.0.1:<port>   always running
    listener REMOTE  <adapter>:<port>   only while remote access is enabled

Which listener accepted a connection is decided by the kernel and recorded by
our own WSGI wrapper before Flask sees the request. That is the whole trust
signal, and it is why it cannot be forged: a client can set any header it likes,
but CGI/WSGI maps header names to `HTTP_UPPER_UNDERSCORE`, so a key containing a
dot -- `luma.listener` -- is not reachable from the wire at all. Comparing
`request.remote_addr` against a loopback set would have been the obvious
alternative and is weaker: it is an inference about an address rather than a
fact about a socket, and it quietly becomes wrong the day somebody puts a proxy
in front.

Nothing here authenticates anybody. It establishes *where a request arrived*;
`luma_access` says who may call what, and the gate joins the two.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading

log = logging.getLogger(__name__)

#: WSGI environ key naming the listener a request arrived on. The dot is
#: load-bearing: HTTP headers become HTTP_X_Y, so nothing a client sends can
#: ever produce this key.
LISTENER_KEY = "luma.listener"

LOCAL = "local"
REMOTE = "remote"

#: Loopback is the only address family that means "this machine".
_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")


class _ListenerTag:
    """WSGI middleware stamping which listener served a request.

    One instance wraps the application per listener, so the value is fixed when
    the server is created rather than computed per request. There is no code
    path that derives it from request content.
    """

    def __init__(self, app, listener: str, host_check=None):
        self._app = app
        self._listener = listener
        self._host_check = host_check

    def __call__(self, environ, start_response):
        # Overwrite unconditionally. If a key of this name could somehow be
        # present it did not come from us, and ours is the only correct answer.
        environ[LISTENER_KEY] = self._listener
        if self._listener == LOCAL and not is_loopback_host_header(
                environ.get("HTTP_HOST")):
            return _misdirected(start_response)
        if (self._listener == REMOTE and self._host_check is not None
                and not self._host_check(environ.get("HTTP_HOST"))):
            return _misdirected_remote(start_response)
        return self._app(environ, start_response)


def is_loopback_host_header(value) -> bool:
    """Whether a Host header names this computer: `localhost` or a loopback literal.

    The local listener trusts whoever reaches it, because only this computer can.
    A web page the operator visits can still reach it: after the page has loaded,
    its own name is made to resolve to 127.0.0.1 (DNS rebinding) and the page then
    reads and writes as the local operator, from a request whose Host header is
    that page's name. Refusing any Host that is not loopback closes that, and costs
    an operator nothing: their browser is pointed at 127.0.0.1 or localhost.

    A missing Host (HTTP/1.0) is refused for the same reason: it is not evidence
    of anything.
    """
    host = str(value or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):                         # [::1] or [::1]:8080
        end = host.find("]")
        host = host[1:end] if end > 0 else ""
    elif host.count(":") == 1:                       # name:port or a.b.c.d:port
        host = host.split(":", 1)[0]
    if host.endswith("."):
        host = host[:-1]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


MIN_PORT, MAX_PORT = 1024, 65535


class PortError(ValueError):
    """An unusable Remote Access port, with a sentence an operator can read."""


def validate_port(value) -> int:
    """The Remote Access port as an int, or PortError.

    The same rule as the local application port (a whole number from 1024 to 65535; a bool, blank, text,
    a float or a very long number is not one), kept as its own function because the Remote Access
    listener must not depend on the local-port module. `tests/test_request_bodies.py` holds the two
    equal over a table of values.
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


def split_host_header(value):
    """(host, port) from a Host header: lower-case, no brackets, no trailing dot; port is an int or None."""
    text = str(value or "").strip().lower()
    port = None
    if text.startswith("["):
        end = text.find("]")
        rest = text[end + 1:] if end > 0 else ""
        host = text[1:end] if end > 0 else ""
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif text.count(":") == 1:
        host, _, tail = text.partition(":")
        port = int(tail) if tail.isdigit() else None
        if tail and not tail.isdigit():
            return "", None
    else:
        host = text
    if host.endswith("."):
        host = host[:-1]
    return host, port


def machine_names():
    """The names this computer answers to, lower-case: its hostname, its `.local` form, and localhost."""
    names = {"localhost"}
    for getter in (socket.gethostname, socket.getfqdn):
        try:
            value = str(getter() or "").strip().lower().rstrip(".")
        except OSError:
            value = ""
        if value:
            names.add(value)
            names.add(value.split(".")[0])
    for name in list(names):
        if "." not in name:
            names.add(name + ".local")
    return names


def host_allowed(header, bound, extra_names=()):
    """Whether a remote request's Host header names this listener.

    Allowed: the address Remote Access is bound to (with its port, when the header carries one), this
    computer's own name and `localhost`, loopback literals, and the names a supplied certificate
    carries (an operator who gave the certificate a DNS name reaches it by that name). Anything else
    is refused: a name that merely points at this address (DNS rebinding) is not evidence of
    anything, and nothing here is derived from the request. A missing Host is refused.
    """
    host, port = split_host_header(header)
    if not host:
        return False
    if bound and port is not None and port != int(bound[1]):
        return False
    if bound and host == str(bound[0]).strip().lower():
        return True
    if is_loopback_host_header(host):
        return True
    names = machine_names() | {str(n).strip().lower().rstrip(".") for n in extra_names or ()}
    return host in names


def _misdirected_remote(start_response):
    body = (b"This address is not one this listener is served on. Use the address shown in "
            b"LumaSuite's Remote Access settings.")
    start_response("421 Misdirected Request",
                   [("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store")])
    return [body]


def quiet_handler():
    """A request handler that names neither the web framework nor the interpreter in `Server`."""
    from werkzeug.serving import WSGIRequestHandler

    class QuietHandler(WSGIRequestHandler):
        def version_string(self):
            return "LumaSuite"

    return QuietHandler


def _misdirected(start_response):
    body = (b"This address is only served to the computer running the "
            b"application, by its own address (127.0.0.1 or localhost).")
    start_response("421 Misdirected Request",
                   [("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store")])
    return [body]


def tag_for_bind(host: str) -> str:
    """Which tag a listener bound to `host` deserves.

    Loopback earns local trust; anything else does not, *including* the
    application's own primary listener. That matters because LUMA_HOST can be
    forced to a routable address without remote access being enabled at all,
    and a listener that answered the network while handing out local trust
    would be the whole boundary gone.
    """
    return LOCAL if is_loopback_address(host) else REMOTE


def serve_tagged(host, port, app, **kwargs):
    """Build a WSGI server whose requests carry the right listener tag.

    Every network listener in the product is created through this, so "which
    listener accepted this" is decided once, from the address actually bound,
    rather than at three call sites that could disagree.
    """
    from werkzeug.serving import make_server
    kwargs.setdefault("request_handler", quiet_handler())
    return make_server(host, port, _ListenerTag(app, tag_for_bind(host)),
                       **kwargs)


def listener_of(environ) -> str:
    """Which listener served this request.

    An untagged request is LOCAL, and that is a statement about what can
    actually produce one. Every listener the product opens is tagged by
    `serve_tagged` or by `RemoteListener`, so a request with no tag did not
    arrive over a socket at all -- it came from an in-process caller such as
    Flask's test client, which is as local as anything gets.

    This was the other way round first, on the reasoning that unknown should
    mean untrusted. It made the whole existing test suite fail closed, which
    looked safe and was actually a good way to hide the real hole: the packaged
    launcher was opening a third, unwrapped listener. Tagging every listener at
    the point it is bound fixes the cause; treating unknown as remote only
    concealed it.
    """
    value = environ.get(LISTENER_KEY)
    return REMOTE if value == REMOTE else LOCAL


def is_local_request(environ) -> bool:
    """True for a request that did not arrive on a network-facing listener.

    Deliberately not `remote_addr in 127.0.0.0/8`: that is a statement about an
    address the request claims to come from, and this is a statement about the
    socket the operating system accepted it on.
    """
    return listener_of(environ) == LOCAL


def is_loopback_address(address: str) -> bool:
    """Whether a literal address is loopback. Used for validating a bind choice.

    This is about configuration, never about trusting a request.
    """
    try:
        return ipaddress.ip_address(str(address).strip()) in _LOOPBACK
    except ValueError:
        return False


def is_wildcard_address(address: str) -> bool:
    """Whether an address means "every interface on this machine".

    Its own predicate because `is_loopback_address("0.0.0.0")` is False --
    0.0.0.0 is the unspecified address, not a loopback one -- so a check
    written to refuse loopback silently accepts the one value that exposes the
    host on every network it is attached to. A test that removed the
    availability check found that: the guard had been passing for the wrong
    reason.
    """
    try:
        return ipaddress.ip_address(str(address).strip()).is_unspecified
    except ValueError:
        return False


def _why_bind_failed(host: str, port) -> str:
    """The operating system's own reason, recovered after a failed bind.

    Called only once a bind has already been declined. werkzeug prints the
    reason to stderr and calls sys.exit(1), so the SystemExit that arrives
    carries nothing an operator could act on -- and "not on this machine" and
    "something else is using that port" need different remedies.
    """
    family = socket.AF_INET6 if ":" in str(host) else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.bind((str(host), int(port)))
    except OSError as exc:
        return str(exc.strerror or exc)
    except (TypeError, ValueError) as exc:
        return str(exc)
    finally:
        probe.close()
    return "the address or port was briefly unavailable"


def is_usable_remote_address(address: str) -> bool:
    """Whether this is a concrete address an administrator may bind remotely.

    A remote listener is always a deliberate choice of one network. Loopback is
    already served, the wildcard is every network at once, and anything that is
    not an address at all cannot be one of them.

    Nor can anything that is not a host: a broadcast or multicast address
    parses perfectly well and cannot be bound to, and this guard used to call
    both usable. Refused as a category rather than as a list of values, so the
    next reserved range does not need remembering. Link-local is kept, because
    a machine really can have one and an administrator on a directly attached
    network may legitimately choose it.
    """
    text = str(address or "").strip()
    if not text:
        return False
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return False
    if is_loopback_address(text) or is_wildcard_address(text):
        return False
    if parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified:
        # `is_reserved` is 240.0.0.0/4, which includes the limited broadcast
        # address 255.255.255.255 -- checked, because a separate clause for it
        # was carrying a comment claiming otherwise.
        return False
    return True


class RemoteListener:
    """The optional second listener, and its lifecycle.

    Kept separate from the loopback server so that enabling and disabling remote
    access is starting and stopping *this* object, and never touching the one
    that serves the local operator. Disabling can therefore never break local
    use, which is the property section 4 of the brief asks for.
    """

    def __init__(self, app):
        self._app = app
        self._server = None
        self._thread = None
        self._lock = threading.RLock()
        self._bound = None                      # (host, port) while running
        self._scheme = "http"

    # -- state ---------------------------------------------------------
    @property
    def running(self) -> bool:
        with self._lock:
            return self._server is not None

    @property
    def bound(self):
        with self._lock:
            return self._bound

    def describe(self) -> dict:
        """What Settings shows: enabled, where, and the URL that reaches it.

        The advertised URL carries the scheme actually in use. An address given
        as http:// when the listener is serving TLS would simply fail to
        connect, and the other way round would be worse.
        """
        with self._lock:
            # Both, not just the server. They are set together, but "there is
            # a server and we do not know where it is bound" is a state this
            # must answer rather than raise on -- a caller building an
            # advertised URL got a TypeError out of it instead of "not
            # running", and the fail-safe reading of not knowing is not
            # running.
            if not self._server or not self._bound:
                return {"enabled": False, "host": None, "port": None,
                        "url": None, "scheme": None, "secure": False}
            host, port = self._bound
            return {"enabled": True, "host": host, "port": port,
                    "scheme": self._scheme,
                    "secure": self._scheme == "https",
                    "url": f"{self._scheme}://{host}:{port}/"}

    # -- lifecycle -----------------------------------------------------
    def start(self, host: str, port: int, ssl_context=None, allowed_names=()) -> dict:
        """Begin answering on `host:port`, over TLS when a context is given.

        Returns a result dict rather than raising for an ordinary operator
        mistake -- an address that is not on this machine, or a port already
        taken -- because those are answers the Settings page has to display, not
        faults.

        There is exactly one remote listener, and it is either TLS or it is
        not. No plaintext listener is opened alongside an HTTPS one, not even
        to redirect: a redirect surface would accept a request -- including,
        eventually, somebody typing a password into a page it served -- before
        it could be protected. An operator who reaches the wrong scheme gets a
        connection failure, which is unambiguous.
        """
        host = str(host or "").strip()
        if not host:
            return {"ok": False, "error": "no address selected"}
        if not is_usable_remote_address(host):
            # Loopback is already served and binding it here would tag remote
            # requests as local; the wildcard exposes every network at once,
            # which is never something to arrive at by default.
            reason = ("loopback is always served locally and cannot be chosen "
                      "as the remote address" if is_loopback_address(host)
                      else ("choose one network rather than all of them"
                            if is_wildcard_address(host)
                            else "that is not a usable address"))
            return {"ok": False, "error": reason}
        try:
            port = int(port)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid port"}

        with self._lock:
            # Make before break.
            #
            # Reconfiguring used to stop the running listener first and only
            # then discover that the new address was not on this machine, or
            # that the port was taken -- which left remote access off, with the
            # failure arriving as a dropped connection rather than as a
            # message. Building the new one first means a reconfiguration that
            # cannot succeed changes nothing at all.
            #
            # Rebinding the very same address and port is the one case that
            # cannot work that way, because the old listener is still holding
            # it; there, stopping first is what was asked for.
            rebinding_in_place = (self._server is not None
                                  and self._bound == (host, port))
            if rebinding_in_place:
                self.stop()
            try:
                from werkzeug.serving import make_server
                # The remote listener serves the SAME application object, with
                # its own tag. One application, one set of state, two doors.
                # Captured per server, so a replacement being made while the old listener still
                # answers cannot change what the old one accepts.
                names = tuple(allowed_names or ())

                def host_check(header, _bound=(host, port), _names=names):
                    return host_allowed(header, _bound, _names)

                server = make_server(host, port,
                                     _ListenerTag(self._app, REMOTE, host_check),
                                     threaded=True, ssl_context=ssl_context,
                                     request_handler=quiet_handler())
            except OSError as exc:
                log.warning("[REMOTE] cannot bind %s:%s: %s", host, port,
                            type(exc).__name__)
                return {"ok": False,
                        "error": f"cannot bind {host}:{port} ({exc.strerror or exc})"}
            except SystemExit:
                # werkzeug answers a bind failure by printing to stderr and
                # calling sys.exit(1). SystemExit is a BaseException, so it
                # passes both clauses above and, left alone, kills the request
                # thread instead of answering the operator.
                #
                # It also carries no reason, so the reason is recovered here --
                # safely, because the listener that works is still running.
                reason = _why_bind_failed(host, port)
                log.warning("[REMOTE] cannot bind %s:%s: %s", host, port, reason)
                return {"ok": False,
                        "error": f"cannot bind {host}:{port} ({reason})"}
            except Exception as exc:
                log.warning("[REMOTE] listener failed to start: %s", type(exc).__name__)
                return {"ok": False, "error": f"listener failed: {type(exc).__name__}"}

            # The new listener exists, so the old one can go now.
            if self._server is not None:
                self.stop()

            thread = threading.Thread(
                target=server.serve_forever,
                name="luma-remote-listener", daemon=True)
            thread.start()
            self._server, self._thread, self._bound = server, thread, (host, port)
            self._scheme = "https" if ssl_context is not None else "http"
            log.info("[REMOTE] listening on %s://%s:%s", self._scheme, host, port)
            return {"ok": True, **self.describe()}

    def stop(self) -> dict:
        """Stop answering remotely. The local listener is untouched."""
        with self._lock:
            server, thread = self._server, self._thread
            self._server = self._thread = self._bound = None
            self._scheme = "http"
        if server is None:
            return {"ok": True, "enabled": False}
        try:
            server.shutdown()
            server.server_close()
        except Exception as exc:
            log.info("[REMOTE] shutdown raised %s", type(exc).__name__)
        if thread is not None:
            # A listener that outlives the call that stopped it is a socket
            # still accepting connections nobody believes are possible.
            thread.join(timeout=5.0)
            if thread.is_alive():
                log.warning("[REMOTE] listener thread did not stop within 5s")
                return {"ok": False, "enabled": False,
                        "error": "listener did not stop cleanly"}
        log.info("[REMOTE] stopped")
        return {"ok": True, "enabled": False}


def local_app(app):
    """Wrap the application for the loopback listener."""
    return _ListenerTag(app, LOCAL)


def usable_bind_addresses(adapters) -> list:
    """Filter enumerated adapters down to ones that can actually be bound.

    Takes whatever /api/adapters already produces, so there is one adapter
    enumeration in the product rather than a second one written for this
    feature. Loopback is excluded: it is already served.
    """
    usable = []
    for adapter in adapters or []:
        address = (adapter.get("address") or adapter.get("ip") or "").strip()
        # One concrete network. Loopback is already served, and the wildcard is
        # not a network an administrator can reason about.
        if not is_usable_remote_address(address):
            continue
        usable.append({
            "address": address,
            "name": adapter.get("name") or adapter.get("adapter") or address,
            "cidr": adapter.get("cidr") or "",
        })
    return usable


def can_bind(host: str, port: int) -> bool:
    """Whether this machine can bind that address, without keeping it.

    Used to validate a selection before saving it, so Settings can refuse an
    address that belongs to an adapter that has since gone away.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((str(host), int(port)))
            return True
        finally:
            probe.close()
    except (OSError, ValueError):
        return False
