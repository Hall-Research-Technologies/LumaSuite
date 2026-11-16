"""Display Groups: a named collection of displays that receive the same source.

A Display Group is **not** a Video Wall. It has no geometry at all -- no rows, no
columns, no coordinates, no bezel compensation, no window placement and no image
splitting. Each member shows the whole of whatever source is routed to the group,
by the same ordinary full-screen A/V route a single display gets. The Video Wall
feature is a separate subsystem and is not used here.

Identity:
  * Membership is by stable device identity (a canonical MAC), never by address,
    so a display that changes address stays in its groups.
  * A group has an opaque id of its own, independent of its name and of who is in
    it, so renaming or re-populating a group never changes what a permission
    refers to.
  * A display may belong to any number of groups.

Execution: the application's routes use `run_parallel`, which dispatches the already
authorised members in one small bounded batch so the displays change together; each is
locked, routed and verified on its own. `run_sequential` (one member at a time) remains for
callers that need strict order. Either way a member that fails does not stop the rest, and
members already routed are not rolled back -- a group has no atomicity promise, and the
result says exactly which displays did and did not change.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from pathlib import Path

import luma_store

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_NAME_LENGTH = 60
MAX_MEMBERS = 256
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _clean_name(value) -> str:
    text = " ".join(str(value or "").split())
    return "".join(c for c in text if c.isprintable())[:MAX_NAME_LENGTH].strip()


def _clean_members(values, canonical):
    """(members, rejected): canonical MACs in first-seen order, no duplicates.

    Anything that is not a MAC -- an address, a hostname, a name -- is rejected
    rather than converted: membership is identity, and an address is not one.
    """
    members, rejected, seen = [], [], set()
    if not isinstance(values, (list, tuple)):
        return members, ["members must be a list"]
    for value in values:
        identity = canonical(value) if isinstance(value, str) else ""
        if not identity or not _MAC.match(identity):
            rejected.append(str(value)[:40])
            continue
        if identity not in seen:
            seen.add(identity)
            members.append(identity)
    return members, rejected


class GroupStore:
    """The persisted set of groups, safe to use from many request threads."""

    def __init__(self, path, canonical):
        self._path = Path(path)
        self._canonical = canonical
        self._lock = threading.RLock()
        self._groups: dict = {}
        self._load()

    # ---- persistence ---------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            self._groups = {}
            raw = luma_store.read_json(self._path, default=None)
            if raw is None:
                return
            entries = raw.get("groups") if isinstance(raw, dict) else None
            if not isinstance(entries, list):
                luma_store.quarantine(self._path)
                return
            for entry in entries:
                try:
                    gid = str(entry["id"]).strip().lower()
                    name = _clean_name(entry.get("name"))
                    members, _bad = _clean_members(entry.get("members") or [],
                                                   self._canonical)
                    if gid and name:
                        self._groups[gid] = {
                            "id": gid, "name": name, "members": members,
                            "created_at": float(entry.get("created_at") or 0),
                            "updated_at": float(entry.get("updated_at") or 0)}
                except Exception:
                    log.warning("[GROUPS] skipping an unreadable group record")

    def reload(self) -> None:
        self._load()

    def _persist(self) -> bool:
        return luma_store.atomic_write_json(self._path, {
            "version": SCHEMA_VERSION,
            "groups": [self._groups[g] for g in sorted(self._groups)]})

    # ---- reads ---------------------------------------------------------
    def all(self) -> list:
        """Every group, in a deterministic order, as independent copies."""
        with self._lock:
            return [dict(g, members=list(g["members"]))
                    for g in sorted(self._groups.values(),
                                    key=lambda g: (g["name"].lower(), g["id"]))]

    def get(self, group_id):
        with self._lock:
            g = self._groups.get(str(group_id or "").strip().lower())
            return None if g is None else dict(g, members=list(g["members"]))

    def groups_containing(self, identity) -> list:
        identity = self._canonical(identity)
        with self._lock:
            return [g["id"] for g in self._groups.values() if identity in g["members"]]

    def hold(self):
        """The store's lock, held by an operation that reads membership and then acts on it."""
        return self._lock

    def referenced_identities(self) -> dict:
        """identity -> sorted group names that contain it (for Clear Units)."""
        out: dict = {}
        with self._lock:
            for g in self._groups.values():
                for member in g["members"]:
                    out.setdefault(member, set()).add(g["name"])
        return {k: sorted(v) for k, v in out.items()}

    # ---- writes (each one read-modify-write under the lock) -------------
    def create(self, name, members):
        name = _clean_name(name)
        if not name:
            return {"ok": False, "error": "A group needs a name."}
        clean, rejected = _clean_members(members, self._canonical)
        if rejected:
            return {"ok": False, "error": "Some members are not displays that can be "
                                          "identified by hardware address."}
        if len(clean) > MAX_MEMBERS:
            return {"ok": False, "error": "That group has too many displays."}
        with self._lock:
            gid = "grp_" + secrets.token_hex(8)
            now = time.time()
            self._groups[gid] = {"id": gid, "name": name, "members": clean,
                                 "created_at": now, "updated_at": now}
            if not self._persist():
                self._groups.pop(gid, None)
                return {"ok": False, "error": "The group could not be saved."}
            return {"ok": True, "group": self.get(gid)}

    def update(self, group_id, name=None, members=None):
        gid = str(group_id or "").strip().lower()
        with self._lock:
            current = self._groups.get(gid)
            if current is None:
                return {"ok": False, "error": "No such group.", "missing": True}
            before = dict(current, members=list(current["members"]))
            if name is not None:
                cleaned = _clean_name(name)
                if not cleaned:
                    return {"ok": False, "error": "A group needs a name."}
                current["name"] = cleaned
            if members is not None:
                clean, rejected = _clean_members(members, self._canonical)
                if rejected:
                    self._groups[gid] = before
                    return {"ok": False,
                            "error": "Some members are not displays that can be "
                                     "identified by hardware address."}
                if len(clean) > MAX_MEMBERS:
                    self._groups[gid] = before
                    return {"ok": False, "error": "That group has too many displays."}
                current["members"] = clean
            current["updated_at"] = time.time()
            if not self._persist():
                self._groups[gid] = before
                return {"ok": False, "error": "The group could not be saved."}
            return {"ok": True, "group": self.get(gid), "before": before}

    def forget_members(self, identities) -> dict:
        """Remove cleared displays from every group, all-or-nothing.

        A group keeps its name and grants even when it ends up empty; what must
        never remain is a member that names a device that is no longer known.
        Returns {"ok", "changed", "before"}; `before` feeds restore().
        """
        gone = {self._canonical(i) for i in identities if i}
        with self._lock:
            before = {gid: list(g["members"]) for gid, g in self._groups.items()}
            changed = 0
            for g in self._groups.values():
                kept = [m for m in g["members"] if m not in gone]
                if len(kept) != len(g["members"]):
                    g["members"] = kept
                    g["updated_at"] = time.time()
                    changed += 1
            if changed and not self._persist():
                self.restore(before, persist=False)
                return {"ok": False, "changed": 0, "before": before}
            return {"ok": True, "changed": changed, "before": before}

    def restore(self, before, persist=True) -> None:
        with self._lock:
            for gid, members in before.items():
                if gid in self._groups:
                    self._groups[gid]["members"] = list(members)
            if persist:
                self._persist()

    def delete(self, group_id):
        gid = str(group_id or "").strip().lower()
        with self._lock:
            removed = self._groups.pop(gid, None)
            if removed is None:
                return {"ok": False, "error": "No such group.", "missing": True}
            if not self._persist():
                self._groups[gid] = removed
                return {"ok": False, "error": "The group could not be removed."}
            return {"ok": True, "group": removed}


