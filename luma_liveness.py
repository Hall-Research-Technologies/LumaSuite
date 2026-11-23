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
import zlib

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


# ---------------------------------------------------------------- re-probe schedule
#
# A unit that has gone quiet is looked at again, but not at the full cadence of the units that
# answer. Measured with 250 silent units on the shared refresh: every sweep spent a worker, a
# socket and up to two seconds (a refused connect on Windows costs about 2 s whatever timeout is
# asked for) on each of them, so the sweep outlasted the page's ten-second interval and the host ran
# ~500 threads and ~2,300 sockets to learn the same thing again.
#
# The schedule is exponential with a cap, and it is derived from the record that already exists
# (`misses` and `ts`), so it adds no persisted field: a unit is due again BACKOFF_BASE seconds after
# its last probe once it is offline, then twice that, and so on up to BACKOFF_CAP. A unit that has
# not yet missed MISS_LIMIT reads is always due -- one dropped frame is not "gone" -- and any
# answer clears the record, which makes it due at once. A manual refresh passes `force` and probes
# everything.

#: Seconds after an offline unit's last probe before it is due again (the first step).
BACKOFF_BASE = 10.0
#: The longest a quiet unit goes unprobed.
BACKOFF_CAP = 120.0


def backoff_seconds(misses) -> float:
    """How long after its last probe a unit with `misses` consecutive unanswered reads waits."""
    try:
        over = int(misses) - MISS_LIMIT
    except (TypeError, ValueError):
        return 0.0
    if over < 0:
        return 0.0
    return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** min(over, 10)))


def jitter(key) -> float:
    """A fixed factor in [0.75, 1.25) for `key`, so units that went quiet together are not due together.

    Without it every unit that missed its reads in the same sweep waits exactly the same time, so at the cap
    the whole crowd falls due in one sweep every BACKOFF_CAP seconds (measured with 1,000 silent units: a
    77-second sweep, 64% of the time). Deterministic -- the same unit always gets the same factor -- so
    nothing is stored and a test can predict it.
    """
    return 0.75 + 0.5 * ((zlib.crc32(str(key).encode("utf-8", "replace")) % 1000) / 1000.0)


def is_due(unit, now=None, force=False) -> bool:
    """True when `unit` should be probed now.

    Never-probed units, units still within the miss limit and any unit under `force` are due. An
    offline unit is due once its backoff since the last probe has elapsed.
    """
    if force or not isinstance(unit, dict):
        return True
    record_ = unit.get(FIELD)
    if not isinstance(record_, dict):
        return True
    try:
        misses = int(record_.get("misses") or 0)
        ts = float(record_.get("ts") or 0)
    except (TypeError, ValueError):
        return True
    wait = backoff_seconds(misses)
    if wait <= 0:
        return True
    wait *= jitter(unit.get("mac") or unit.get("ip") or "")
    current = time.time() if now is None else now
    if ts > current:          # a clock that moved back must not silence a unit for ever
        return True
    return current - ts >= wait
