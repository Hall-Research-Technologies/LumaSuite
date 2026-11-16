"""Lock order and liveness around the restore's inventory merge.

The established order, which the restore fix must not disturb:

    restore lock -> account store -> device names -> display groups -> cache_lock

`cache_lock` is held only for in-memory work. The inventory FILE's own lock is taken after
`cache_lock` has been released (the save snapshots first), never inside it and never the other
way round for this code, so the two cannot be held in opposite orders by the restore, the
save, a scan or a poller. Three tests hold that:

  * statically, from the source of the functions involved;
  * dynamically, by wrapping every one of those locks and recording, per thread, which lock was
    taken while which other was held, across a workload of restores, saves, scan ingests and
    disk merges running at once;
  * by a watchdog: the same workload with bounded joins, so a deadlock fails with the stacks of
    every thread instead of hanging the suite.

Real application, isolated state, network fenced.
"""
import ast
import collections
import copy
import os
import sys
import threading
import time
import traceback
import unittest
from unittest import mock

import _support  # noqa: F401  (fence and scratch data directory)

from test_restore_atomicity import Base, D1, D2

import luma_store
import luma_projection
import lumaserver as _server

REAL_PERSIST = _server._persist_units_cache
SOURCE = open(os.path.join(os.path.dirname(os.path.abspath(_server.__file__)), "lumaserver.py"),
              encoding="utf-8").read()
TREE = ast.parse(SOURCE)


def function(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("no function " + name)


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def with_cache_lock_bodies(fn):
    """Every `with cache_lock:` block in the function, as the list of its statements."""
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.With) and any(dotted(i.context_expr) == "cache_lock" for i in node.items):
            found.append(node)
    return found


def calls_in(nodes):
    names = set()
    for stmt in nodes:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                names.add(dotted(node.func))
    return names


class StaticLockOrderTests(unittest.TestCase):

    def test_the_merge_does_only_in_memory_work_while_holding_cache_lock(self):
        allowed = {"list", "cache_units.items", "isinstance", "luma_backup.merge_devices", "zip",
                   "new_row.items", "row.get", "learned.append", "_restore_unmerge_cache", "cache_units.get",
                   "row.pop", "reversed", "undo"}
        for name in ("_restore_merge_cache", "_restore_unmerge_cache"):
            blocks = with_cache_lock_bodies(function(name))
            self.assertTrue(blocks, name + " must hold cache_lock")
            used = calls_in(blocks)
            self.assertLessEqual(used, allowed, "%s does more than memory work under cache_lock: %s"
                                 % (name, sorted(used - allowed)))

    def test_the_save_takes_its_snapshot_under_cache_lock_and_nothing_else(self):
        blocks = with_cache_lock_bodies(function("_persist_units_cache"))
        self.assertEqual(len(blocks), 1)
        self.assertLessEqual(calls_in(blocks), {"dict", "cache_units.items"})
        # and no file lock or write inside it
        text = ast.dump(blocks[0])
        for forbidden in ("lock_for", "atomic_write_json", "read_json"):
            self.assertNotIn(forbidden, text)

    def test_nothing_that_takes_another_lock_runs_inside_the_restores_cache_section(self):
        # cache_lock is a plain (non-reentrant) lock: the restore must not call the merge, the save
        # or the file layer while holding it.
        used = calls_in(with_cache_lock_bodies(function("_restore_locked")))
        for forbidden in ("_persist_units_cache", "_restore_merge_cache", "_restore_unmerge_cache",
                          "luma_store.atomic_write_json", "luma_store.read_json", "luma_store.lock_for"):
            self.assertNotIn(forbidden, used)

    def test_the_restore_takes_the_stores_before_it_ever_reaches_cache_lock(self):
        fn = function("api_backup_restore")
        for node in ast.walk(fn):
            if isinstance(node, ast.With):
                names = [dotted(i.context_expr) for i in node.items]
                if "REMOTE.store._lock" in names:
                    self.assertEqual(names, ["REMOTE.store._lock", "REMOTE.aliases._lock", "REMOTE.groups._lock"])
                    return
        self.fail("the restore no longer holds the stores' locks across the transaction")

    def test_the_save_never_nests_the_file_lock_inside_cache_lock_or_the_reverse(self):
        fn = function("_persist_units_cache")
        # `with cache_lock` and `with luma_store.lock_for(...)` are siblings, not nested.
        outer = [n for n in ast.walk(fn) if isinstance(n, ast.With)]
        for node in outer:
            names = [dotted(i.context_expr.func) if isinstance(i.context_expr, ast.Call)
                     else dotted(i.context_expr) for i in node.items]
            inner_withs = [n for s in node.body for n in ast.walk(s) if isinstance(n, ast.With)]
            inner = [dotted(i.context_expr.func) if isinstance(i.context_expr, ast.Call)
                     else dotted(i.context_expr) for w in inner_withs for i in w.items]
            if "cache_lock" in names:
                self.assertNotIn("luma_store.lock_for", inner)
            if "luma_store.lock_for" in names:
                self.assertNotIn("cache_lock", inner)


