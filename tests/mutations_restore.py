"""Mutations for the restore's inventory merge, the inventory save, lock order and the read rule.

Entry format is the harness's:

    (id, file, anchor, replacement, what it breaks, suites that must notice)

An anchor must match exactly once, in an LF file. Each entry breaks ONE critical branch of the
rc4 fix on purpose and names the focused tests that must fail. A mutation that can deadlock the
restore (a lock taken twice) is pointed at the static tests and the watchdog test, which fail with
a bounded wait instead of hanging the harness.

`CANARIES` are not part of the sweep. `--canary` runs them to validate the harness itself: the
first must be caught (the harness can see a failure in these suites) and the second changes
nothing and must survive (the harness does not report a pass as a catch).
"""
import os
import sys

PY = sys.executable


def py(file, *selectors):
    return ("python:" + file + (":" + ",".join(selectors) if selectors else ""),
            [PY, os.path.join("tests", file)] + list(selectors))


RACE = "test_restore_cache_race.py"
ORDER = "test_restore_lock_order.py"
STORE = "test_store_read_errors.py"

CRITICAL = py(RACE, "RestoreCacheMergeIsOneCriticalSection")
FAILED_SAVE = py(RACE, "AFailedInventorySaveIsNotReportedAsSuccess")
PERSIST = py(RACE, "PersistReportsWhatHappened")
ADJACENT = py(RACE, "AdjacentInventoryFilePaths")
READERS = py(RACE, "AnExportOrInspectDuringARestoreCannotMakeItFail")
STATIC = py(ORDER, "StaticLockOrderTests")
DYNAMIC = py(ORDER, "DynamicLockOrderTests")
PROGRESS = py(ORDER, "NoDeadlockUnderAMixedWorkloadTests.test_restores_saves_ingests_and_disk_merges_all_make_progress")
STORE_READS = py(STORE, "LumaStoreReads")
OTHER_STORES = py(STORE, "OtherStoresShareTheRule")

MERGE_HEAD = ("        rows = [(key, row) for key, row in cache_units.items() if isinstance(row, dict)]\n"
              "        merged, matched, unmatched = luma_backup.merge_devices(payload, [row for _, row in rows])\n"
              "        try:\n")

