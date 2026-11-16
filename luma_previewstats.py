"""Preview traffic counters, for measuring how much a page asks of the devices.

Off unless `LUMA_PREVIEW_STATS=1` is set in the environment, and silent either
way: nothing is logged, nothing is written to disk, and when off every call is a
single boolean test. The numbers are exposed only through a local-only endpoint
(`/api/preview_stats`), so a restricted or remote account can neither read nor
reset them.

Two things are counted, because they answer different questions:

  * `requests`  -- how often a page asked this host for a picture, by kind and
                   by whether the answer was an image, and whether `fresh` was set.
  * `device`    -- how often this host actually went to a device. The gap between
                   the two is the shared cache doing its job.
"""
import os
import threading

_lock = threading.Lock()
_counts = {}
_MAX_KEYS = 64                      # a bounded table: a flood of odd keys costs nothing


def enabled():
    return os.environ.get("LUMA_PREVIEW_STATS", "") == "1"


def note(group, key):
    """Count one event. A no-op unless enabled."""
    if not enabled():
        return
    label = "%s.%s" % (group, key)
    with _lock:
        if label in _counts or len(_counts) < _MAX_KEYS:
            _counts[label] = _counts.get(label, 0) + 1


def snapshot():
    with _lock:
        return dict(sorted(_counts.items()))


def reset():
    with _lock:
        _counts.clear()