class Tracked:
    """A lock that records which other tracked lock the thread already held when it was taken."""

    held = threading.local()
    edges = collections.defaultdict(set)      # (held, taken) -> {thread names}
    guard = threading.Lock()

    def __init__(self, name, inner):
        self.name, self.inner = name, inner

    @classmethod
    def reset(cls):
        with cls.guard:
            cls.edges.clear()

    def _stack(self):
        stack = getattr(Tracked.held, "stack", None)
        if stack is None:
            stack = Tracked.held.stack = []
        return stack

    def acquire(self, blocking=True, timeout=-1):
        stack = self._stack()
        if self.name not in stack:
            with Tracked.guard:
                for other in set(stack):
                    Tracked.edges[(other, self.name)].add(threading.current_thread().name)
        got = self.inner.acquire(blocking, timeout)
        if got:
            stack.append(self.name)
        return got

    def release(self):
        stack = self._stack()
        for i in range(len(stack) - 1, -1, -1):
            if stack[i] == self.name:
                del stack[i]
                break
        self.inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def locked(self):
        return self.inner.locked()


class WorkloadBase(Base):

    def setUp(self):
        super().setUp()
        for name, replacement in (("_persist_units_cache", REAL_PERSIST),):
            patcher = mock.patch.object(self.m, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self._file_before = None
        if os.path.exists(self.m.CACHE_FILE):
            with open(self.m.CACHE_FILE, "rb") as handle:
                self._file_before = handle.read()
        self.addCleanup(self._put_file_back)
        Tracked.reset()
        file_key = os.path.abspath(self.m.CACHE_FILE)
        luma_store.lock_for(self.m.CACHE_FILE)
        remote = self.m.REMOTE
        for target, attribute, name in ((self.m, "cache_lock", "cache"),
                                        (remote.store, "_lock", "store"),
                                        (remote.aliases, "_lock", "aliases"),
                                        (remote.groups, "_lock", "groups")):
            patcher = mock.patch.object(target, attribute, Tracked(name, getattr(target, attribute)))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(luma_store._locks, {file_key: Tracked("file", luma_store._locks[file_key])})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _put_file_back(self):
        if self._file_before is None:
            if os.path.exists(self.m.CACHE_FILE):
                os.remove(self.m.CACHE_FILE)
        else:
            with open(self.m.CACHE_FILE, "wb") as handle:
                handle.write(self._file_before)

    # ---- the workload ----------------------------------------------------------------------
    def ingest_loop(self, stop, counter):
        d1 = copy.deepcopy(self.unit_of(D1))
        n = 0
        while not stop.is_set():
            n += 1
            info = dict(d1, status="scan-%d" % n)
            with self.m.cache_lock:
                index = self.m._mac_index_of_cache()
                self.m._ingest_scanned_unit(info["ip"], info, index)
            counter["ingest"] += 1

    def unit_of(self, identity):
        with self.m.cache_lock:
            return next(copy.deepcopy(u) for u in self.m.cache_units.values()
                        if luma_projection.unit_identity(u) == identity)

    def run_mixed_workload(self, seconds, restorers=2, bound=90, extra=()):
        stop = threading.Event()
        counter = collections.Counter()
        errors = []

        def guarded(name, fn, *args):
            def body():
                try:
                    fn(*args)
                except BaseException:
                    errors.append((name, traceback.format_exc()))
            return threading.Thread(target=body, name=name, daemon=True)

        def restore_loop(stop):
            client = self.m.APP.test_client()
            from io import BytesIO
            while not stop.is_set():
                response = client.post("/api/backup/restore", content_type="multipart/form-data",
                                       data={"backup": (BytesIO(self.blob), "b.lumabak"),
                                             "passphrase": "a good backup passphrase"})
                body = response.get_json()
                if response.status_code == 200 and body.get("status") == "RESTORED":
                    counter["restored"] += 1
                elif response.status_code == 409 and body.get("reason") == "restore_in_progress":
                    counter["busy"] += 1
                else:
                    counter["unexpected"] += 1
                    errors.append(("restore", (response.status_code, body)))

        def save_loop(stop):
            while not stop.is_set():
                if REAL_PERSIST() is False:
                    counter["save_failed"] += 1
                counter["saved"] += 1

        def merge_loop(stop):
            while not stop.is_set():
                self.m._merge_disk_into_memory()
                self.m._load_units_snapshot()
                counter["merged"] += 1

        threads = ([guarded("restore-%d" % i, restore_loop, stop) for i in range(restorers)]
                   + [guarded("save-%d" % i, save_loop, stop) for i in range(2)]
                   + [guarded("ingest", self.ingest_loop, stop, counter),
                      guarded("merge", merge_loop, stop)]
                   + [guarded(name, fn, stop) for name, fn in extra])
        for t in threads:
            t.start()
        time.sleep(seconds)
        stop.set()
        deadline = time.monotonic() + bound
        for t in threads:
            t.join(max(0.1, deadline - time.monotonic()))
        stuck = [t.name for t in threads if t.is_alive()]
        if stuck:
            frames = sys._current_frames()
            dump = "\n".join("--- %s\n%s" % (t.name, "".join(traceback.format_stack(frames[t.ident])))
                             for t in threads if t.is_alive() and t.ident in frames)
            self.fail("DEADLOCK or stall: %s did not finish within the bound\n%s" % (stuck, dump))
        return counter, errors


class DynamicLockOrderTests(WorkloadBase):

    def test_no_pair_of_locks_is_ever_taken_in_both_orders_and_cache_lock_is_innermost(self):
        counter, errors = self.run_mixed_workload(4)
        self.assertEqual(errors, [])
        self.assertGreater(counter["restored"], 0, "precondition: at least one restore completed")
        edges = {k: v for k, v in Tracked.edges.items()}
        for (first, second) in list(edges):
            if (second, first) in edges:
                self.fail("locks %s and %s were taken in both orders: %s / %s"
                          % (first, second, sorted(edges[(first, second)]), sorted(edges[(second, first)])))
        # cache_lock is the innermost lock: nothing is acquired while it is held. The file
        # lock is taken after it has been released, and never the other way round either.
        under_cache = sorted(t for (h, t) in edges if h == "cache")
        self.assertEqual(under_cache, [], "a lock was taken while cache_lock was held: %s" % under_cache)
        self.assertNotIn(("file", "cache"), edges, "cache_lock was taken inside the inventory file lock")
        # and the order the restore establishes is the documented one
        allowed = {("store", "aliases"), ("store", "groups"), ("store", "cache"), ("store", "file"),
                   ("aliases", "groups"), ("aliases", "cache"), ("aliases", "file"),
                   ("groups", "cache"), ("groups", "file")}
        self.assertLessEqual(set(edges), allowed, "unexpected lock nesting: %s" % sorted(set(edges) - allowed))


class NoDeadlockUnderAMixedWorkloadTests(WorkloadBase):

    def test_restores_saves_ingests_and_disk_merges_all_make_progress(self):
        counter, errors = self.run_mixed_workload(8)
        self.assertEqual(errors, [], errors)
        self.assertEqual(counter["unexpected"], 0)
        for what in ("restored", "saved", "ingest", "merged"):
            self.assertGreater(counter[what], 0, "%s made no progress: %s" % (what, dict(counter)))
        self.assertEqual(counter["save_failed"], 0)

    def test_the_inventory_is_consistent_afterwards(self):
        self.run_mixed_workload(5)
        with self.m.cache_lock:
            rows = list(self.m.cache_units.items())
        identities = [luma_projection.unit_identity(u) for _ip, u in rows if luma_projection.unit_identity(u)]
        self.assertEqual(len(identities), len(set(identities)))
        for ip, unit in rows:
            self.assertEqual(unit.get("ip"), ip)
        self.assertIn(D1, identities)
        self.assertIn(D2, identities)

    def test_the_watchdog_itself_reports_a_stall_with_stacks(self):
        """A deadlock must fail with the stacks of the stuck threads, not hang the suite."""
        never = threading.Lock()
        never.acquire()
        release = threading.Event()

        def stuck(_stop):
            never.acquire()                      # waits until the test lets go
        try:
            with self.assertRaises(AssertionError) as caught:
                self.run_mixed_workload(0.5, restorers=1, bound=1.5, extra=[("deliberately-stuck", stuck)])
        finally:
            never.release()
            release.set()
        message = str(caught.exception)
        self.assertIn("DEADLOCK or stall", message)
        self.assertIn("deliberately-stuck", message)


if __name__ == "__main__":
    unittest.main()