RESTORE_MUTATIONS = [
    # ---- the merge is one critical section over live rows ---------------------------------------
    ("rcache.merge-outside-the-lock", "lumaserver.py", MERGE_HEAD,
     "        rows = [(key, row) for key, row in cache_units.items() if isinstance(row, dict)]\n"
     "    merged, matched, unmatched = luma_backup.merge_devices(payload, [row for _, row in rows])\n"
     "    with cache_lock:\n"
     "        try:\n",
     "the merge runs with the lock released and its stale copy overwrites a scan that landed meanwhile",
     [CRITICAL]),
    ("rcache.row-replaced-not-filled", "lumaserver.py",
     "                        row[field] = value\n                        learned.append((key, field, previous, value))",
     "                        cache_units[key] = dict(row, **{field: value})\n"
     "                        learned.append((key, field, previous, value))",
     "a copy is swapped in for the live row (a poller's reference goes stale) and a removed row is brought back",
     [CRITICAL]),
    ("rcache.merge-does-not-fill-missing", "lumaserver.py",
     "                    if previous is _ABSENT or previous != value:",
     "                    if False:",
     "the backup's remembered details are never applied",
     [CRITICAL, FAILED_SAVE]),
    ("rcache.backup-beats-live", "luma_backup.py",
     "            if unit.get(field) in (None, \"\"):\n                unit[field] = value",
     "            if True:\n                unit[field] = value",
     "the backup overwrites what a scan has just learned",
     [CRITICAL]),
    ("rcache.merged-paired-with-wrong-rows", "lumaserver.py",
     "        rows = [(key, row) for key, row in cache_units.items() if isinstance(row, dict)]\n"
     "        merged, matched, unmatched = luma_backup.merge_devices(payload, [row for _, row in rows])\n"
     "        try:\n            for (key, row), new_row in zip(rows, merged):",
     "        rows = [(key, row) for key, row in cache_units.items() if isinstance(row, dict)]\n"
     "        merged, matched, unmatched = luma_backup.merge_devices(payload, [row for _, row in rows])\n"
     "        try:\n            for (key, row), new_row in zip(rows, merged[::-1]):",
     "merged rows are paired with the wrong live rows",
     [CRITICAL, FAILED_SAVE]),
    # ---- a failed save is said, and what was learned is taken back ------------------------------
    ("rcache.save-failure-ignored", "lumaserver.py",
     "        if learned and not inventory_saved:",
     "        if False and learned and not inventory_saved:",
     "a restore whose inventory could not be saved reports a clean RESTORED",
     [FAILED_SAVE]),
    ("rcache.no-warning-text", "lumaserver.py",
     "    if not devices_saved:\n        body[\"warning\"]",
     "    if False:\n        body[\"warning\"]",
     "the operator is not told the device details were not saved",
     [FAILED_SAVE]),
    ("rcache.saved-flag-always-true", "lumaserver.py",
     "\"awaiting_scan\": unmatched, \"saved\": devices_saved}",
     "\"awaiting_scan\": unmatched, \"saved\": True}",
     "the response claims the inventory was saved when it was not",
     [FAILED_SAVE]),
    ("rcache.no-rollback-of-learned", "lumaserver.py",
     "            devices_undone = _restore_unmerge_cache(learned)",
     "            devices_undone = 0",
     "what the merge learned stays in memory although it was never saved",
     [FAILED_SAVE]),
    ("rcache.rollback-reverts-concurrent-write", "lumaserver.py",
     "            if not isinstance(row, dict) or row.get(field, _ABSENT) != value:\n                continue",
     "            if not isinstance(row, dict):\n                continue",
     "the rollback overwrites a value a scan changed after the merge",
     [FAILED_SAVE]),
    ("rcache.rollback-leaves-absent-field", "lumaserver.py",
     "            if previous is _ABSENT:\n                row.pop(field, None)",
     "            if previous is _ABSENT:\n                pass",
     "the rollback leaves a field that was never there before the restore",
     [FAILED_SAVE]),
    ("rcache.partial-merge-not-undone", "lumaserver.py",
     "            _restore_unmerge_cache(learned, _held=True)\n            raise",
     "            raise",
     "a merge that fails partway leaves the inventory half changed",
     [FAILED_SAVE]),
    # ---- the save reports what happened and survives what is on disk ----------------------------
    ("persist.failure-reported-as-success", "lumaserver.py",
     "        logging.warning(f\"[persist] failed {e}\")\n        return False",
     "        logging.warning(f\"[persist] failed {e}\")\n        return True",
     "a save that failed says it succeeded",
     [PERSIST, FAILED_SAVE]),
    ("persist.dropped-write-is-failure", "lumaserver.py",
     "skipped: the cache was cleared while this write was pending\")\n                return True",
     "skipped: the cache was cleared while this write was pending\")\n                return False",
     "a write dropped because the inventory was cleared is called a failure",
     [PERSIST]),
    ("persist.units-not-list-accepted", "lumaserver.py",
     "    if isinstance(data, dict) and isinstance(data.get(\"units\"), list):",
     "    if isinstance(data, dict) and \"units\" in data:",
     "a file whose units value is not a list blocks every later save",
     [ADJACENT]),
    ("persist.bad-file-not-kept", "lumaserver.py",
     "                luma_store.quarantine(CACHE_FILE)\n            for u in (disk_rows or []):",
     "                pass\n            for u in (disk_rows or []):",
     "an unusable inventory file is overwritten without being kept for inspection",
     [ADJACENT]),
    ("persist.unhashable-address-accepted", "lumaserver.py",
     "                if isinstance(u, dict) and isinstance(u.get(\"ip\"), str):",
     "                if isinstance(u, dict) and \"ip\" in u:",
     "a disk row whose address is not text breaks every save",
     [ADJACENT]),
    # ---- lock order -----------------------------------------------------------------------------
    ("rlock.restore-holds-lock-across-merge-call", "lumaserver.py",
     "        matched, unmatched, learned = _restore_merge_cache(payload)",
     "        with cache_lock:\n            matched, unmatched, learned = _restore_merge_cache(payload)",
     "the restore takes the non-reentrant cache lock twice: a self-deadlock",
     [STATIC, PROGRESS]),
    ("rlock.store-order-reversed", "lumaserver.py",
     "        with REMOTE.store._lock, REMOTE.aliases._lock, REMOTE.groups._lock:\n            return _restore_locked(payload, summary)",
     "        with REMOTE.groups._lock, REMOTE.aliases._lock, REMOTE.store._lock:\n            return _restore_locked(payload, summary)",
     "the restore takes the stores in the opposite order from every other path",
     [STATIC, DYNAMIC]),
    ("rlock.file-lock-inside-cache-lock", "lumaserver.py",
     "        with luma_store.lock_for(CACHE_FILE):\n            existing = {}",
     "        with cache_lock, luma_store.lock_for(CACHE_FILE):\n            existing = {}",
     "the save nests the inventory file's lock inside cache_lock",
     [DYNAMIC]),
    ("rlock.save-inside-cache-section", "lumaserver.py",
     "        inventory_saved = _persist_units_cache()",
     "        with cache_lock:\n            inventory_saved = _persist_units_cache()",
     "the restore saves the inventory while holding cache_lock: a self-deadlock",
     [STATIC, PROGRESS]),
    # ---- an export or inspection never makes a restore fail -----------------------------------
    ("rread.config-read-without-lock", "luma_backup.py",
     "    with luma_store.lock_for(path):\n        return path.read_text(encoding=\"utf-8\")",
     "    return path.read_text(encoding=\"utf-8\")",
     "an export or the confirmation screen reads a file while the restore replaces it",
     [READERS]),
    # ---- a read error is not a corrupt file ---------------------------------------------------
    ("store.no-retry", "luma_store.py",
     "READ_ATTEMPTS = 3", "READ_ATTEMPTS = 1",
     "a momentary read error is final",
     [STORE_READS]),
    ("store.quarantine-on-read-error", "luma_store.py",
     "        if strict:\n            raise\n        return default",
     "        quarantine(path)\n        if strict:\n            raise\n        return default",
     "a good file that could not be read is renamed away",
     [STORE_READS]),
    ("store.update-writes-over-unread", "luma_store.py",
     "            current = _read_unlocked(path, default, strict=True)",
     "            current = _read_unlocked(path, default, strict=False)",
     "a read-modify-write runs on a default it never read from the file",
     [STORE_READS]),
    ("store.parse-error-not-quarantined", "luma_store.py",
     "        log.error(\"[STORE] %s is unreadable (%s); quarantining\", path.name, type(exc).__name__)\n        quarantine(path)",
     "        log.error(\"[STORE] %s is unreadable (%s); quarantining\", path.name, type(exc).__name__)",
     "a file whose contents are corrupt is left in place to be re-read every time",
     [STORE_READS]),
    ("store.users-quarantines-read-error", "luma_users.py",
     "            except luma_store.ReadFailed as exc:",
     "            except ZeroDivisionError as exc:",
     "the accounts file is renamed away on a momentary read error",
     [OTHER_STORES]),
    ("store.aliases-quarantines-read-error", "luma_aliases.py",
     "            except luma_store.ReadFailed as exc:",
     "            except ZeroDivisionError as exc:",
     "the device-names file is renamed away on a momentary read error",
     [OTHER_STORES]),
    ("store.port-quarantines-read-error", "luma_portcontrol.py",
     "                except OSError as exc:\n                    # Could not be read;",
     "                except ZeroDivisionError as exc:\n                    # Could not be read;",
     "the port settings are renamed away on a momentary read error",
     [OTHER_STORES]),
]

CANARIES = [
    # Must be CAUGHT: an obvious break in code these suites cover.
    ("canary.persist-always-fails", "lumaserver.py",
     "        logging.debug(f\"[persist] wrote {len(rows)} units -> {CACHE_FILE}\")\n        return True",
     "        logging.debug(f\"[persist] wrote {len(rows)} units -> {CACHE_FILE}\")\n        return False",
     "canary: a successful save reports failure",
     [PERSIST]),
    # Must SURVIVE: the replacement is the anchor with a comment, so behaviour is identical.
    ("canary.no-op", "lumaserver.py",
     "_ABSENT = object()\n",
     "_ABSENT = object()  # no-op canary\n",
     "canary: changes nothing, so no suite may fail",
     [PERSIST, CRITICAL]),
]
