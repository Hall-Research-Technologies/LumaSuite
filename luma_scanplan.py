"""Bounding a scan request before anything is expanded.

A scan expression is operator text: a selected adapter's network plus any number
of additional (routed) networks. The cost of a scan is the number of addresses it
probes, so that number is worked out arithmetically from the expression and
checked against fixed limits *before* a single address is materialised. Nothing
here ever builds the address space in order to discover that it is too large.

Accepted forms, separated by commas, semicolons or white space:

    192.0.2.17                 one address
    192.0.2.1-254              a last-octet range
    198.51.100.1-198.51.100.254   a full range between two addresses
    192.0.2.0/24               a network (host addresses only)

Limits (module constants, asserted by tests):

    MAX_RANGE_ADDRESSES   addresses one range may name            4096  (a /20)
    MAX_SCAN_ADDRESSES    addresses one request may name          8192
    MAX_RANGES            expressions one request may carry          32

A request over a limit is refused with a sentence, never truncated: a scan that
quietly probed the first 4096 hosts of a /16 would report "nothing found" for
addresses it never tried.
"""
from __future__ import annotations

import ipaddress
import re

MAX_RANGE_ADDRESSES = 4096
MAX_SCAN_ADDRESSES = 8192
MAX_RANGES = 32

_SPLIT = re.compile(r"[,\s;]+")
_OCTET_RANGE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3})$")
_FULL_RANGE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3}(?:\.\d{1,3}){3})$")
_ADDRESS = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class ScanRangeError(ValueError):
    """A scan expression that is malformed, unsafe to probe, or too large."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def _v4(text: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        raise ScanRangeError("invalid_range",
                             "%r is not a valid IPv4 address." % text[:40])


def measure(part: str):
    """(first, count) for one expression, with no address list built.

    `first` is the integer value of the first address probed, `count` how many.
    Raises `ScanRangeError` for anything that is not one of the accepted forms.
    """
    if "/" in part:
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ScanRangeError("invalid_range",
                                 "%r is not a valid network." % part[:40])
        if net.version != 4:
            raise ScanRangeError("invalid_range", "Only IPv4 networks can be scanned.")
        # hosts() excludes the network and broadcast address, except in a /31
        # and /32 where it keeps them; mirror that without iterating.
        total = net.num_addresses
        if net.prefixlen >= 31:
            return int(net.network_address), total
        return int(net.network_address) + 1, total - 2
    m = _OCTET_RANGE.match(part)
    if m:
        base = _v4(m.group(1))
        end = int(m.group(2))
        if end > 255:
            raise ScanRangeError("invalid_range",
                                 "%r is not an address range." % part[:40])
        start = int(str(base).split(".")[-1])
        lo, hi = sorted((start, end))
        return (int(base) - start) + lo, hi - lo + 1
    m = _FULL_RANGE.match(part)
    if m:
        a, b = int(_v4(m.group(1))), int(_v4(m.group(2)))
        lo, hi = sorted((a, b))
        return lo, hi - lo + 1
    if _ADDRESS.match(part):
        return int(_v4(part)), 1
    raise ScanRangeError("invalid_range",
                         "%r is not an address, a range or a network." % part[:40])


def check(targets) -> int:
    """Total addresses `targets` would probe, or raise `ScanRangeError`.

    Blank is allowed and means nothing to scan (0).
    """
    text = str(targets or "").strip()
    if not text:
        return 0
    parts = [p for p in _SPLIT.split(text) if p]
    if len(parts) > MAX_RANGES:
        raise ScanRangeError(
            "too_many_ranges",
            "A scan can name at most %d ranges; this one names %d." % (MAX_RANGES, len(parts)))
    total = 0
    for part in parts:
        _first, count = measure(part)
        if count > MAX_RANGE_ADDRESSES:
            raise ScanRangeError(
                "range_too_large",
                "%s covers %d addresses; one range may cover at most %d (a /20). "
                "Split it into smaller ranges." % (part[:40], count, MAX_RANGE_ADDRESSES))
        total += count
        if total > MAX_SCAN_ADDRESSES:
            raise ScanRangeError(
                "scan_too_large",
                "This scan would probe more than %d addresses. Remove a range "
                "or narrow one." % MAX_SCAN_ADDRESSES)
    return total


def normalise(value) -> list:
    """A saved list of additional ranges from a list or a delimited string.

    Each entry is validated; duplicates are dropped in order. Raises
    `ScanRangeError` rather than saving something that could not be scanned.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if not isinstance(item, str):
                raise ScanRangeError("invalid_range", "Ranges must be text.")
            parts.extend(p for p in _SPLIT.split(item.strip()) if p)
    elif isinstance(value, str):
        parts = [p for p in _SPLIT.split(value.strip()) if p]
    else:
        raise ScanRangeError("invalid_range", "Ranges must be text.")
    seen, out = set(), []
    for part in parts:
        if part not in seen:
            seen.add(part)
            out.append(part)
    check(",".join(out))
    return out
