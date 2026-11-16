"""Where a remote caller may make the server connect.

Discovery and device control are different trust boundaries.

    DISCOVERY   probes candidate addresses to find out what is there.
    CONTROL     talks to a device the application already knows, using the
                credential it holds for that device.

The server attaches a device password to nearly every connection it makes. If
the address comes from a remote browser, that browser can choose where the
password goes, so the two rules below are applied to every request that
arrived on the network-facing listener. The local operator is untouched.

    control     every address the request names must be one the server
                already holds in its inventory. An address it has never seen
                is refused, whatever it looks like.
    discovery   an address the server has never seen may be probed only if it
                is a unicast address that is not on the public Internet:
                never loopback, link-local, multicast, unspecified, reserved
                or global. That keeps a stolen session from aiming the
                configured scan credential at a host of its own choosing,
                while still letting a remote administrator scan the site.

Which rule applies is decided by the route; which values the rule examines is
decided by name, across every route, so an endpoint added later that takes an
`ip` is covered without anyone remembering to list it. A test fails when a
handler reads an address-like body key this module does not know.
"""
from __future__ import annotations

import ipaddress
import re

#: Body or query keys that name a device the server will connect to.
ADDRESS_KEYS = frozenset({
    "ip", "ips", "targets", "decoder_ip", "encoder_ip", "source_ip",
    "destination_ip", "device_ip", "video_encoder_ip", "audio_encoder_ip",
})

#: Routes that may probe an address the server has never seen.
DISCOVERY_ROUTES = frozenset({
    "/api/scan",
    "/api/tcp_probe",
    "/api/ping",
    "/api/producer/test_device",
})

#: Keys whose value is an expression, not one address.
EXPRESSION_KEYS = frozenset({"targets"})

_MAC = re.compile(r"^[0-9A-Fa-f]{2}([:\-]?[0-9A-Fa-f]{2}){5}$")


class Refusal(Exception):
    """A request named a destination it may not."""

    def __init__(self, status: int, reason: str, message: str):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message


def parse_ipv4(text) -> ipaddress.IPv4Address | None:
    """One literal IPv4 address, or None.

    Deliberately strict: no hostname, port, brackets, zone, userinfo, path or
    surrounding space. A string that had to be cleaned up was not an address,
    and one that is spliced into a URL must not be able to add a host.
    """
    if not isinstance(text, str) or text != text.strip() or not text:
        return None
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        return None


def discovery_scope_ok(address: ipaddress.IPv4Address) -> bool:
    return not (address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_unspecified
                or address.is_reserved or address.is_global
                or address == ipaddress.IPv4Address("255.255.255.255"))


def _values(value):
    """Every string a request value carries: a string, a list, or records."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                if "ip" in item:
                    yield from _values(item["ip"])
            else:
                yield from _values(item)
    elif isinstance(value, dict):
        for key in ("ip", "address"):
            if key in value:
                yield from _values(value[key])
    elif value is None or isinstance(value, (bool, int, float)):
        return
    else:
        yield repr(value)


def named_addresses(payload) -> list[tuple[str, str]]:
    """(key, value) for every address-like value in a request."""
    found = []
    if not isinstance(payload, dict):
        return found
    for key in ADDRESS_KEYS:
        if key in payload:
            for value in _values(payload[key]):
                found.append((key, value))
    return found


def check(rule: str, payload, inventory, expand_targets) -> None:
    """Raise `Refusal` unless every destination in `payload` is permitted.

    `inventory` returns the set of addresses the server currently holds. It is
    called at most once, and not at all for a request that names no address.
    `expand_targets` turns a scan expression into the addresses that would be
    probed, so the rule judges what will be contacted rather than the text.
    """
    discovery = rule in DISCOVERY_ROUTES
    named = named_addresses(payload)
    if not named:
        return
    known = set(inventory())

    for key, value in named:
        if key in EXPRESSION_KEYS and discovery:
            text = value.strip()
            if not text:
                continue
            try:
                addresses = expand_targets(text)
            except ValueError as exc:
                raise Refusal(400, "invalid_address", str(exc))
            for address in addresses:
                _discovery_address(address, known)
            continue

        # A query string carries several addresses as one comma-separated value.
        for text in (piece.strip() for piece in value.split(",")):
            if not text:
                continue
            if _MAC.match(text):
                # An identity, resolved by the handler against recorded state.
                continue
            if discovery:
                _discovery_address(text, known)
            else:
                _control_address(text, known)


def _discovery_address(text: str, known: set) -> None:
    if text in known:
        return
    address = parse_ipv4(text)
    if address is None:
        raise Refusal(400, "invalid_address",
                      "That is not a valid IPv4 address.")
    if not discovery_scope_ok(address):
        raise Refusal(400, "address_out_of_scope",
                      f"{address} cannot be scanned from a remote session.")


def _control_address(text: str, known: set) -> None:
    if parse_ipv4(text) is None:
        raise Refusal(400, "invalid_address",
                      "That is not a valid IPv4 address.")
    if text not in known:
        raise Refusal(404, "unknown_device",
                      f"{text} is not a device this application has found. "
                      "Scan for it first.")
