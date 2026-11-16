"""What a restore does to each thing: replaced, preserved locally, or regenerated.

The contract is the module docstring and `luma_backup.RESTORE_RULES`; these tests hold the
behaviour to it on the real application: isolated state, the transport faked, the network
fenced. Account routing-interface fields travel in a backup, and a backup written before
they existed migrates without anyone losing the interface they use.
"""
import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, SOURCE_A,
                      RealApp, RealRemote, identity, saved_wall)

import luma_backup
import luma_users

A = identity(SOURCE_A)
D1, D2 = identity(DISPLAY_1), identity(DISPLAY_2)
PASS = "a good backup passphrase"


class RuleTableTests(unittest.TestCase):
    def test_every_section_has_exactly_one_rule(self):
        names = {s.name for s in luma_backup.SECTIONS}
        self.assertEqual({s.on_absent for s in luma_backup.SECTIONS} - {"remove", "keep"}, set())
        removable = set(luma_backup.RESTORE_RULES["removed_when_the_backup_has_none"])
        kept = {s.name for s in luma_backup.SECTIONS if s.on_absent == "keep"}
        self.assertEqual(removable | kept, names)
        self.assertEqual(removable & kept, set())

    def test_device_sign_in_and_remote_access_are_never_removed(self):
        by_name = {s.name: s for s in luma_backup.SECTIONS}
        for name in ("device_credentials", "remote_access"):
            self.assertEqual(by_name[name].on_absent, "keep", name)

    def test_accounts_are_part_of_the_authoritative_set(self):
        # The approved rule: a backup that held no accounts removes the ones on this computer.
        # The operator is warned first (see test_restore_sessions), and the local operator can
        # always create an administrator again.
        by_name = {s.name: s for s in luma_backup.SECTIONS}
        self.assertEqual(by_name["accounts"].on_absent, "remove")
        self.assertIn("accounts", luma_backup.RESTORE_RULES["removed_when_the_backup_has_none"])

    def test_the_table_names_all_three_groups(self):
        for group in ("replaced", "removed_when_the_backup_has_none", "preserved_locally", "regenerated"):
            self.assertTrue(luma_backup.RESTORE_RULES[group], group)

    def test_a_new_backup_says_what_it_manages(self):
        payload = luma_backup.build_payload(tempfile.mkdtemp(), [], app_version="x")
        self.assertEqual(payload["managed"], [s.name for s in luma_backup.SECTIONS])


