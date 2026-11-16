"""Serialising operations that target the same physical equipment.

Two teachers pressing Show on the same display at the same moment is now an
ordinary event rather than a hypothetical one, so the question is what the
second one gets. The answer this branch takes is the one that suits AV
control: **the last successful authorised operation wins.** Nobody reserves a
room, nobody is locked out because a browser was closed, and a display is never
left unusable by an operator who walked away.

What that is *not* is a licence for two requests to interleave inside one
transaction. The device work already has its own verification and rollback, and
those guarantees only hold if one transaction at a time is running against a
given piece of equipment. So an operation takes a short lock on the resources it
touches, does its work, and releases. Concurrency is preserved between rooms;
it is removed only where it would corrupt something.

Three rules, each of which exists because of a failure mode the project has
already met:

  * **Narrow, not global.** One lock per display (decoder). Routing
    in Room 101 must not wait on Room 102, or a site becomes single-file.

  * **Deterministic ordering.** An operation touching several resources takes
    them in sorted order, always. Two operations grabbing the same pair in
    opposite orders is the textbook deadlock.

  * **Bounded waiting.** A lock is never held across a request boundary and
    never waited on forever. A device that stops answering mid-transaction must
    not take the room's next operation down with it.

The locks protect *operations*, never reads. Observation goes through the state
cache and takes nothing.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time

log = logging.getLogger(__name__)

#: How long an operation will wait for equipment another operation is using.
#: Long enough to cover a real transaction -- a route writes the stream and reads
#: it back -- short enough that a wedged operation surfaces
#: as a refusal rather than a hung browser.
DEFAULT_TIMEOUT_SECONDS = 45.0


class ResourceBusy(Exception):
    """Another operation is using this equipment and did not finish in time."""

    def __init__(self, resource):
        super().__init__(f"{resource} is busy")
        self.resource = resource


class ResourceLocks:
    """Per-resource mutual exclusion, acquired in a deterministic order."""

    def __init__(self, timeout=DEFAULT_TIMEOUT_SECONDS):
        self._timeout = float(timeout)
        self._guard = threading.Lock()
        self._locks: dict = {}
        self._held: dict = {}
        self._stats = {"acquired": 0, "waited": 0, "timed_out": 0}

    def _lock_for(self, key):
        with self._guard:
            existing = self._locks.get(key)
            if existing is None:
                existing = threading.Lock()
                self._locks[key] = existing
            return existing

    @contextlib.contextmanager
    def hold(self, *resources, timeout=None):
        """Hold every named resource for the duration of the block.

        Sorted before acquiring, so two operations naming the same pair in
        opposite orders cannot each hold half of what the other needs. Anything
        already acquired is released if a later one cannot be had, so a timeout
        leaves nothing behind.
        """
        wanted = sorted({str(r) for r in resources if r})
        if not wanted:
            yield
            return

        deadline = time.monotonic() + (self._timeout if timeout is None
                                       else float(timeout))
        acquired = []
        try:
            for key in wanted:
                lock = self._lock_for(key)
                # Try without blocking first, purely so contention can be
                # counted. `waited` existed and was never incremented, so it
                # read zero after a measurably contended acquisition -- which
                # states there was no contention rather than saying nothing.
                if not lock.acquire(blocking=False):
                    with self._guard:
                        self._stats["waited"] += 1
                    remaining = max(0.0, deadline - time.monotonic())
                    if not lock.acquire(timeout=remaining):
                        with self._guard:
                            self._stats["timed_out"] += 1
                        raise ResourceBusy(key)
                acquired.append((key, lock))
                with self._guard:
                    self._stats["acquired"] += 1
                    self._held[key] = time.time()
            yield
        finally:
            # Release in reverse, and release everything even if the body
            # raised. A lock kept because an operation failed is the failure
            # that takes the room down rather than one request.
            for key, lock in reversed(acquired):
                with self._guard:
                    self._held.pop(key, None)
                try:
                    lock.release()
                except RuntimeError:
                    log.warning("[LOCKS] %s was already released", key)

    # ---- resource names ----------------------------------------------
    #
    # Named by stable identity, never by address: the same decoder reached at a
    # new address is the same piece of equipment and must take the same lock.

    @staticmethod
    def decoder(identity) -> str:
        return "decoder:" + str(identity or "").lower()

    def busy(self) -> list:
        with self._guard:
            return sorted(self._held)

    def stats(self) -> dict:
        with self._guard:
            return dict(self._stats)