def run_sequential(members, route_one) -> dict:
    """Route each member in order, one at a time, and report every outcome.

    `route_one(index, identity)` performs and verifies one display's route and
    returns a dict with at least `ok`. It is called strictly in order; the next
    call does not begin until the previous one has returned. An exception from
    one member is that member's failure and nothing more -- the rest still run,
    and nothing already done is undone.
    """
    results = []
    for index, identity in enumerate(members):
        try:
            outcome = route_one(index, identity)
            if not isinstance(outcome, dict) or "ok" not in outcome:
                outcome = {"ok": False, "reason": "no_result"}
        except Exception as exc:                  # a member's failure is only its own
            log.warning("[GROUPS] member %d raised %s", index, type(exc).__name__)
            outcome = {"ok": False, "reason": "error"}
        results.append(dict(outcome, index=index))
    routed = sum(1 for r in results if r.get("ok"))
    return {"ok": bool(results) and routed == len(results),
            "routed": routed, "total": len(results), "results": results}


#: How many destination writes one request may have in flight at once. Small and fixed: the
#: point is to make the displays change together, not to open a connection storm. A larger
#: selection is worked through in waves of this size.
DISPATCH_WORKERS = 6
MAX_DISPATCH_WORKERS = 8


def dispatch_workers() -> int:
    """The bound in force: `LUMA_ROUTE_WORKERS` if set, clamped to 1..MAX_DISPATCH_WORKERS."""
    import os
    try:
        wanted = int(os.environ.get("LUMA_ROUTE_WORKERS", DISPATCH_WORKERS))
    except (TypeError, ValueError):
        wanted = DISPATCH_WORKERS
    return max(1, min(MAX_DISPATCH_WORKERS, wanted))