class ApplyTests(unittest.TestCase):
    """apply_payload on a scratch folder: what is written, kept and removed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="luma-rules-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for section in luma_backup.SECTIONS:
            self.write(section.filename, {"current": section.name})

    def write(self, name, content):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            json.dump(content, handle)

    def exists(self, name):
        return os.path.exists(os.path.join(self.dir, name))

    def apply(self, payload):
        def write_json(path, content):
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(content, handle)
        return luma_backup.apply_payload(payload, self.dir, write_json=write_json)

    def payload(self, held, managed=True):
        sections = {name: {"from_backup": name} for name in held}
        body = {"sections": sections, "machine": {}}
        if managed:
            body["managed"] = [s.name for s in luma_backup.SECTIONS]
        return body

    def test_a_record_absent_from_the_backup_is_gone_from_a_replaced_section(self):
        self.write("display_groups.json", {"groups": [{"id": "x", "name": "Only Now", "members": []}]})
        self.apply(self.payload(["display_groups"]))
        with open(os.path.join(self.dir, "display_groups.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"from_backup": "display_groups"})

    def test_a_managed_section_the_backup_held_none_of_is_removed(self):
        result = self.apply(self.payload(["accounts"]))
        for name, filename in (("device_names", "device_aliases.json"),
                               ("display_groups", "display_groups.json"),
                               ("video_walls", "video_walls.json"),
                               ("production_studio", "producer_state.json")):
            self.assertFalse(self.exists(filename), name)
            self.assertIn(filename, result["removed"])

    def test_device_sign_in_and_remote_access_survive_their_absence(self):
        self.apply(self.payload([]))
        for filename in ("producer_config.json", "remote_access.json"):
            self.assertTrue(self.exists(filename), filename)

    def test_accounts_the_backup_held_none_of_are_removed(self):
        result = self.apply(self.payload([]))
        self.assertFalse(self.exists("users.json"))
        self.assertIn("users.json", result["removed"])

    def test_a_backup_that_does_not_say_what_it_manages_removes_nothing(self):
        result = self.apply(self.payload(["accounts"], managed=False))
        self.assertEqual(result["removed"], [])
        for section in luma_backup.SECTIONS:
            self.assertTrue(self.exists(section.filename), section.filename)

    def test_machine_bound_values_stay_with_this_computer(self):
        self.write("producer_config.json", {"username": "old", "firmware_path": "D:/here", "logos_path": "D:/l"})
        payload = {"sections": {"device_credentials": {"username": "new", "firmware_path": "E:/there"}},
                   "machine": {"device_credentials": {"firmware_path": "E:/there"}}, "managed": ["device_credentials"]}
        self.apply(payload)
        with open(os.path.join(self.dir, "producer_config.json"), encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual((written["username"], written["firmware_path"]), ("new", "D:/here"))

    def test_the_summary_names_what_will_be_removed_and_only_what_exists(self):
        os.remove(os.path.join(self.dir, "video_walls.json"))
        summary = luma_backup.summarise(self.payload(["accounts"]), {}, self.dir)
        self.assertEqual(summary["will_remove"], ["device_names", "display_groups", "production_studio"])
        self.assertTrue(any("will be removed" in note for note in summary["notes"]))
        legacy = luma_backup.summarise(self.payload(["accounts"], managed=False), {}, self.dir)
        self.assertEqual(legacy["will_remove"], [])

    def test_a_failure_after_a_removal_puts_the_removed_file_back(self):
        snapshot = tempfile.mkdtemp(prefix="luma-rules-snap-")
        self.addCleanup(shutil.rmtree, snapshot, True)
        before = {s.filename: open(os.path.join(self.dir, s.filename), "rb").read()
                  for s in luma_backup.SECTIONS}
        saved = luma_backup.snapshot(self.dir, snapshot)

        def failing_writer(path, content):
            raise OSError("disk went away")
        # Accounts are written first (and fail); remove one managed file first to prove the
        # rollback brings back a deletion as well as a write.
        os.remove(os.path.join(self.dir, "video_walls.json"))
        with self.assertRaises(OSError):
            luma_backup.apply_payload(self.payload(["accounts"]), self.dir, write_json=failing_writer)
        restored, failures = luma_backup.rollback(snapshot, self.dir, saved)
        self.assertEqual(failures, [])
        after = {s.filename: open(os.path.join(self.dir, s.filename), "rb").read()
                 for s in luma_backup.SECTIONS}
        self.assertEqual(after, before)


class MigrationTests(unittest.TestCase):
    def accounts(self, *permissions_by_role):
        users = []
        for index, (role, permissions) in enumerate(permissions_by_role):
            entry = {"id": "u%d" % index, "username": "n%d" % index, "password_hash": "h", "role": role}
            if permissions is not None:
                entry["permissions"] = permissions
            users.append(entry)
        return {"sections": {"accounts": {"users": users}}, "machine": {}}

    def migrate(self, payload):
        return luma_backup.migrate(payload, {"format": 1})["sections"]["accounts"]["users"]

    def test_an_account_from_before_the_setting_keeps_standard_and_gets_no_matrix(self):
        users = self.migrate(self.accounts(("user", {"sources": [], "destinations": [], "groups": []})))
        self.assertEqual((users[0]["permissions"]["standard_routing"], users[0]["permissions"]["matrix_routing"]),
                         (True, False))

    def test_an_explicit_choice_is_never_overwritten(self):
        users = self.migrate(self.accounts(
            ("user", {"standard_routing": False, "matrix_routing": True}),
            ("user", {"standard_routing": True, "matrix_routing": True}),
            ("user", {"matrix_routing": False})))
        self.assertEqual([(u["permissions"].get("standard_routing"), u["permissions"]["matrix_routing"]) for u in users],
                         [(False, True), (True, True), (None, False)])

    def test_administrators_have_no_setting_written(self):
        users = self.migrate(self.accounts(("admin", {"sources": []}), ("admin", None)))
        self.assertNotIn("standard_routing", users[0]["permissions"])
        self.assertNotIn("permissions", users[1])

    def test_the_count_reaches_the_summary_in_words(self):
        payload = luma_backup.migrate(self.accounts(("user", {}), ("user", {}), ("user", {"matrix_routing": True})),
                                      {"format": 1})
        self.assertEqual(payload["migrated_accounts"], 2)
        notes = luma_backup.summarise(payload, {})["notes"]
        self.assertTrue(any("2 accounts from an earlier version will keep Standard routing" in n for n in notes), notes)

    def test_a_malformed_account_list_is_left_for_validation_not_a_crash(self):
        for bad in ({"sections": {"accounts": {"users": "nope"}}}, {"sections": {"accounts": {"users": [3, None]}}},
                    {"sections": {"accounts": {}}}):
            luma_backup.migrate(copy.deepcopy(bad), {"format": 1})


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.store = self.remote.store
        self.system = self.remote.remote
        self.names = [s.filename for s in luma_backup.SECTIONS]
        self._originals = {}
        for name in self.names:
            path = os.path.join(self.m.DATA_DIR, name)
            if os.path.exists(path):
                self._originals[name] = open(path, "rb").read()
        self.addCleanup(self._put_back)
        self.snapshot_dir = tempfile.mkdtemp(prefix="luma-rules-snap-")
        self.addCleanup(shutil.rmtree, self.snapshot_dir, True)
        patcher = mock.patch.object(self.m, "BACKUP_SNAPSHOT_DIR", self.snapshot_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reset_flags)
        self.local = self.m.APP.test_client()
        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.m._save_persisted_config()

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

    def export(self):
        response = self.local.post("/api/backup/export", json={"passphrase": PASS})
        self.assertEqual(response.status_code, 200)
        return response.data

    def restore(self, blob):
        return self.local.post("/api/backup/restore", content_type="multipart/form-data",
                               data={"backup": (io.BytesIO(blob), "b.lumabak"), "passphrase": PASS})

    def accounts(self):
        return {a["username"]: a["permissions"] for a in self.store.list_accounts()}

    def test_routing_interfaces_round_trip(self):
        self.store.create("both", USER_PASSWORD, luma_users.ROLE_USER,
                          luma_users.Permissions([A], [D1], [], (), True, True))
        self.store.create("matrixonly", USER_PASSWORD, luma_users.ROLE_USER,
                          luma_users.Permissions([A], [D1], [], (), False, True))
        self.store.create("standardonly", USER_PASSWORD, luma_users.ROLE_USER,
                          luma_users.Permissions([A], [D1], []))
        blob = self.export()
        for name in ("both", "matrixonly", "standardonly"):
            self.store.delete(next(a["id"] for a in self.store.list_accounts() if a["username"] == name))
        self.assertEqual(self.restore(blob).get_json()["status"], "RESTORED")
        got = {n: (p["standard_routing"], p["matrix_routing"]) for n, p in self.accounts().items() if n != "root"}
        self.assertEqual(got, {"both": (True, True), "matrixonly": (False, True), "standardonly": (True, False)})

    def test_the_current_state_that_the_backup_does_not_hold_is_replaced(self):
        self.store.create("kept", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions([A], [D1], []))
        group = self.system.groups.create("InBackup", [D1])["group"]
        self.system.aliases.set_alias(A, "Backed Up Name")
        blob = self.export()
        self.system.groups.create("AddedLater", [D2])
        self.system.aliases.set_alias(identity(DISPLAY_2), "Added Later")
        self.store.create("added", USER_PASSWORD, luma_users.ROLE_USER)
        body = self.restore(blob).get_json()
        self.assertEqual(body["status"], "RESTORED")
        self.assertEqual([g["name"] for g in self.system.groups.all()], ["InBackup"])
        self.assertEqual(self.system.aliases.all(), {A: "Backed Up Name"})
        self.assertNotIn("added", self.accounts())
        self.assertIn("kept", self.accounts())
        self.assertEqual(group["id"], self.system.groups.all()[0]["id"])

    def test_a_group_list_empty_in_the_backup_is_empty_afterwards(self):
        blob = self.export()                               # no groups, no walls, no names at export
        group = self.system.groups.create("Later", [D1])["group"]
        self.system.aliases.set_alias(A, "Later Name")
        state = self.m._load_video_wall_state()
        state["walls"] = [saved_wall("w1", "Later Wall", [DISPLAY_1])]
        self.m._save_video_wall_state(state)
        body = self.restore(blob).get_json()
        self.assertEqual(body["status"], "RESTORED")
        self.assertEqual(self.system.groups.all(), [])
        self.assertEqual(self.system.aliases.all(), {})
        self.assertEqual(self.m._load_video_wall_state()["walls"], [])
        self.assertIn("root", self.accounts(), "accounts are never removed by absence")
        self.assertTrue(body["applied"]["removed"])

    def test_a_legacy_backup_changes_only_what_it_carries(self):
        self.system.groups.create("Local Group", [D1])
        legacy_payload = luma_backup.build_payload(self.m.DATA_DIR, [], app_version="old")
        del legacy_payload["managed"]
        for gone in ("display_groups", "device_names"):
            legacy_payload["sections"].pop(gone, None)
        blob = luma_backup.seal(legacy_payload, PASS, app_version="old")
        self.system.aliases.set_alias(A, "Local Name")
        body = self.restore(blob).get_json()
        self.assertEqual(body["status"], "RESTORED")
        self.assertEqual([g["name"] for g in self.system.groups.all()], ["Local Group"])
        self.assertEqual(self.system.aliases.all(), {A: "Local Name"})

    def test_a_legacy_account_without_the_setting_keeps_standard_routing(self):
        self.store.create("old", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions([A], [D1], []))
        payload = luma_backup.build_payload(self.m.DATA_DIR, [], app_version="old")
        for entry in payload["sections"]["accounts"]["users"]:
            entry.get("permissions", {}).pop("standard_routing", None)
            entry.get("permissions", {}).pop("matrix_routing", None)
        blob = luma_backup.seal(payload, PASS, app_version="old")
        self.store.delete(next(a["id"] for a in self.store.list_accounts() if a["username"] == "old"))
        self.assertEqual(self.restore(blob).get_json()["status"], "RESTORED")
        self.assertEqual((self.accounts()["old"]["standard_routing"], self.accounts()["old"]["matrix_routing"]),
                         (True, False))

    def test_inspect_tells_the_operator_what_will_be_removed_and_changes_nothing(self):
        blob = self.export()
        self.system.groups.create("Later", [D1])
        before = self.system.groups.all()
        response = self.local.post("/api/backup/inspect", content_type="multipart/form-data",
                                   data={"backup": (io.BytesIO(blob), "b.lumabak"), "passphrase": PASS})
        summary = response.get_json()["summary"]
        self.assertIn("display_groups", summary["will_remove"])
        self.assertTrue(any("display groups" in n and "removed" in n for n in summary["notes"]))
        self.assertEqual(self.system.groups.all(), before)

    def test_a_failed_restore_after_a_removal_leaves_no_hybrid(self):
        blob = self.export()
        self.system.groups.create("Later", [D1])
        before = {n: open(os.path.join(self.m.DATA_DIR, n), "rb").read() for n in self.names
                  if os.path.exists(os.path.join(self.m.DATA_DIR, n))}
        real = self.m._restore_write

        def write(path, content):
            if os.path.basename(str(path)) == "producer_config.json":
                raise OSError("the disk went away")
            return real(path, content)
        with mock.patch.object(self.m, "_restore_write", write):
            response = self.restore(blob)
        self.assertEqual(response.get_json()["status"], "FAILED \u2014 ROLLED BACK")
        after = {n: open(os.path.join(self.m.DATA_DIR, n), "rb").read() for n in self.names
                 if os.path.exists(os.path.join(self.m.DATA_DIR, n))}
        self.assertEqual(after, before)

    def test_after_a_restore_the_restored_assignments_protect_their_units(self):
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions([A], [], []))
        self.system.groups.create("Lobby", [D1])
        state = self.m._load_video_wall_state()
        state["walls"] = [saved_wall("w1", "Wall One", [DISPLAY_2])]
        self.m._save_video_wall_state(state)
        blob = self.export()
        self.store.delete(next(a["id"] for a in self.store.list_accounts() if a["username"] == "alex"))
        self.assertEqual(self.restore(blob).get_json()["status"], "RESTORED")
        admin = self.remote.sign_in("root", ADMIN_PASSWORD)
        body = self.remote.post(admin, "/api/clear_units", {}).get_json()
        self.assertEqual(body["scope"], "unassigned")
        with self.m.cache_lock:
            left = sorted(identity(u) for u in self.m.cache_units.values())
        self.assertEqual(left, sorted([A, D1, D2]))

    def test_the_passphrase_is_asked_for_once_and_still_enforced(self):
        # One passphrase in the request, the same length rule, the same cipher.
        weak = self.local.post("/api/backup/export", json={"passphrase": "short"})
        self.assertEqual(weak.status_code, 400)
        self.assertEqual(weak.get_json()["reason"], "weak_passphrase")
        self.assertEqual(luma_backup.MIN_PASSPHRASE, 8)
        header = luma_backup.read_header(self.export())
        self.assertEqual((header["cipher"], header["kdf"]["name"], header["kdf"]["n"]),
                         ("AES-256-GCM", "scrypt", 1 << 15))


if __name__ == "__main__":
    unittest.main()
