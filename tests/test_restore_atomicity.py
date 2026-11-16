"""A restore must leave the old configuration or the new one, never a mixture.

Reading the restore path turned up four ways to end up with a hybrid, each
reproduced here against the real endpoint before it was fixed:

  1. The rollback only put back files that existed beforehand, so a file the
     restore created stayed -- and the snapshot folder is reused from one restore
     to the next, so a stale file from an earlier restore could be put back.
  2. The rollback copied over live files in place, so an interruption in the
     middle of it truncated the very file it was recovering.
  3. Nothing stopped a normal write landing in the middle. The account, name and
     group stores keep their contents in memory and rewrite the whole file on any
     change, so a change between "users.json restored" and "reload from disk"
     wrote the old accounts straight back over the restored ones.
  4. If reloading failed after the files were applied, the next ordinary save
     wrote the process's old state over the restore.

Real application, isolated state, network fenced.
"""
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, SOURCE_A,
                      RealApp, RealRemote, identity)

import luma_backup
import luma_users

A = identity(SOURCE_A)
D1, D2 = identity(DISPLAY_1), identity(DISPLAY_2)
PASS = "a good backup passphrase"


class Base(unittest.TestCase):

    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.store = self.remote.store
        self.aliases = self.remote.remote.aliases
        self.groups = self.remote.remote.groups
        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER,
                          luma_users.Permissions([A], [D1], []))
        self.aliases.set_alias(A, "Main Stage")
        self.group = self.groups.create("Lobby", [D1, D2])["group"]
        self.local = self.m.APP.test_client()
        self.names = [section.filename for section in luma_backup.SECTIONS]

        # Every file a restore can write is put back afterwards, whatever it did.
        self._originals = {}
        for name in self.names:
            path = os.path.join(self.m.DATA_DIR, name)
            if os.path.exists(path):
                self._originals[name] = open(path, "rb").read()
        self.addCleanup(self._put_back)

        self.snapshot_dir = tempfile.mkdtemp(prefix="luma-restore-snap-")
        self.addCleanup(shutil.rmtree, self.snapshot_dir, True)
        patcher = mock.patch.object(self.m, "BACKUP_SNAPSHOT_DIR", self.snapshot_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reset_flags)

        self.m._save_persisted_config()
        self.blob = self.local.post("/api/backup/export", json={"passphrase": PASS}).data
        # The state the restore will have to replace.
        self.store.delete(next(a["id"] for a in self.store.list_accounts()
                               if a["username"] == "alex"))
        self.store.create("zed", USER_PASSWORD, luma_users.ROLE_USER)
        self.aliases.set_alias(A, "Replaced Name")
        self.groups.delete(self.group["id"])
        self.groups.create("OnlyBefore", [D1])
        self.before = self.files()

    def _put_back(self):
        for name in self.names:
            path = os.path.join(self.m.DATA_DIR, name)
            if name in self._originals:
                with open(path, "wb") as handle:
                    handle.write(self._originals[name])
            elif os.path.exists(path):
                os.remove(path)

    def _reset_flags(self):
        self.m._restore_in_progress = False
        self.m._restart_required = False

    def files(self):
        out = {}
        for name in self.names:
            path = os.path.join(self.m.DATA_DIR, name)
            if os.path.exists(path):
                out[name] = open(path, "rb").read()
        return out

    def restore(self):
        return self.local.post(
            "/api/backup/restore", content_type="multipart/form-data",
            data={"backup": (io.BytesIO(self.blob), "b.lumabak"), "passphrase": PASS})

    def writing(self, fail=None, before=None):
        """Wrap the restore's writer: fail on one file, run a hook before another."""
        real = self.m._restore_write

        def write(path, content):
            name = os.path.basename(str(path))
            if before and name == before[0]:
                before[1]()
            if name == fail:
                raise OSError("the disk went away")
            return real(path, content)
        return mock.patch.object(self.m, "_restore_write", write)

    def usernames(self):
        with open(os.path.join(self.m.DATA_DIR, "users.json"), encoding="utf-8") as handle:
            return {u["username"] for u in json.load(handle)["users"]}


class ARollbackRestoresTheWholeStateTests(Base):

    def test_a_failure_partway_leaves_every_file_as_it_was(self):
        with self.writing(fail="producer_state.json"):
            response = self.restore()
        self.assertEqual(response.get_json()["status"], "FAILED — ROLLED BACK")
        self.assertEqual(self.files(), self.before,
                         "some files were restored and some were not")

    def test_a_file_the_restore_created_is_removed_by_the_rollback(self):
        walls = os.path.join(self.m.DATA_DIR, "video_walls.json")
        if os.path.exists(walls):
            os.remove(walls)
        self.before = self.files()
        with self.writing(fail="producer_state.json"):
            response = self.restore()
        self.assertEqual(response.get_json()["status"], "FAILED — ROLLED BACK")
        self.assertFalse(os.path.exists(walls),
                         "a half-applied restore left a file that was never there")
        self.assertEqual(self.files(), self.before)

    def test_a_stale_snapshot_from_an_earlier_restore_is_never_resurrected(self):
        walls = os.path.join(self.m.DATA_DIR, "video_walls.json")
        if os.path.exists(walls):
            os.remove(walls)
        self.before = self.files()
        with open(os.path.join(self.snapshot_dir, "video_walls.json"), "w") as handle:
            handle.write('{"walls": ["stale"]}')
        with self.writing(fail="producer_state.json"):
            self.restore()
        self.assertFalse(os.path.exists(walls))
        self.assertEqual(self.files(), self.before)
        # The folder is also what a person recovers from by hand, so it must hold
        # this restore's files and nothing an earlier one left.
        self.assertFalse(os.path.exists(os.path.join(self.snapshot_dir, "video_walls.json")),
                         "the snapshot folder still holds an earlier restore's file")


