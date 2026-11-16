"""One device read serves every browser watching the same thing.

The User projection is derived from the host's cache and costs nothing to
serve. Anything that has to ask a device -- the cache refresh a page triggers, a
preview image -- does: ten clients would cost ten times as many device calls as
one, which is the thing this architecture is supposed to prevent.

So observation goes through here: a short time-to-live, and single-flight so
concurrent readers wait for one answer instead of each starting their own.
Ten browsers on one classroom display become one read.

Two rules from the project's own hard-won list are built in:

  * Discarding a stale answer is not the same as not asking. A sequence number
    protects correctness; single-flight is what stops a slow read having
    another stacked on top of it.

  * An operation that already touched the device resets the cadence. After a
    Show or a switch, the next read must see what just happened rather than a
    cached picture of what came before, so writers invalidate the key.

This is observation only. Nothing here is consulted to decide whether an
operation is allowed, and no routing path reads through it.
"""
from __future__ import annotations

import threading
import time

#: How long an observed answer may be reused. Long enough that a room full of
#: phones is one read, short enough that a display someone else changed is
#: right again almost immediately.
DEFAULT_TTL_SECONDS = 2.0


class StateCache:
    """Memoise expensive device reads briefly, with single-flight per key."""

    def __init__(self, ttl=DEFAULT_TTL_SECONDS):
        self._ttl = float(ttl)
        self._lock = threading.Lock()
        self._entries: dict = {}        # key -> (expires_at, value)
        self._inflight: dict = {}       # key -> threading.Event
        #: Bumped whenever a key is invalidated. A read that began before the
        #: bump is describing a world that no longer exists, so its answer is
        #: returned to the caller who asked for it and never stored.
        #:
        #: Two counters, not one. A per-key counter alone missed the case where
        #: everything is dropped at once while a read is in flight for a key
        #: that had never been invalidated individually: it had no entry, so
        #: its generation was unchanged and the stale answer was stored.
        self._generation: dict = {}
        self._epoch = 0
        self._stamped: dict = {}        # key -> when the last answer was stored (survives invalidation)
        self._workers: list = []        # background refresh threads, so a caller can wait on them
        self._stats = {"served": 0, "computed": 0, "joined": 0, "discarded": 0}

    def get(self, key, producer):
        """Return a fresh-enough value for `key`, computing it at most once.

        The first caller computes. Callers that arrive while that is running
        wait for it and share the result rather than starting a second read of
        the same device.
        """
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry[0] > now:
                self._stats["served"] += 1
                return entry[1]
            # Remembered before the read starts, and checked before its answer
            # is stored. Both halves: the key's own counter and the epoch that
            # a wholesale invalidation moves.
            generation = (self._epoch, self._generation.get(key, 0))
            waiting = self._inflight.get(key)
            if waiting is None:
                waiting = threading.Event()
                self._inflight[key] = waiting
                mine = True
            else:
                mine = False

        if not mine:
            # Somebody is already reading this. Wait for them rather than
            # asking the device the same question again.
            self._stats["joined"] += 1
            waiting.wait(timeout=30.0)
            with self._lock:
                entry = self._entries.get(key)
            if entry is not None:
                return entry[1]
            # The reader failed or timed out. Compute rather than return
            # nothing -- but do it without holding anybody else up.
            return producer()

        try:
            value = producer()
        except Exception:
            with self._lock:
                self._inflight.pop(key, None)
            waiting.set()
            raise

        with self._lock:
            if (self._epoch, self._generation.get(key, 0)) == generation:
                self._entries[key] = (time.time() + self._ttl, value)
                self._stats["computed"] += 1
            else:
                # Something changed the device while this read was in flight.
                # The value is still returned to whoever asked -- it is the
                # best answer that caller has -- but it must not be handed to
                # anybody else as though it were current.
                self._stats["discarded"] += 1
            self._inflight.pop(key, None)
        waiting.set()
        return value

    def get_bounded(self, key, producer, wait):
        """Refresh `key` without ever making a caller wait longer than `wait` seconds.

        Returns ``{"fresh": bool, "age_s": float or None, "refreshing": bool}``.

        `get` makes every caller wait for the whole device sweep, and a sweep that meets
        an unreachable unit takes as long as that unit's timeout -- so a page read could
        stall for seconds on a display nobody was looking at. Here the sweep runs on its
        own thread, once at a time, and a caller waits at most `wait` for it. If it is not
        done by then the caller carries on with what was last read, and is told how old
        that is. The thread finishes and stores its answer for the next caller.

        Observation only: nothing an operation is allowed to do depends on this.
        """
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry[0] > now:
                self._stats["served"] += 1
                return {"fresh": True, "age_s": self._age(key, now), "refreshing": False}
            generation = (self._epoch, self._generation.get(key, 0))
            waiting = self._inflight.get(key)
            if waiting is None:
                waiting = threading.Event()
                self._inflight[key] = waiting
                worker = threading.Thread(
                    target=self._refresh, args=(key, producer, waiting, generation),
                    name="luma-observe-" + str(key), daemon=True)
                self._workers.append(worker)
                worker.start()
            else:
                self._stats["joined"] += 1
        finished = waiting.wait(timeout=max(0.0, float(wait)))
        with self._lock:
            now = time.time()
            entry = self._entries.get(key)
            fresh = bool(finished and entry and entry[0] > now)
            return {"fresh": fresh, "age_s": self._age(key, now),
                    "refreshing": key in self._inflight}

    def _age(self, key, now):
        stamped = self._stamped.get(key)
        return None if stamped is None else round(max(0.0, now - stamped), 1)

    def _refresh(self, key, producer, waiting, generation):
        try:
            value = producer()
        except Exception:                       # a failed sweep leaves the last answer standing
            value = None
            failed = True
        else:
            failed = False
        with self._lock:
            if not failed and (self._epoch, self._generation.get(key, 0)) == generation:
                stamp = time.time()
                self._entries[key] = (stamp + self._ttl, value)
                self._stamped[key] = stamp
                self._stats["computed"] += 1
            elif not failed:
                self._stats["discarded"] += 1
            self._inflight.pop(key, None)
            self._workers = [w for w in self._workers if w.is_alive() and w is not threading.current_thread()]
        waiting.set()

    def wait_idle(self, timeout=5.0):
        """Wait for any background refresh to finish. For shutdown and for tests."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                running = [w for w in self._workers if w.is_alive()]
            if not running:
                return True
            for worker in running:
                worker.join(timeout=0.05)
        return False

    def invalidate(self, key=None):
        """Drop a cached answer, or all of them.

        Called after an operation that changed the thing being observed, so the
        next read reports what just happened instead of what came before.
        """
        with self._lock:
            if key is None:
                self._entries.clear()
                # One counter covers every key, including ones this cache has
                # never seen before.
                self._epoch += 1
            else:
                self._entries.pop(key, None)
                self._generation[key] = self._generation.get(key, 0) + 1

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)
