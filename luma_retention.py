"""How long the Activity log is kept, and how large it may grow.

Two limits, both enforced, because either alone leaves a way to grow without
bound: a day count lets a busy installation fill the disk inside the window, and
a size limit alone lets entries from last year sit there indefinitely.

    retention days    entries older than this are removed          default 30
    maximum size      the log file may not exceed this many bytes  default 4 MiB

The size default comes from the measured entry: a routing or sign-in record is
about 200-250 bytes compact and about 330 bytes in the file as written, so 4 MiB
is roughly 12,000 entries -- over 400 events a day for the whole 30 days, far
more than a facility with a few dozen displays produces. Removing the oldest
entries first is what keeps recent history when the size limit bites.

Pure functions only, so the rules can be tested without a store or a clock.
"""
from __future__ import annotations

DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365

DEFAULT_MAX_BYTES = 4 * 1024 * 1024
MIN_MAX_BYTES = 256 * 1024
MAX_MAX_BYTES = 64 * 1024 * 1024

#: Bytes the file adds around an entry's compact JSON (indentation and commas).
ENTRY_OVERHEAD_BYTES = 128

#: An absolute ceiling on entries, whatever the byte limit is set to. A flood of
#: tiny entries must not defeat the size estimate.
HARD_MAX_ENTRIES = 100_000

SECONDS_PER_DAY = 86400


class RetentionError(ValueError):
    """A retention setting that is out of bounds or not a number."""


def defaults() -> dict:
    return {"days": DEFAULT_RETENTION_DAYS, "max_bytes": DEFAULT_MAX_BYTES}


def limits() -> dict:
    return {"min_days": MIN_RETENTION_DAYS, "max_days": MAX_RETENTION_DAYS,
            "min_bytes": MIN_MAX_BYTES, "max_bytes": MAX_MAX_BYTES}


def _whole(value, name):
    # bool is an int in Python; "True days" is not a number of days.
    if isinstance(value, bool) or value is None:
        raise RetentionError("%s must be a whole number." % name)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or value != int(value):
            raise RetentionError("%s must be a whole number." % name)
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and len(text) <= 12:
            return int(text)
    raise RetentionError("%s must be a whole number." % name)


def validate(days=None, max_bytes=None, current=None) -> dict:
    """The settings these values describe, or `RetentionError`.

    An omitted value keeps `current` (or the default). Out-of-range values are
    refused rather than clamped: a retention period quietly changed from the one
    somebody typed is a surprise in a security record.
    """
    base = dict(defaults())
    if isinstance(current, dict):
        base.update({k: current[k] for k in ("days", "max_bytes") if k in current})
    result = dict(base)
    if days is not None:
        result["days"] = _whole(days, "Retention days")
    if max_bytes is not None:
        result["max_bytes"] = _whole(max_bytes, "Maximum log size")
    if not MIN_RETENTION_DAYS <= result["days"] <= MAX_RETENTION_DAYS:
        raise RetentionError("Retention must be between %d and %d days."
                             % (MIN_RETENTION_DAYS, MAX_RETENTION_DAYS))
    if not MIN_MAX_BYTES <= result["max_bytes"] <= MAX_MAX_BYTES:
        raise RetentionError("The maximum log size must be between %d KB and %d MB."
                             % (MIN_MAX_BYTES // 1024, MAX_MAX_BYTES // (1024 * 1024)))
    return result


def from_saved(value) -> dict:
    """Settings read from a file. Anything malformed becomes the defaults."""
    if not isinstance(value, dict):
        return defaults()
    try:
        return validate(value.get("days"), value.get("max_bytes"))
    except RetentionError:
        return defaults()


def entry_size(entry_json_length: int) -> int:
    return int(entry_json_length) + ENTRY_OVERHEAD_BYTES


def prune(entries, sizes, now, days, max_bytes):
    """(entries, sizes) after both limits, oldest first removed.

    `entries` are chronological dicts with an `at` timestamp (an entry without a
    usable one is treated as old, never as current), `sizes` the matching
    estimated bytes. Returns new lists; the inputs are not modified.
    """
    cutoff = now - days * SECONDS_PER_DAY
    start = 0
    n = len(entries)
    while start < n:
        at = entries[start].get("at") if isinstance(entries[start], dict) else None
        if isinstance(at, (int, float)) and not isinstance(at, bool) and at >= cutoff:
            break
        start += 1
    total = sum(sizes[start:])
    while start < n and (total > max_bytes or (n - start) > HARD_MAX_ENTRIES):
        total -= sizes[start]
        start += 1
    return list(entries[start:]), list(sizes[start:])
