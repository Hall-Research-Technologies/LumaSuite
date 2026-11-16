"""A restore must not lose, overwrite, resurrect or mis-merge the device inventory.

The defect (found in the rc3 closeout, fixed in rc4): after a restore wrote its files,
`_restore_locked` copied `cache_units` under `cache_lock`, RELEASED the lock, merged the backup's
learned device facts into the copy, then took the lock again and wrote every merged row back
by its address. Anything a scan, a matrix refresh or a liveness check wrote in between was
overwritten by the stale copy, a unit that moved address in that window came back at its old
address as well (one identity under two addresses), and a unit removed in that window was
brought back. The write to `units_cache.json` that followed also swallowed a failure, so a
restore could say RESTORED when the inventory had not been saved.

These tests reproduce the window deterministically. The merge is the stretch of the restore
that used to run with the lock released, so the hook below starts a "concurrent writer" from
inside the merge. On the old code the writer finishes during the merge and the restore then
writes its stale copy over it. On the fixed code the merge runs inside the one critical
section, so the writer has to wait for it and lands afterwards; the assertions are the same
either way: the writer's change is in the final inventory.

Real application, isolated state, network fenced. The inventory file is the real one (the shared
fixture stubs the save out, so these tests put the real function back).
"""
import copy
import json
import os
import threading
import time
import unittest
from unittest import mock

import _support  # noqa: F401  (installs the fence and the scratch data directory first)

from test_restore_atomicity import Base, D1, D2

import luma_backup
import luma_projection
import luma_store
import lumaserver as _server

REAL_PERSIST = _server._persist_units_cache
MOVED_TO = "198.51.100.199"


def identity_of(unit):
    return luma_projection.unit_identity(unit)


