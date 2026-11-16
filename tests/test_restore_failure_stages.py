"""A restore that fails at any stage leaves the old configuration, whole.

test_restore_atomicity proves the rollback by failing the last file. A rollback that works for
the last file and not for the first is not a rollback, so every file a restore writes or removes
is failed in turn, with the same two questions asked each time:

  * is every file on disk exactly as it was?
  * is every account, group, name and open session in memory exactly as it was?

Real application, isolated state, network fenced.
"""
import os
import unittest
from unittest import mock

import _support
from _support import ADMIN_PASSWORD, USER_PASSWORD

import luma_backup
import luma_users

from test_restore_atomicity import Base, A, D1, D2, PASS


class EveryStageTests(Base):
    def reset_to_before(self):
        """The state setUp built, put back exactly: files first, then every store re-read."""
        for name in self.names:
            path = os.path.join(self.m.DATA_DIR, name)
            if name in self.before:
                with open(path, "wb") as handle:
                    handle.write(self.before[name])
            elif os.path.exists(path):
                os.remove(path)
        self.store._load()
        self.aliases._load()
        self.groups.reload()

    def memory(self):
        accounts = sorted(a["username"] for a in self.store.list_accounts())
        groups = sorted(g["name"] for g in self.groups.all())
        return accounts, groups, dict(self.aliases.all())

    def test_failing_each_file_in_turn_leaves_files_and_memory_as_they_were(self):
        session = self.remote.sign_in("root", ADMIN_PASSWORD)
        exercised = []
        for name in self.names:
            with self.subTest(failing=name):
                memory_before = self.memory()
                files_before = self.files()
                with self.writing(fail=name):
                    response = self.restore()
                body = response.get_json()
                if name not in files_before and name not in (self.written_by_restore()):
                    # A file this restore would not write cannot be failed.
                    continue
                self.assertEqual(response.status_code in (200, 500), True)
                if response.status_code == 500:
                    exercised.append(name)
                    self.assertEqual(body["status"], "FAILED — ROLLED BACK", body)
                    self.assertEqual(self.files(), files_before,
                                     "a file differs after the restore failed at %s" % name)
                    self.assertEqual(self.memory(), memory_before)
                    self.assertEqual(session.get("/api/admin/users").status_code, 200,
                                     "a failed restore ended an open session")
                else:
                    # The file was not part of this restore: it ran to the end. Put the state back
                    # for the next stage.
                    self.reset_to_before()

    def test_the_loop_above_really_failed_several_different_stages(self):
        failed = []
        for name in self.names:
            with self.writing(fail=name):
                response = self.restore()
            if response.status_code == 500:
                failed.append(name)
                self.assertEqual(self.files(), self.before)
            else:
                self.reset_to_before()
        self.assertGreaterEqual(len(failed), 4, "only %s were failed; the loop proves little" % failed)
        self.assertIn("users.json", failed, "the accounts file is the one that matters most")

    def written_by_restore(self):
        return [section.filename for section in luma_backup.SECTIONS]

    def test_a_removal_followed_by_a_failure_puts_the_removed_file_back(self):
        # The backup holds no display groups, so the restore REMOVES display_groups.json, then a
        # later file fails: the removed file must come back, and so must the groups in memory.
        groups_path = os.path.join(self.m.DATA_DIR, "display_groups.json")
        self.assertTrue(os.path.exists(groups_path))
        self.groups.delete(next(iter(self.groups.all()))["id"])
        self.groups.delete(next(iter(self.groups.all()))["id"]) if self.groups.all() else None
        self.m._save_persisted_config()
        from_empty = self.local.post("/api/backup/export", json={"passphrase": PASS}).data
        self.groups.create("Added Later", [D1])
        before_files, before_memory = self.files(), self.memory()
        self.blob = from_empty
        with self.writing(fail="producer_state.json"):
            response = self.restore()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.files(), before_files)
        self.assertEqual(self.memory(), before_memory)

    def test_a_failure_after_the_accounts_were_removed_restores_them_and_their_sessions_stay(self):
        # A backup that held no accounts removes users.json; a later write fails.
        with self.store._lock:
            saved = dict(self.store._accounts)
        # The backup in self.blob holds accounts; build one that does not.
        path = os.path.join(self.m.DATA_DIR, "users.json")
        os.replace(path, path + ".keep")
        try:
            self.m._save_persisted_config()
            empty = self.local.post("/api/backup/export", json={"passphrase": PASS}).data
        finally:
            os.replace(path + ".keep", path)
        session = self.remote.sign_in("root", ADMIN_PASSWORD)
        before_files, before_memory = self.files(), self.memory()
        self.blob = empty
        with self.writing(fail="producer_state.json"):
            response = self.restore()
        self.assertEqual(response.status_code, 500, response.get_data(as_text=True))
        self.assertEqual(self.files(), before_files)
        self.assertEqual(self.memory(), before_memory)
        self.assertTrue(os.path.exists(path), "the accounts file was left removed")
        self.assertEqual(session.get("/api/admin/users").status_code, 200)

    def test_a_snapshot_that_cannot_be_taken_changes_nothing_at_all(self):
        before_files, before_memory = self.files(), self.memory()
        with mock.patch.object(luma_backup, "snapshot", side_effect=OSError("no room")):
            response = self.restore()
        body = response.get_json()
        self.assertEqual((response.status_code, body["reason"]), (500, "no_snapshot"))
        self.assertEqual(self.files(), before_files)
        self.assertEqual(self.memory(), before_memory)

    def test_a_wrong_passphrase_or_a_damaged_file_changes_nothing(self):
        before_files, before_memory = self.files(), self.memory()
        for mutate in (lambda b: b, lambda b: b[:-20], lambda b: b[:40] + bytes(len(b) - 40),
                       lambda b: b"not a backup"):
            blob = mutate(self.blob)
            wrong = PASS if blob is not self.blob else "the wrong passphrase"
            response = self.local.post("/api/backup/restore", content_type="multipart/form-data",
                                       data={"backup": (__import__("io").BytesIO(blob), "b"),
                                             "passphrase": wrong})
            self.assertEqual(response.status_code, 400, response.get_data(as_text=True)[:200])
            self.assertEqual(self.files(), before_files)
            self.assertEqual(self.memory(), before_memory)


class AfterTheApplyTests(Base):
    def test_a_failure_to_reload_says_restart_and_keeps_writes_refused(self):
        with mock.patch.object(self.m.REMOTE.store, "_load", side_effect=OSError("cannot read")):
            response = self.restore()
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("RESTART REQUIRED", body["status"])
        self.assertTrue(self.m._restart_required)
        refused = self.local.post("/api/admin/users", json={"username": "late", "password": "long enough pw",
                                                           "role": "user"})
        self.assertEqual(refused.status_code, 409)

    def test_a_failure_to_revoke_sessions_never_reports_the_restore_as_failed(self):
        with mock.patch.object(self.m.REMOTE.store, "revoke_sessions_outdated_by",
                               side_effect=RuntimeError("boom")):
            response = self.restore()
        # The files are restored; what failed is this process picking them up.
        self.assertEqual(response.status_code, 200)
        self.assertIn("RESTART REQUIRED", response.get_json()["status"])


if __name__ == "__main__":
    unittest.main()
