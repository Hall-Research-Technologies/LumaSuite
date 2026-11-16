"""Whether a device is answering, learned from reads the application already makes.

The shared matrix refresh reads every known encoder and decoder. Whether each read
was answered is recorded on the unit, so "offline" costs no extra traffic and every
surface (the administrator's matrix, the User page, groups, walls, the route gates)
consumes the same derived answer.

One missed read is not "offline": a dropped frame or a busy device would otherwise
disable a working display for a moment. A device is offline after MISS_LIMIT
consecutive unanswered refreshes, and any answer clears it at once. A unit that has
never been polled carries no record and is not called offline: the scan that found it
had just read it.
"""
from __future__ import annotations

import time

#: Consecutive unanswered refreshes before a device is called offline.
MISS_LIMIT = 2

FIELD = "reach"


def record(unit, answered, now=None) -> None:
    """Note one refresh's outcome on `unit` (mutates it; the caller holds the lock)."""
    if not isinstance(unit, dict):
        return
    previous = unit.get(FIELD) if isinstance(unit.get(FIELD), dict) else {}
    misses = 0 if answered else int(previous.get("misses") or 0) + 1
    unit[FIELD] = {"misses": min(misses, 1000),
                   "ts": int(time.time() if now is None else now)}


def is_offline(unit) -> bool:
    """True when the unit has missed MISS_LIMIT refreshes in a row."""
    if not isinstance(unit, dict):
        return False
    record_ = unit.get(FIELD)
    if not isinstance(record_, dict):
        return False
    try:
        return int(record_.get("misses") or 0) >= MISS_LIMIT
    except (TypeError, ValueError):
        return False