class InventoryBase(Base):
    """Base's fixture, with the real inventory save and the real inventory file."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(self.m, "_persist_units_cache", REAL_PERSIST)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._file_before = None
        if os.path.exists(self.m.CACHE_FILE):
            with open(self.m.CACHE_FILE, "rb") as handle:
                self._file_before = handle.read()
        self.addCleanup(self._put_the_inventory_file_back)
        REAL_PERSIST()
        self.assertTrue(os.path.exists(self.m.CACHE_FILE), "precondition: the inventory can be saved")

    def _put_the_inventory_file_back(self):
        for leftover in (self.m.CACHE_FILE + ".corrupt",):
            if os.path.exists(leftover):
                os.remove(leftover)
        if self._file_before is None:
            if os.path.exists(self.m.CACHE_FILE):
                os.remove(self.m.CACHE_FILE)
        else:
            with open(self.m.CACHE_FILE, "wb") as handle:
                handle.write(self._file_before)

    def ip_of(self, identity):
        with self.m.cache_lock:
            return next(ip for ip, u in self.m.cache_units.items() if identity_of(u) == identity)

    def holders(self, identity):
        with self.m.cache_lock:
            return sorted(ip for ip, u in self.m.cache_units.items() if identity_of(u) == identity)

    def forget_learned_hostname(self, identity):
        """The scan has not learned D's hostname yet; the backup has. Returns the backup's value."""
        ip = self.ip_of(identity)
        with self.m.cache_lock:
            wanted = self.m.cache_units[ip].pop("hostname")
        return wanted

    def restore_with_a_writer_in_the_merge(self, write):
        """Run a restore; `write()` is started on its own thread from inside the merge.

        Returns (response, writer_finished_during_the_merge). The writer is always joined.
        """
        real = luma_backup.merge_devices
        state = {"during": None, "thread": None}

        def merge(payload, units):
            def body():
                try:
                    write()
                except BaseException as exc:      # a writer that failed must not look like a pass
                    state["error"] = exc
            thread = threading.Thread(target=body, name="inventory-writer", daemon=True)
            state["thread"] = thread
            thread.start()
            thread.join(0.5)
            state["during"] = not thread.is_alive()
            return real(payload, units)

        with mock.patch.object(luma_backup, "merge_devices", merge):
            response = self.restore()
        state["thread"].join(10)
        self.assertFalse(state["thread"].is_alive(), "the writer never finished: deadlock")
        self.assertNotIn("error", state, state.get("error"))
        return response, state["during"]


class RestoreCacheMergeIsOneCriticalSection(InventoryBase):
    """The two promoted closeout scenarios, plus the ones the same defect implies."""

    def scan_lands(self):
        """What a scan that finished in the gap does: refresh one unit, move another."""
        d1_ip, d2_ip = self.ip_of(D1), self.ip_of(D2)

        def write():
            with self.m.cache_lock:
                self.m.cache_units[d1_ip]["status"] = "FRESH-FROM-SCAN"
                moved = copy.deepcopy(self.m.cache_units.pop(d2_ip))
                moved["ip"] = MOVED_TO
                self.m.cache_units[MOVED_TO] = moved
        return d1_ip, write

    def test_a_scan_that_lands_during_the_merge_is_not_overwritten(self):
        d1_ip, write = self.scan_lands()
        response, _during = self.restore_with_a_writer_in_the_merge(write)
        self.assertEqual(response.get_json()["status"], "RESTORED", response.get_json())
        with self.m.cache_lock:
            status = self.m.cache_units[d1_ip].get("status")
        self.assertEqual(status, "FRESH-FROM-SCAN",
                         "restore wrote its stale copy of the row over a newer scan result")

    def test_a_unit_that_moved_during_the_merge_is_not_held_under_two_addresses(self):
        _d1_ip, write = self.scan_lands()
        response, _during = self.restore_with_a_writer_in_the_merge(write)
        self.assertEqual(response.get_json()["status"], "RESTORED", response.get_json())
        self.assertEqual(self.holders(D2), [MOVED_TO],
                         "the identity came back at its old address as well")

    def test_a_unit_removed_during_the_merge_is_not_brought_back(self):
        d1_ip = self.ip_of(D1)

        def write():
            with self.m.cache_lock:
                self.m.cache_generation += 1
                self.m.cache_units.pop(d1_ip)
            REAL_PERSIST()                        # the removal path saves the inventory itself

        response, _during = self.restore_with_a_writer_in_the_merge(write)
        self.assertEqual(response.get_json()["status"], "RESTORED", response.get_json())
        self.assertEqual(self.holders(D1), [], "a removed unit was resurrected by the restore")
        with open(self.m.CACHE_FILE, encoding="utf-8") as handle:
            on_disk = [identity_of(u) for u in json.load(handle)]
        self.assertNotIn(D1, on_disk, "a removed unit was written back to the inventory file")

    def test_the_inventory_is_one_row_per_identity_and_keyed_by_its_own_address(self):
        _d1_ip, write = self.scan_lands()
        self.restore_with_a_writer_in_the_merge(write)
        with self.m.cache_lock:
            rows = list(self.m.cache_units.items())
        identities = [identity_of(u) for _ip, u in rows if identity_of(u)]
        self.assertEqual(len(identities), len(set(identities)), "an identity is held twice")
        for ip, unit in rows:
            self.assertEqual(unit.get("ip"), ip, "a row is filed under an address that is not its own")

    def test_the_restore_holds_the_lock_across_the_merge(self):
        """The writer in the merge must be WAITING for the lock, not finished before it."""
        _d1_ip, write = self.scan_lands()
        _response, during = self.restore_with_a_writer_in_the_merge(write)
        self.assertFalse(during, "a writer ran to completion inside the merge: the lock was not held")

    def test_learned_facts_are_filled_into_the_live_row_not_a_copy(self):
        """A poller holds a reference to its row and mutates it in place; a swapped-in copy
        would silently discard everything it writes afterwards."""
        wanted = self.forget_learned_hostname(D1)
        ip = self.ip_of(D1)
        with self.m.cache_lock:
            row = self.m.cache_units[ip]
        self.assertEqual(self.restore().get_json()["status"], "RESTORED")
        with self.m.cache_lock:
            self.assertIs(self.m.cache_units[ip], row, "the restore replaced the live row")
        self.assertEqual(row.get("hostname"), wanted, "the backup's learned fact was not applied")
        row["status"] = "polled-after-restore"
        with self.m.cache_lock:
            self.assertEqual(self.m.cache_units[ip]["status"], "polled-after-restore")

    def test_live_data_wins_over_the_backup(self):
        ip = self.ip_of(D1)
        with self.m.cache_lock:
            self.m.cache_units[ip]["hostname"] = "Learned By A Scan"
        self.assertEqual(self.restore().get_json()["status"], "RESTORED")
        with self.m.cache_lock:
            self.assertEqual(self.m.cache_units[ip]["hostname"], "Learned By A Scan")


class ConcurrentWritersCannotLoseState(InventoryBase):
    """The same defect without the hook: writers hammer the inventory while restores run."""

    def test_no_writer_update_is_lost_across_repeated_restores(self):
        ips = [self.ip_of(D1), self.ip_of(D2)]
        stop = threading.Event()
        written = {ip: 0 for ip in ips}
        errors = []

        def writer(ip):
            n = 0
            try:
                while not stop.is_set():
                    n += 1
                    with self.m.cache_lock:
                        row = self.m.cache_units.get(ip)
                        if row is not None:
                            row["seq"] = n
                            written[ip] = n
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(ip,), daemon=True) for ip in ips]
        for t in threads:
            t.start()
        try:
            for _ in range(12):
                for ip in ips:                     # give each restore something to learn
                    with self.m.cache_lock:
                        self.m.cache_units[ip].pop("hostname", None)
                self.assertEqual(self.restore().get_json()["status"], "RESTORED")
        finally:
            stop.set()
            for t in threads:
                t.join(10)
        self.assertEqual(errors, [])
        self.assertFalse(any(t.is_alive() for t in threads))
        with self.m.cache_lock:
            for ip in ips:
                self.assertEqual(self.m.cache_units[ip]["seq"], written[ip],
                                 "a restore reverted a newer write to the inventory")

    def test_the_inventory_file_is_never_torn_while_restores_and_saves_overlap(self):
        stop = threading.Event()
        problems = []

        def saver():
            while not stop.is_set():
                REAL_PERSIST()

        def reader():
            while not stop.is_set():
                try:
                    with open(self.m.CACHE_FILE, encoding="utf-8") as handle:
                        data = json.load(handle)
                    if not isinstance(data, list) or not all(isinstance(u, dict) for u in data):
                        problems.append("not a list of rows")
                    ips = [u.get("ip") for u in data]
                    if len(ips) != len(set(ips)):
                        problems.append("an address appears twice")
                except (FileNotFoundError, PermissionError):
                    pass     # this reader raced the atomic replace on Windows; not a torn read
                except (ValueError, OSError) as exc:
                    problems.append("%s: %s" % (type(exc).__name__, exc))

        threads = [threading.Thread(target=f, daemon=True) for f in (saver, saver, reader)]
        for t in threads:
            t.start()
        try:
            for _ in range(8):
                self.assertEqual(self.restore().get_json()["status"], "RESTORED")
        finally:
            stop.set()
            for t in threads:
                t.join(10)
        self.assertEqual(problems[:5], [], "the inventory file was torn")


class AnExportOrInspectDuringARestoreCannotMakeItFail(InventoryBase):
    """Found by the mixed-workload run: a read of a configuration file made WITHOUT its lock
    (an export, or the confirmation screen's account comparison) overlapping the restore's atomic
    replace of the same file. On Windows a replace is refused while another handle is open, so the
    restore's write failed and the whole restore rolled back, for nothing the operator did wrong.
    The readers now take the file's own lock, so they wait for the replace instead of blocking it."""

    def test_restores_succeed_while_exports_and_inspections_run(self):
        import io
        stop = threading.Event()
        errors = []

        def reader(path, make_args):
            client = self.m.APP.test_client()
            try:
                while not stop.is_set():
                    response = client.post(path, **make_args())
                    if response.status_code != 200:
                        errors.append((path, response.status_code))
            except BaseException as exc:
                errors.append((path, repr(exc)))

        inspect_args = lambda: dict(content_type="multipart/form-data", data={
            "backup": (io.BytesIO(self.blob), "b.lumabak"), "passphrase": "a good backup passphrase"})
        export_args = lambda: dict(json={"passphrase": "a good backup passphrase"})
        threads = [threading.Thread(target=reader, args=("/api/backup/inspect", inspect_args), daemon=True),
                   threading.Thread(target=reader, args=("/api/backup/export", export_args), daemon=True)]
        for t in threads:
            t.start()
        failures = []
        try:
            for _ in range(10):
                body = self.restore().get_json()
                if body.get("status") != "RESTORED":
                    failures.append(body.get("status") or body)
        finally:
            stop.set()
            for t in threads:
                t.join(30)
        self.assertEqual(failures, [], "a restore failed because a reader overlapped its write")
        # A read that overlapped a restore is a 409 only if it is a write; reads must all answer.
        self.assertEqual(errors, [], "an export or inspection failed while a restore ran")


    def test_a_reader_holding_the_accounts_file_makes_the_restore_wait_not_fail(self):
        """The deterministic form of the above. A reader sits INSIDE its read of users.json (handle open, as a
        slow disk or a scanner would hold it) while the restore reaches that file. With the readers under the
        file lock the restore's write waits for the reader and then succeeds; without it, the open handle makes
        the atomic replace fail on Windows and the restore rolls back."""
        import io
        import pathlib
        users = os.path.join(self.m.DATA_DIR, "users.json")
        reading, release = threading.Event(), threading.Event()
        real = pathlib.Path.read_text

        def slow_read_text(path, *args, **kwargs):
            if threading.current_thread().name == "slow-reader" and os.path.abspath(str(path)) == os.path.abspath(users):
                with open(path, "rb"):                 # the handle stays open while the reader "reads"
                    reading.set()
                    release.wait(30)
                    return real(path, *args, **kwargs)
            return real(path, *args, **kwargs)

        reader_result = {}

        def reader():
            client = self.m.APP.test_client()
            response = client.post("/api/backup/inspect", content_type="multipart/form-data", data={
                "backup": (io.BytesIO(self.blob), "b.lumabak"), "passphrase": "a good backup passphrase"})
            reader_result["status"] = response.status_code

        restore_result = {}

        def restorer():
            restore_result["body"] = self.restore().get_json()

        with mock.patch.object(pathlib.Path, "read_text", slow_read_text):
            t_read = threading.Thread(target=reader, name="slow-reader", daemon=True)
            t_read.start()
            self.assertTrue(reading.wait(30), "precondition: the reader never reached the accounts file")
            t_restore = threading.Thread(target=restorer, name="restorer", daemon=True)
            t_restore.start()
            time.sleep(2.5)            # long enough for the restore to reach and try to write users.json
            release.set()
            t_read.join(30)
            t_restore.join(60)
        self.assertFalse(t_read.is_alive() or t_restore.is_alive(), "deadlock: a reader and a restore waiting on each other")
        self.assertEqual(reader_result.get("status"), 200)
        self.assertEqual(restore_result["body"].get("status"), "RESTORED",
                         "a reader that overlapped the restore's write made the restore fail: %s" % restore_result["body"])


class AFailedInventorySaveIsNotReportedAsSuccess(InventoryBase):

    def failing_inventory_write(self, before_failing=None):
        real = luma_store.atomic_write_json

        def write(path, payload, *args, **kwargs):
            if os.path.abspath(str(path)) == os.path.abspath(self.m.CACHE_FILE):
                if before_failing:
                    before_failing()
                return False
            return real(path, payload, *args, **kwargs)
        return mock.patch.object(luma_store, "atomic_write_json", write)

    def test_the_response_says_the_device_details_were_not_saved(self):
        self.forget_learned_hostname(D1)
        with self.failing_inventory_write():
            body = self.restore().get_json()
        self.assertTrue(body["ok"], "the files were restored and must not be reported as failed")
        self.assertIs(body["devices"].get("saved"), False,
                      "a restore whose inventory save failed said nothing about it: %s" % body)
        self.assertIn("could not be saved", body.get("warning", ""))

    def test_what_was_learned_is_taken_back_so_memory_and_disk_agree(self):
        ip = self.ip_of(D1)
        self.forget_learned_hostname(D1)
        with self.m.cache_lock:
            before = copy.deepcopy(self.m.cache_units)
        with self.failing_inventory_write():
            self.restore()
        with self.m.cache_lock:
            self.assertEqual(self.m.cache_units, before,
                             "the inventory was left changed by a restore that could not save it")
            self.assertNotIn("hostname", self.m.cache_units[ip])

    def test_a_field_a_scan_changed_meanwhile_is_not_reverted_by_the_rollback(self):
        ip = self.ip_of(D1)
        self.forget_learned_hostname(D1)

        def scan_changes_it():
            with self.m.cache_lock:
                self.m.cache_units[ip]["hostname"] = "Fresh Name From A Scan"
        with self.failing_inventory_write(before_failing=scan_changes_it):
            self.restore()
        with self.m.cache_lock:
            self.assertEqual(self.m.cache_units[ip]["hostname"], "Fresh Name From A Scan")

    def test_the_configuration_is_still_restored_when_only_the_inventory_save_failed(self):
        self.forget_learned_hostname(D1)
        with self.failing_inventory_write():
            self.restore()
        self.assertIn("alex", self.usernames(), "the configuration restore was undone for a cache failure")
        self.assertNotIn("zed", self.usernames())

    def test_a_successful_save_says_so_and_the_file_holds_what_was_learned(self):
        wanted = self.forget_learned_hostname(D1)
        body = self.restore().get_json()
        self.assertIs(body["devices"].get("saved"), True)
        self.assertNotIn("warning", body)
        with open(self.m.CACHE_FILE, encoding="utf-8") as handle:
            rows = {identity_of(u): u for u in json.load(handle)}
        self.assertEqual(rows[D1].get("hostname"), wanted)

    def test_a_merge_that_fails_partway_leaves_the_inventory_as_it_was(self):
        """Two rows learn something; the second assignment raises. The first is undone."""
        ips = [self.ip_of(D1), self.ip_of(D2)]
        for identity in (D1, D2):
            self.forget_learned_hostname(identity)

        class Breaks(dict):
            armed = True

            def __setitem__(self, key, value):
                if Breaks.armed and key == "hostname":
                    raise RuntimeError("simulated failure while filling a row")
                super().__setitem__(key, value)

        with self.m.cache_lock:
            self.m.cache_units[ips[1]] = Breaks(self.m.cache_units[ips[1]])
            before = {ip: dict(row) for ip, row in self.m.cache_units.items()}
        try:
            body = self.restore().get_json()
        finally:
            Breaks.armed = False
        self.assertEqual(body["status"], "RESTORED — RESTART REQUIRED", body)
        with self.m.cache_lock:
            after = {ip: dict(row) for ip, row in self.m.cache_units.items()}
        self.assertEqual(after, before, "a half-applied merge was left in the inventory")


class PersistReportsWhatHappened(InventoryBase):

    def test_true_on_success(self):
        self.assertTrue(REAL_PERSIST())

    def test_false_when_the_write_fails(self):
        with mock.patch.object(luma_store, "atomic_write_json", return_value=False):
            self.assertFalse(REAL_PERSIST())

    def test_true_when_the_write_is_dropped_because_the_inventory_was_cleared(self):
        real_read = luma_store.read_json

        def read_then_clear(path, default=None):
            data = real_read(path, default=default)
            with self.m.cache_lock:
                self.m.cache_generation += 1
            return data
        with mock.patch.object(luma_store, "read_json", read_then_clear):
            self.assertTrue(REAL_PERSIST(), "a dropped write is not a failure")


class AdjacentInventoryFilePaths(InventoryBase):
    """The paths beside the restore, each examined rather than assumed to match another product."""

    def write_raw(self, text):
        with open(self.m.CACHE_FILE, "w", encoding="utf-8") as handle:
            handle.write(text)

    def quarantined(self):
        return os.path.exists(self.m.CACHE_FILE + ".corrupt")

    def test_a_malformed_file_is_quarantined_and_memory_is_untouched(self):
        self.write_raw("{ this is not json")
        with self.m.cache_lock:
            before = copy.deepcopy(self.m.cache_units)
        self.assertEqual(self.m._merge_disk_into_memory(), 0)
        self.assertTrue(self.quarantined())
        with self.m.cache_lock:
            self.assertEqual(self.m.cache_units, before)
        REAL_PERSIST()
        with open(self.m.CACHE_FILE, encoding="utf-8") as handle:
            self.assertIsInstance(json.load(handle), list, "the next save must write a fresh inventory")

    def test_a_file_that_is_not_an_inventory_does_not_block_every_later_save(self):
        """Valid JSON of the wrong shape used to make every save raise, for ever."""
        for shape in ('{"units": 5}', '"text"', "42", '{"units": {"a": 1}}', '{"other": []}'):
            with self.subTest(shape=shape):
                self.write_raw(shape)
                if os.path.exists(self.m.CACHE_FILE + ".corrupt"):
                    os.remove(self.m.CACHE_FILE + ".corrupt")
                REAL_PERSIST()
                self.assertTrue(self.quarantined(), "the unusable file was overwritten, not kept for inspection")
                with open(self.m.CACHE_FILE, encoding="utf-8") as handle:
                    rows = json.load(handle)
                self.assertIsInstance(rows, list, "a save did not replace a file of shape %s" % shape)
                self.assertEqual(len(rows), len(self.m.cache_units))

    def test_rows_that_are_not_dicts_or_have_no_usable_address_are_ignored_on_disk(self):
        ip = self.ip_of(D1)
        self.write_raw(json.dumps([1, "x", None, {"ip": ["unhashable"]}, {"ip": ip, "extra": "kept"}]))
        REAL_PERSIST()
        with open(self.m.CACHE_FILE, encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertTrue(all(isinstance(u, dict) and "ip" in u for u in written),
                        "the save did not replace a file with rows it cannot use")
        rows = {u["ip"]: u for u in written}
        self.assertEqual(rows[ip].get("extra"), "kept", "a disk-only field was not preserved")

    def test_the_two_accepted_shapes(self):
        ip = self.ip_of(D1)
        self.write_raw(json.dumps({"units": [{"ip": ip, "extra": "kept"}]}))
        REAL_PERSIST()
        with open(self.m.CACHE_FILE, encoding="utf-8") as handle:
            rows = {u["ip"]: u for u in json.load(handle)}
        self.assertEqual(rows[ip].get("extra"), "kept")

    def test_a_missing_file_is_created(self):
        os.remove(self.m.CACHE_FILE)
        self.assertEqual(self.m._merge_disk_into_memory(), 0)
        REAL_PERSIST()
        self.assertTrue(os.path.exists(self.m.CACHE_FILE))

    def test_an_empty_file_is_treated_as_unreadable_and_recovered(self):
        self.write_raw("")
        self.assertEqual(self.m._merge_disk_into_memory(), 0)
        REAL_PERSIST()
        with open(self.m.CACHE_FILE, encoding="utf-8") as handle:
            self.assertIsInstance(json.load(handle), list)

    def test_an_empty_inventory_list_adds_nothing(self):
        self.write_raw("[]")
        self.assertEqual(self.m._merge_disk_into_memory(), 0)

    def test_a_non_list_units_value_cannot_break_the_disk_merge(self):
        for text in ('{"units": 5}', '{"units": "abc"}', '{"units": null}'):
            with self.subTest(text=text):
                self.write_raw(text)
                with self.m.cache_lock:
                    before = copy.deepcopy(self.m.cache_units)
                self.assertEqual(self.m._merge_disk_into_memory(), 0)
                with self.m.cache_lock:
                    self.assertEqual(self.m.cache_units, before)

    def test_a_failed_save_leaves_the_previous_file_intact(self):
        with open(self.m.CACHE_FILE, "rb") as handle:
            before = handle.read()
        with mock.patch.object(luma_store, "atomic_write_json", return_value=False):
            REAL_PERSIST()
        with open(self.m.CACHE_FILE, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_a_disk_row_for_a_unit_held_under_another_address_is_not_added(self):
        d2_ip = self.ip_of(D2)
        with self.m.cache_lock:
            moved = self.m.cache_units.pop(d2_ip)
            moved["ip"] = MOVED_TO
            self.m.cache_units[MOVED_TO] = moved
        self.write_raw(json.dumps([dict(moved, ip=d2_ip)]))
        self.m._merge_disk_into_memory()
        self.assertEqual(self.holders(D2), [MOVED_TO])

    def test_concurrent_saves_and_disk_merges_never_corrupt_the_file_or_memory(self):
        stop = threading.Event()
        errors = []

        def loop(fn):
            try:
                while not stop.is_set():
                    fn()
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=loop, args=(f,), daemon=True)
                   for f in (REAL_PERSIST, REAL_PERSIST, self.m._merge_disk_into_memory)]
        for t in threads:
            t.start()
        time.sleep(1.5)
        stop.set()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertFalse(self.quarantined(), "a concurrent read saw a torn file and quarantined it")
        with open(self.m.CACHE_FILE, encoding="utf-8") as handle:
            self.assertIsInstance(json.load(handle), list)


if __name__ == "__main__":
    unittest.main()