class ARollbackThatFailsIsSaidLoudlyTests(Base):

    def test_it_is_reported_as_incomplete_and_the_evidence_is_kept(self):
        def broken(src, dst):
            raise OSError("the volume is read-only")

        with self.writing(fail="producer_state.json"), \
                mock.patch.object(luma_backup, "_atomic_copy", broken, create=True):
            response = self.restore()
        body = response.get_json()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(body["status"], "FAILED — ROLLBACK INCOMPLETE", body)
        self.assertTrue(body["rollback_failures"])
        self.assertFalse(body.get("ok"))
        self.assertTrue(body.get("recovery_snapshot"),
                        "the operator is not told where the way back is")
        self.assertTrue(os.path.exists(os.path.join(body["recovery_snapshot"], "users.json")),
                        "the snapshot was discarded while the rollback was incomplete")

    def test_the_rollback_never_truncates_a_live_file_in_place(self):
        real = getattr(luma_backup, "_atomic_copy", None)
        self.assertIsNotNone(real, "the rollback has no atomic copy")
        seen = []

        def watching(src, dst):
            seen.append(os.path.basename(str(dst)))
            return real(src, dst)

        with self.writing(fail="producer_state.json"), \
                mock.patch.object(luma_backup, "_atomic_copy", watching):
            self.restore()
        self.assertIn("users.json", seen)


class ANormalWriteCannotLandInTheMiddleTests(Base):

    def run_with_concurrent(self, work, after_file):
        errors = []

        def target():
            try:
                work()
            except Exception as exc:                       # pragma: no cover
                errors.append(exc)

        thread = threading.Thread(target=target, daemon=True)

        def start_it():
            thread.start()
            thread.join(0.5)              # long enough to have written, if it can

        with self.writing(before=(after_file, start_it)):
            response = self.restore()
        thread.join(5)
        self.assertFalse(errors, errors)
        return response

    def test_an_account_change_during_the_restore_does_not_undo_it(self):
        response = self.run_with_concurrent(
            lambda: self.store.create("latecomer", USER_PASSWORD, luma_users.ROLE_USER),
            "device_aliases.json")
        self.assertEqual(response.get_json()["status"], "RESTORED", response.get_json())
        names = self.usernames()
        self.assertIn("alex", names, "the restored accounts were overwritten")
        self.assertNotIn("zed", names, "the old accounts were written back over the restore")

    def test_an_alias_change_during_the_restore_does_not_undo_it(self):
        response = self.run_with_concurrent(
            lambda: self.aliases.set_alias(identity(DISPLAY_2), "Late Name"),
            "display_groups.json")
        self.assertEqual(response.get_json()["status"], "RESTORED")
        with open(os.path.join(self.m.DATA_DIR, "device_aliases.json")) as handle:
            text = handle.read()
        self.assertIn("Main Stage", text)
        self.assertNotIn("Replaced Name", text)

    def test_a_group_change_during_the_restore_does_not_undo_it(self):
        response = self.run_with_concurrent(
            lambda: self.groups.create("Late", [D2]), "producer_config.json")
        self.assertEqual(response.get_json()["status"], "RESTORED")
        with open(os.path.join(self.m.DATA_DIR, "display_groups.json")) as handle:
            text = handle.read()
        self.assertIn("Lobby", text)
        self.assertNotIn("OnlyBefore", text)

    def test_other_writes_are_refused_while_it_runs(self):
        seen = {}

        def poke():
            seen["response"] = self.m.APP.test_client().post("/api/config", json={})

        with self.writing(before=("device_aliases.json", poke)):
            self.restore()
        response = seen["response"]
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["reason"], "restore_in_progress")

    def test_reads_still_work_while_it_runs(self):
        seen = {}

        def poke():
            seen["response"] = self.m.APP.test_client().get("/api/config")

        with self.writing(before=("device_aliases.json", poke)):
            self.restore()
        self.assertNotEqual(seen["response"].status_code, 409)

    def test_a_second_restore_cannot_start_inside_the_first(self):
        seen = {}

        def again():
            seen["response"] = self.restore()

        with self.writing(before=("device_aliases.json", again)):
            first = self.restore()
        self.assertEqual(seen["response"].status_code, 409)
        self.assertEqual(seen["response"].get_json()["reason"], "restore_in_progress")
        self.assertEqual(first.get_json()["status"], "RESTORED")

    def test_the_lock_is_released_after_a_failure(self):
        with self.writing(fail="producer_state.json"):
            self.restore()
        response = self.m.APP.test_client().post("/api/config", json={})
        self.assertNotEqual(response.status_code, 409,
                            "a failed restore left the application refusing writes")
        self.assertEqual(self.restore().get_json()["status"], "RESTORED")


class ALoadThatFailsAfterTheFilesAreAppliedTests(Base):

    def test_further_writes_are_refused_until_a_restart(self):
        with mock.patch.object(self.aliases, "_load", side_effect=RuntimeError("no")):
            response = self.restore()
        self.assertEqual(response.get_json()["status"], "RESTORED — RESTART REQUIRED")
        refused = self.m.APP.test_client().post("/api/config", json={})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.get_json()["reason"], "restart_required")
        self.assertNotEqual(self.m.APP.test_client().get("/api/config").status_code, 409)

    def test_the_restored_files_are_left_exactly_as_restored(self):
        with mock.patch.object(self.aliases, "_load", side_effect=RuntimeError("no")):
            self.restore()
        self.assertNotEqual(self.files(), self.before)
        self.assertIn("alex", self.usernames())


if __name__ == "__main__":
    unittest.main()