def run_parallel(entries, route_one, workers=None, wrap=None) -> dict:
    """Route every entry in one bounded parallel batch and report every outcome.

    The counterpart of `run_sequential` for a request that names several displays:
    everything is resolved and authorised by the caller first, then the writes are
    dispatched together so the displays change at about the same moment instead of
    one after another. `route_one(index, entry)` has the same contract as for
    `run_sequential` and may block (it takes its own display lock). At most `workers`
    calls are in flight, so a large selection is a few waves, never a thread or request
    storm. `wrap(fn)` lets the caller bind per-thread context (a request) to each call;
    it is applied once per entry, on the calling thread.

    Results are in entry order whatever order the calls finished in. Each carries
    `t_start` and `t_end`: milliseconds since the batch began, so how far apart the
    writes began can be measured. No credential or address is recorded. An exception
    from one entry is that entry's failure and nothing more; nothing is undone.
    """
    from concurrent.futures import ThreadPoolExecutor

    entries = list(entries)
    bound = max(1, min(MAX_DISPATCH_WORKERS, int(workers or dispatch_workers())))
    began = time.monotonic()

    def ms():
        return round((time.monotonic() - began) * 1000.0, 1)

    def call(index, entry):
        started = ms()
        try:
            outcome = route_one(index, entry)
            if not isinstance(outcome, dict) or "ok" not in outcome:
                outcome = {"ok": False, "reason": "no_result"}
        except Exception as exc:                  # an entry's failure is only its own
            log.warning("[GROUPS] entry %d raised %s", index, type(exc).__name__)
            outcome = {"ok": False, "reason": "error"}
        return dict(outcome, index=index, t_start=started, t_end=ms())

    results = [None] * len(entries)
    if entries:
        with ThreadPoolExecutor(max_workers=min(bound, len(entries)),
                                thread_name_prefix="luma-route") as pool:
            futures = []
            for index, entry in enumerate(entries):
                task = (lambda i=index, e=entry: call(i, e))
                futures.append(pool.submit(wrap(task) if wrap else task))
            for index, future in enumerate(futures):
                results[index] = future.result()
    routed = sum(1 for r in results if r.get("ok"))
    return {"ok": bool(results) and routed == len(results),
            "routed": routed, "total": len(results), "results": results,
            "timing": timing(results, bound)}


def timing(results, workers=None) -> dict:
    """First/last start and end (ms since the batch began) over results carrying them."""
    starts = [r["t_start"] for r in results if isinstance(r, dict) and "t_start" in r]
    ends = [r["t_end"] for r in results if isinstance(r, dict) and "t_end" in r]
    if not starts or not ends:
        return {"workers": workers, "first_start_ms": None, "last_start_ms": None,
                "first_end_ms": None, "last_end_ms": None, "start_spread_ms": None}
    return {"workers": workers, "first_start_ms": min(starts), "last_start_ms": max(starts),
            "first_end_ms": min(ends), "last_end_ms": max(ends),
            "start_spread_ms": round(max(starts) - min(starts), 1)}
