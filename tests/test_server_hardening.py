"""Credential handling, Clear Units, persistence, cache traffic, audit text and backup.

These run on the real application with its state isolated and restored (`RealApp`,
`RealRemote`), the device transport faked in memory, and the network fenced.
"""
import io
import json
import os
import re
import threading
import time
import unittest
from unittest import mock

import _support
from _support import (ADMIN_PASSWORD, USER_PASSWORD, DISPLAY_1, DISPLAY_2, DISPLAY_3, SOURCE_A, SOURCE_B,
                      RealApp, RealRemote, identity)

import luma_audit_text
import luma_backup
import luma_store
import luma_users

A, B = identity(SOURCE_A), identity(SOURCE_B)
D1, D2, D3 = identity(DISPLAY_1), identity(DISPLAY_2), identity(DISPLAY_3)
CANARY = "canary-secret-pw-7731"
CANARY_FALLBACK = "canary-fallback-pw-9042"


class Base(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.app.seed()
        self.remote = RealRemote(self, self.app)
        self.store = self.remote.store
        self.m.CONFIG["password"] = CANARY
        self.m.CONFIG["fallback_password"] = CANARY_FALLBACK
        self.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        self.local = self.m.APP.test_client()
        self.admin = self.remote.sign_in("root", ADMIN_PASSWORD)


class CredentialNonDisclosureTests(Base):
    def test_the_local_computer_can_still_show_the_device_password(self):
        body = self.local.get("/api/config?include_password=1").get_json()
        self.assertEqual((body["password"], body["fallback_password"]), (CANARY, CANARY_FALLBACK))

    def test_a_remote_administrator_is_refused_the_password_and_gets_none(self):
        for flag in ("1", "true", "True"):
            response = self.admin.get("/api/config?include_password=" + flag)
            self.assertEqual(response.status_code, 403)
            text = response.get_data(as_text=True)
            self.assertNotIn(CANARY, text)
            self.assertNotIn(CANARY_FALLBACK, text)
            self.assertEqual(response.get_json()["reason"], "local_only")

    def test_the_plain_config_read_never_carries_a_password(self):
        body = self.admin.get("/api/config").get_json()
        self.assertNotIn("password", body)
        self.assertNotIn("fallback_password", body)
        self.assertNotIn(CANARY, json.dumps(body))
        self.assertTrue(body["has_fallback_password"])

    def test_no_remote_response_anywhere_contains_the_device_passwords(self):
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER,
                          luma_users.Permissions([A], [D1], []))
        client = self.remote.sign_in("alex", USER_PASSWORD)
        admin_gets = ["/api/config", "/api/config?include_password=1", "/api/cache", "/api/app_version",
                      "/api/adapters", "/api/heartbeat", "/api/admin/users", "/api/admin/devices",
                      "/api/admin/aliases", "/api/admin/groups", "/api/admin/audit",
                      "/api/remote_access", "/api/auth/session",
                      "/api/video_wall/state", "/api/firmware_status", "/api/files"]
        for path in admin_gets:
            text = self.admin.get(path).get_data(as_text=True)
            self.assertNotIn(CANARY, text, path)
            self.assertNotIn(CANARY_FALLBACK, text, path)
        for path in ("/api/user/state", "/api/auth/session", "/api/user/preview?source=" + A):
            text = client.get(path).get_data(as_text=True)
            self.assertNotIn(CANARY, text, path)

    def test_saving_settings_does_not_echo_the_passwords_back(self):
        response = self.remote.post(self.admin, "/api/config", {"username": "admin"})
        self.assertNotIn(CANARY, response.get_data(as_text=True))

    def test_the_audit_log_never_holds_a_password(self):
        self.remote.post(self.admin, "/api/config", {"password": "a-new-device-secret",
                                                     "fallback_password": "another-new-secret"})
        text = repr(self.store.audit_entries(limit=200))
        for secret in ("a-new-device-secret", "another-new-secret", CANARY, ADMIN_PASSWORD):
            self.assertNotIn(secret, text)

    def test_the_credential_file_is_written_atomically_and_privately(self):
        self.m._save_persisted_config()
        leftovers = [n for n in os.listdir(self.m.DATA_DIR) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        if os.name != "nt":
            self.assertEqual(os.stat(self.m.CONFIG_FILE).st_mode & 0o077, 0)


class FirmwareHardeningTests(Base):
    def test_a_remote_administrator_cannot_name_a_file_by_absolute_path(self):
        for name in ("/etc/passwd", "C:\\Windows\\win.ini", "../../secret.tar.gz", "sub/x.tar.gz"):
            response = self.remote.post(self.admin, "/api/upgrade", {"targets": [SOURCE_A["ip"]], "file": name})
            self.assertEqual(response.status_code, 400, name)
            if name.startswith("C:\\") and os.name != "nt":
                # A drive-letter path is only a path on Windows. Elsewhere the
                # backslashes are ordinary filename characters, so the request
                # names an unknown file inside the firmware folder and is still
                # refused; nothing outside that folder is read.
                self.assertIn(response.get_json()["error"], ("firmware_name_only", "firmware_not_found"), name)
                continue
            self.assertEqual(response.get_json()["error"], "firmware_name_only", name)

    def test_a_remote_request_cannot_change_the_stored_device_credentials_through_an_upgrade(self):
        self.remote.post(self.admin, "/api/upgrade", {"username": "mallory", "password": "hijacked-pw-1",
                                                      "targets": [], "file": ""})
        self.assertEqual(self.m.CONFIG["password"], CANARY)
        self.assertEqual(self.m.CONFIG["username"], self._original_username)

    def test_the_local_computer_keeps_the_existing_upgrade_behaviour(self):
        self.local.post("/api/upgrade", json={"username": "localuser", "password": "local-pw-1234",
                                              "targets": [], "file": ""})
        self.assertEqual(self.m.CONFIG["username"], "localuser")
        self.assertEqual(self.m.CONFIG["password"], "local-pw-1234")

    def setUp(self):
        super().setUp()
        self._original_username = self.m.CONFIG["username"]

    def test_a_remote_file_list_carries_names_and_no_host_path(self):
        body = self.admin.get("/api/files").get_json()
        self.assertEqual(set(body), {"files"})
        local = self.local.get("/api/files").get_json()
        self.assertIn("base_dir", local)                       # the local operator is unchanged

    def test_browsing_the_host_filesystem_is_local_only(self):
        self.assertEqual(self.admin.get("/api/list_dir").status_code, 404)
        self.assertEqual(self.local.get("/api/list_dir").status_code, 200)


class ClearUnitsCascadeTests(Base):
    """Remote Access off: Clear Units empties the inventory and everything that pointed into it."""

    def setUp(self):
        super().setUp()
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions([A], [D1], []))
        self.group = self.remote.remote.groups.create("Lobby", [D2, D3])["group"]
        self.store.set_permissions(self.user_id(), luma_users.Permissions([A, B], [D1], [self.group["id"]]))
        self.remote.remote.aliases.set_alias(A, "Stage Camera")

    def user_id(self):
        return next(u["id"] for u in self.store.list_accounts() if u["username"] == "alex")

    def clear(self, client=None, **body):
        """A full clear: Remote Access is switched off for the request, as it must be.

        While Remote Access is on, Clear Units keeps assigned units (see
        test_clear_assigned). The cascade tested here is the Remote-Access-off semantics,
        so the listener is stopped around the call and restored for what follows.
        """
        listener = self.remote.remote.listener
        saved = (listener._server, listener._bound)
        listener._server, listener._bound = None, None
        try:
            return self.local.post("/api/clear_units", json=body)
        finally:
            listener._server, listener._bound = saved

    def keys(self):
        with self.m.cache_lock:
            return sorted(self.m.cache_units)

    def perms(self):
        return next(a for a in self.store.list_accounts() if a["username"] == "alex")["permissions"]

    def test_clearing_empties_the_inventory_whatever_is_assigned(self):
        body = self.clear().get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(self.keys(), [])
        self.assertEqual(body["cleared"], 7)

    def test_assignments_groups_and_names_are_cleaned_in_the_same_operation(self):
        body = self.clear().get_json()
        self.assertEqual((body["accounts_updated"], body["groups_updated"]), (1, 1))
        self.assertEqual(self.perms()["sources"], [])
        self.assertEqual(self.perms()["destinations"], [])
        self.assertEqual(self.remote.remote.groups.get(self.group["id"])["members"], [])
        self.assertEqual(self.remote.remote.aliases.all(), {})

    def test_a_group_grant_survives_but_resolves_to_nothing(self):
        self.clear()
        self.assertEqual(self.perms()["groups"], [self.group["id"]])

    def test_ghost_references_to_devices_not_in_the_inventory_are_cleaned_too(self):
        ghost = "02:00:5e:99:99:99"
        self.store.set_permissions(self.user_id(), luma_users.Permissions([A, ghost], [], []))
        self.clear()
        self.assertEqual(self.perms()["sources"], [])

    def test_the_signed_in_user_loses_the_devices_on_the_next_request(self):
        client = self.remote.sign_in("alex", USER_PASSWORD)
        before = client.get("/api/user/state").get_json()
        self.assertTrue(before["sources"])
        self.clear()
        after = client.get("/api/user/state").get_json()
        self.assertEqual((after["sources"], after["destinations"]), ([], []))
        refused = self.remote.post(client, "/api/user/route", {"source": A, "destination": D1})
        self.assertEqual(refused.status_code, 403)

    def test_a_direct_access_session_cannot_reach_cleared_devices(self):
        link = self.local.post("/api/admin/users/%s/direct_access" % self.user_id(),
                               json={"action": "enable"}).get_json()["link"]
        direct = self.m.APP.test_client()
        direct.environ_base["luma.listener"] = "remote"
        self.assertEqual(direct.get("/access/" + link.rsplit("/", 1)[1]).status_code, 200)
        self.assertTrue(direct.get("/api/user/state").get_json()["sources"])
        self.clear()
        state = direct.get("/api/user/state").get_json()
        self.assertEqual((state["sources"], state["destinations"], len(state["groups"])), ([], [], 1))
        self.assertEqual(direct.get("/api/user/state").status_code, 200)    # the link itself still works
        self.assertEqual(direct.get("/api/user/preview?source=" + A).status_code, 403)

    def test_a_rediscovered_device_is_available_for_new_assignment_but_not_automatically_assigned(self):
        self.clear()
        with self.m.cache_lock:
            self.m.cache_units[SOURCE_A["ip"]] = dict(SOURCE_A)
        self.assertEqual(self.perms()["sources"], [])
        ids = [d["id"] for d in self.admin.get("/api/admin/devices").get_json()["devices"]]
        self.assertIn(A, ids)

    def test_a_failure_part_way_restores_every_store(self):
        original = self.remote.remote.aliases.forget_many
        self.remote.remote.aliases.forget_many = lambda ids: {"ok": False, "removed": 0, "before": {}}
        try:
            response = self.clear()
        finally:
            self.remote.remote.aliases.forget_many = original
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(self.keys()), 7)
        self.assertEqual(self.perms()["sources"], sorted([A, B]))
        self.assertEqual(self.remote.remote.groups.get(self.group["id"])["members"], sorted([D2, D3]))

    def test_an_inventory_write_failure_restores_the_assignments(self):
        real = self.m.luma_store.atomic_write_json
        cache_path = self.m.CACHE_FILE

        def fail_for_the_inventory(path, payload, *a, **k):
            return False if str(path) == str(cache_path) else real(path, payload, *a, **k)

        with mock.patch.object(self.m.luma_store, "atomic_write_json", fail_for_the_inventory):
            response = self.clear()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(self.keys()), 7)
        self.assertEqual(self.perms()["sources"], sorted([A, B]))

    def test_removing_selected_units_cascades_for_those_units_only(self):
        # Remote Access off, as for Clear Units above. With it on, Remove Selected keeps what
        # Remote Access uses instead (see test_remove_selected).
        listener = self.remote.remote.listener
        saved = (listener._server, listener._bound)
        listener._server, listener._bound = None, None
        try:
            body = self.local.post("/api/remove_units", json={"ips": [SOURCE_A["ip"]]}).get_json()
        finally:
            listener._server, listener._bound = saved
        self.assertEqual(body["removed"], 1)
        self.assertEqual(self.perms()["sources"], [B])
        self.assertEqual(self.perms()["destinations"], [D1])

    def test_the_review_endpoint_is_gone(self):
        self.assertEqual(self.admin.get("/api/clear_units/preview").status_code, 404)

    def test_a_user_cannot_clear_units(self):
        client = self.remote.sign_in("alex", USER_PASSWORD)
        self.assertEqual(self.remote.post(client, "/api/clear_units", {}).status_code, 403)
        self.assertEqual(len(self.keys()), 7)

    def test_clearing_is_audited(self):
        self.clear()
        events = [e["event"] for e in self.store.audit_entries(limit=50)]
        self.assertIn("units.cleared", events)



class PersistenceTests(Base):
    def setUp(self):
        super().setUp()
        self.m._persist_units_cache = self._real_persist
        self.cache_file = self.m.CACHE_FILE
        self.addCleanup(lambda: os.path.exists(self.cache_file) and os.remove(self.cache_file))

    _real_persist = None

    def read(self):
        with open(self.cache_file, encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_cache_is_written_atomically_with_no_temporary_files(self):
        self.m._persist_units_cache()
        self.assertEqual(len(self.read()), 7)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.cache_file)) if ".tmp" in n], [])

    def test_a_malformed_cache_file_is_quarantined_and_discovery_carries_on(self):
        with open(self.cache_file, "w", encoding="utf-8") as handle:
            handle.write("[{ definitely not json")
        self.m._persist_units_cache()
        self.assertTrue(os.path.exists(self.cache_file + ".corrupt"))
        self.assertEqual(len(self.read()), 7)

    def test_a_malformed_file_never_propagates_through_the_merge(self):
        with open(self.cache_file, "w", encoding="utf-8") as handle:
            handle.write("not json at all")
        self.assertEqual(self.m._merge_disk_into_memory(), 0)
        with self.m.cache_lock:
            self.assertEqual(len(self.m.cache_units), 7)

    def test_the_merge_adds_missing_units_and_never_overwrites_live_state(self):
        rows = [dict(SOURCE_A, hostname="STALE FROM DISK"), dict(_support.SOURCE_C, ip="192.0.2.77", mac="02:00:5e:10:00:77")]
        luma_store.atomic_write_json(self.cache_file, rows)
        self.assertEqual(self.m._merge_disk_into_memory(), 1)
        self.assertEqual(self.app.unit(SOURCE_A["ip"])["hostname"], "Alpha Camera")
        self.assertIsNotNone(self.app.unit("192.0.2.77"))

    def test_a_disk_row_at_a_held_address_never_replaces_it_even_with_no_hardware_identity(self):
        # The identity check alone would not stop this: a row with no MAC has no identity to match,
        # so only the held-address check keeps the stale row from replacing the live record.
        stale = {k: v for k, v in dict(SOURCE_A, hostname="STALE FROM DISK").items() if k != "mac"}
        luma_store.atomic_write_json(self.cache_file, [stale])
        self.assertEqual(self.m._merge_disk_into_memory(), 0)
        self.assertEqual(self.app.unit(SOURCE_A["ip"])["hostname"], "Alpha Camera")

    def test_a_write_whose_snapshot_predates_a_clear_does_not_resurrect_the_units(self):
        original = luma_store.read_json
        fired = []

        def clear_during_the_disk_read(*args, **kwargs):
            if not fired:
                fired.append(1)
                with self.m.cache_lock:
                    self.m.cache_generation += 1
                    self.m.cache_units.clear()
            return original(*args, **kwargs)

        luma_store.read_json = clear_during_the_disk_read
        try:
            self.m._persist_units_cache()
        finally:
            luma_store.read_json = original
        self.assertFalse(os.path.exists(self.cache_file) and self.read())

    def test_a_merge_whose_disk_read_predates_a_clear_does_not_resurrect_the_units(self):
        # The Device Manager reads the cache continuously. A read that began before Clear Units
        # holds the old file's rows; applying them afterwards would put the cleared units back.
        luma_store.atomic_write_json(self.cache_file, [dict(SOURCE_A), dict(_support.SOURCE_C, ip="192.0.2.77", mac="02:00:5e:10:00:77")])
        original = luma_store.read_json
        fired = []

        def clear_during_the_disk_read(*args, **kwargs):
            rows = original(*args, **kwargs)
            if not fired:
                fired.append(1)
                with self.m.cache_lock:
                    self.m.cache_generation += 1
                    self.m.cache_units.clear()
                luma_store.atomic_write_json(self.cache_file, [])
            return rows

        luma_store.read_json = clear_during_the_disk_read
        try:
            added = self.m._merge_disk_into_memory()
        finally:
            luma_store.read_json = original
        self.assertEqual(fired, [1], "precondition: the clear landed during the disk read")
        with self.m.cache_lock:
            held = sorted(self.m.cache_units)
        self.assertEqual((added, held), (0, []), "the stale snapshot was merged back into the cleared inventory")

    def test_concurrent_writers_leave_a_valid_file(self):
        errors = []

        def worker():
            try:
                for _ in range(15):
                    self.m._persist_units_cache()
            except Exception as exc:                          # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.read()), 7)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.cache_file)) if ".tmp" in n], [])

    def test_a_malformed_video_wall_file_is_quarantined_and_reads_as_empty(self):
        path = self.m.VIDEO_WALL_STATE_FILE
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.addCleanup(lambda: [os.path.exists(p) and os.remove(p) for p in (path, path + ".corrupt")])
        self.assertEqual(self.m._load_video_wall_state()["walls"], [])
        self.assertTrue(os.path.exists(path + ".corrupt"))

    def test_the_video_wall_state_is_written_atomically(self):
        path = self.m.VIDEO_WALL_STATE_FILE
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.m._save_video_wall_state({"walls": [], "ignored_candidates": []})
        self.assertEqual(json.load(open(path, encoding="utf-8"))["kind"], "lumasuite.video_walls")
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if ".tmp" in n], [])


# The persist tests need the real function, which RealApp replaced with a no-op.
PersistenceTests._real_persist = staticmethod(__import__("lumaserver")._persist_units_cache)


class CacheTrafficTests(Base):
    def setUp(self):
        super().setUp()
        self.hostname_reads = []
        self.app.patch("_ws_get_hostname", lambda ip, timeout: self.hostname_reads.append(ip) or "")
        self.app.patch("_ws_get_dante_info", lambda ip, timeout: {})
        self.app.patch("_ws_get_dante_power_status", lambda ip, timeout: None)
        self.app.patch("_ws_get_hdcp_encoder", lambda ip, timeout: "")
        self.app.patch("_ws_get_hdcp_decoder", lambda ip, timeout: "")
        self.m._cache_verify_state.update(running=False, finished=-1e9)

    def wait_idle(self):
        deadline = time.time() + 5
        while self.m._cache_verify_state["running"] and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.m._cache_verify_state["running"], "the sweep did not finish")

    def test_many_page_loads_start_one_sweep_not_one_each(self):
        for _ in range(8):
            self.local.get("/api/cache")
        self.wait_idle()
        self.assertEqual(len(self.hostname_reads), 7)         # seven units, swept once

    def test_a_second_sweep_waits_for_the_minimum_interval(self):
        self.local.get("/api/cache")
        self.wait_idle()
        self.local.get("/api/cache")
        self.wait_idle()
        self.assertEqual(len(self.hostname_reads), 7)

    def test_and_runs_again_once_the_interval_has_passed(self):
        self.local.get("/api/cache")
        self.wait_idle()
        self.m._cache_verify_state["finished"] = time.monotonic() - self.m.CACHE_VERIFY_MIN_INTERVAL - 1
        self.local.get("/api/cache")
        self.wait_idle()
        self.assertEqual(len(self.hostname_reads), 14)

    def test_the_guard_is_released_even_when_the_sweep_fails(self):
        self.app.patch("_ws_get_hostname", lambda ip, timeout: (_ for _ in ()).throw(RuntimeError("boom")))
        self.local.get("/api/cache")
        self.wait_idle()
        self.assertFalse(self.m._cache_verify_state["running"])

    def test_the_restricted_state_does_not_start_a_device_sweep(self):
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions([A], [D1], []))
        client = self.remote.sign_in("alex", USER_PASSWORD)
        for _ in range(5):
            client.get("/api/user/state")
        self.wait_idle()
        self.assertEqual(self.hostname_reads, [])

    def test_many_users_share_one_refresh_window(self):
        polls = []
        self.m._observe_cache = __import__("luma_statecache").StateCache(ttl=30)
        self.app.patch("_refresh_matrix_state_core", lambda: polls.append(1) or 0)
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions([A], [D1], []))
        clients = [self.remote.sign_in("alex", USER_PASSWORD) for _ in range(5)]
        threads = [threading.Thread(target=lambda c=c: c.get("/api/user/state")) for c in clients]
        for t in threads: t.start()
        for t in threads: t.join(10)
        self.assertEqual(len(polls), 1)


class AuditTextTests(unittest.TestCase):
    ROOT = _support.ROOT

    def emitted_events(self):
        events = set()
        pattern = re.compile(r'''audit\(\s*(?:"([a-z_.]+)"|'([a-z_.]+)')''')
        # Events chosen by a variable are still string literals somewhere in the module.
        # An event name is `area.what`, or one of three single words.
        literal = re.compile(r'''"((?:login|user|direct_access|remote_access|certificate|access|device|'''
                             r'''group|units|backup|bootstrap)\.[a-z_]+|logout|route|route_group)"''')
        for name in sorted(os.listdir(self.ROOT)):
            if name.endswith(".py") and (name.startswith("luma_") or name == "lumaserver.py"):
                text = open(os.path.join(self.ROOT, name), encoding="utf-8").read()
                for m in pattern.finditer(text):
                    events.add(m.group(1) or m.group(2))
                if name == "luma_users.py":
                    # events chosen by a variable are still string literals somewhere in the module
                    events.update(m.group(1) for m in literal.finditer(text))
        return {e for e in events if e}

    def test_every_event_the_source_can_emit_has_a_human_label(self):
        missing = sorted(e for e in self.emitted_events() if e not in luma_audit_text.EVENT_LABELS)
        self.assertEqual(missing, [], "events with no label would reach an administrator as their own name")

    def test_every_event_flushed_immediately_is_an_event_that_exists(self):
        stale = sorted(e for e in luma_users.AUDIT_IMMEDIATE_EVENTS if e not in self.emitted_events())
        self.assertEqual(stale, [], "an event nothing emits can never be flushed immediately")
        for event in ("user.password_changed", "user.role_changed", "user.permissions_changed"):
            self.assertIn(event, luma_users.AUDIT_IMMEDIATE_EVENTS)

    def test_the_set_of_emitted_events_is_not_trivially_small(self):
        self.assertGreater(len(self.emitted_events()), 20)

    def test_an_unknown_event_falls_back_to_english_never_to_the_id(self):
        text = luma_audit_text.describe({"event": "some.new_thing_happened", "detail": {}})
        self.assertNotIn("some.new", text)
        self.assertNotIn("_", text)
        self.assertEqual(luma_audit_text.event_label("brand.new_event"), "Brand new event")

    def test_an_unknown_reason_is_explained_in_words(self):
        text = luma_audit_text.describe({"event": "access.denied", "detail": {"reason": "missing_feature:usb"}})
        self.assertNotIn("missing_feature", text)
        self.assertNotIn(":", text.replace("because", ""))

    def test_no_label_or_sentence_contains_an_internal_name(self):
        for event, label in luma_audit_text.EVENT_LABELS.items():
            self.assertNotRegex(label, r"[a-z]+[._][a-z]+", event)
        for reason, label in luma_audit_text.REASON_LABELS.items():
            self.assertNotRegex(label, r"[a-z]+_[a-z]+", reason)

    def test_a_route_group_sentence_names_the_failed_displays_in_words(self):
        entry = {"event": "route_group", "actor": "alex", "detail": {
            "source": A, "group": "grp_x", "group_name": "Lobby", "routed": 2, "total": 3,
            "results": [{"display": D1, "ok": True, "result": "verified"},
                        {"display": D2, "ok": False, "result": "unreachable"}]}}
        text = luma_audit_text.describe(entry, lambda i: {A: "Main Stage", D2: "Lobby Two"}.get(i, "a device"))
        self.assertIn("2 of 3 displays routed", text)
        self.assertIn("Lobby Two: the device did not answer", text)
        self.assertNotIn("unreachable", text)

    def test_the_audit_api_returns_sentences_and_no_raw_detail(self):
        # Through the real application: the page never needs an event id or a reason.
        app = RealApp(self)
        remote = RealRemote(self, app)
        remote.store.create("root", ADMIN_PASSWORD, luma_users.ROLE_ADMIN)
        remote.store.audit("access.denied", actor=None, detail={"reason": "missing_feature:usb", "rule": "/x"})
        admin = remote.sign_in("root", ADMIN_PASSWORD)
        body = admin.get("/api/admin/audit").get_json()
        entry = [e for e in body["entries"] if e["event"] == "access.denied"][0]
        self.assertNotIn("detail", entry)
        self.assertNotIn("missing_feature", entry["text"])
        self.assertTrue(all("label" in e and "text" in e for e in body["entries"]))
        for item in body["summary"]["events"]:
            self.assertNotRegex(item["label"], r"[a-z]\.[a-z]")

    def test_audit_detail_is_redacted_recursively_and_usernames_are_not(self):
        redacted = luma_users.redact({"username": "alex", "password": "p", "nested": [{"token": "t",
                                      "csrf": "c", "ok": 1}], "password_hash": "h"})
        self.assertEqual(redacted["username"], "alex")
        self.assertNotEqual(redacted["password"], "p")
        self.assertNotEqual(redacted["nested"][0]["token"], "t")
        self.assertNotEqual(redacted["nested"][0]["csrf"], "c")
        self.assertEqual(redacted["nested"][0]["ok"], 1)


class BackupContainerTests(unittest.TestCase):
    PAYLOAD = {"sections": {"accounts": {"users": []}}, "devices": [], "machine": {}, "notes": []}

    def test_a_round_trip_returns_the_payload(self):
        blob = luma_backup.seal(self.PAYLOAD, "twelve chars!!", app_version="1.1.0")
        header, payload = luma_backup.unseal(blob, "twelve chars!!")
        self.assertEqual(payload["sections"], self.PAYLOAD["sections"])
        self.assertEqual(payload["devices"], [])
        self.assertEqual(header["lumasuite_version"], "1.1.0")

    def test_the_wrong_passphrase_is_refused(self):
        blob = luma_backup.seal(self.PAYLOAD, "correct passphrase")
        with self.assertRaises(luma_backup.BackupError):
            luma_backup.unseal(blob, "incorrect passphrase")

    def test_altering_any_byte_of_the_header_is_refused(self):
        lines = luma_backup.seal(self.PAYLOAD, "correct passphrase").split(b"\n")
        lines[1] = lines[1].replace(b'"format":1', b'"format":2')
        with self.assertRaises(luma_backup.BackupError):
            luma_backup.unseal(b"\n".join(lines), "correct passphrase")

    def test_the_passphrase_minimum_is_eight_characters_and_length_only(self):
        for weak in ("", "short", "1234567", None):
            with self.assertRaises(luma_backup.BackupError):
                luma_backup.seal(self.PAYLOAD, weak)
        for fine in ("12345678", "aaaaaaaa", "        ", "password"):         # no composition rules
            luma_backup.seal(self.PAYLOAD, fine)

    def test_the_payload_never_appears_in_the_file_in_the_clear(self):
        payload = dict(self.PAYLOAD, sections={"device_credentials": {"password": CANARY}})
        blob = luma_backup.seal(payload, "correct passphrase")
        self.assertNotIn(CANARY.encode(), blob)

    def test_the_key_the_certificate_and_the_audit_history_are_never_carried(self):
        self.assertIn("remote_key.pem", luma_backup.EXCLUDED)
        self.assertIn("remote_cert.pem", luma_backup.EXCLUDED)
        self.assertIn("security_audit.json", luma_backup.EXCLUDED)
        carried = {s.filename for s in luma_backup.SECTIONS}
        for name in luma_backup.EXCLUDED:
            self.assertNotIn(name, carried)

    def test_what_is_carried_is_exactly_what_the_product_needs(self):
        self.assertEqual({s.filename for s in luma_backup.SECTIONS},
                         {"users.json", "device_aliases.json", "display_groups.json",
                          "producer_config.json", "remote_access.json", "video_walls.json",
                          "producer_state.json"})

    def test_machine_bound_settings_are_set_aside_not_written_blindly(self):
        by_name = {s.name: s for s in luma_backup.SECTIONS}
        self.assertIn("adapter_id", by_name["remote_access"].machine_keys)
        self.assertIn("cert_path", by_name["remote_access"].machine_keys)
        self.assertIn("firmware_path", by_name["device_credentials"].machine_keys)

    def test_direct_access_keeps_its_digest_and_drops_the_recoverable_blob(self):
        accounts = {"users": [{"direct_access": {"enabled": True, "digest": "abc", "blob": "SECRETBLOB",
                                                 "protection": "dpapi"}}]}
        self.assertEqual(luma_backup.strip_direct_access_blobs(accounts), 1)
        record = accounts["users"][0]["direct_access"]
        self.assertEqual((record["digest"], record["blob"]), ("abc", ""))

    def test_sessions_and_csrf_state_have_no_file_to_leak(self):
        carried = " ".join(s.filename for s in luma_backup.SECTIONS)
        self.assertNotIn("session", carried)

    def test_devices_are_carried_by_hardware_identity_and_never_by_address(self):
        devices = luma_backup.collect_devices([
            {"mac": "02:00:5e:10:00:0a", "ip": "192.0.2.10", "hostname": "Alpha", "model": "M", "status": "idle"},
            {"ip": "192.0.2.11", "hostname": "No mac"}])
        self.assertEqual(devices, [{"mac": "02:00:5e:10:00:0a", "hostname": "Alpha", "model": "M"}])

    def test_a_backup_naming_a_group_member_without_an_identity_is_invalid(self):
        bad = {"sections": {"display_groups": {"groups": [{"id": "grp_1", "name": "G",
                                                            "members": ["192.0.2.10"]}]}}}
        self.assertTrue(luma_backup.validate(bad))


class BackupRestoreEndpointTests(Base):
    def export(self, passphrase="a good backup passphrase"):
        return self.local.post("/api/backup/export", json={"passphrase": passphrase})

    def upload(self, path, blob, passphrase="a good backup passphrase", client=None):
        return (client or self.local).post(path, data={"backup": (io.BytesIO(blob), "b.lumabak"),
                                                       "passphrase": passphrase},
                                           content_type="multipart/form-data")

    def setUp(self):
        super().setUp()
        self.store.create("alex", USER_PASSWORD, luma_users.ROLE_USER, luma_users.Permissions([A], [D1], []))
        self.remote.remote.aliases.set_alias(A, "Main Stage")
        self.group = self.remote.remote.groups.create("Lobby", [D1, D2])["group"]
        self.addCleanup(lambda: [os.path.exists(p) and os.remove(p) for p in
                                 (self.m.CONFIG_FILE,)])

    def test_backup_routes_are_local_only(self):
        for path in ("/api/backup/export", "/api/backup/inspect", "/api/backup/restore"):
            self.assertEqual(self.remote.post(self.admin, path, {"passphrase": "a good backup passphrase"}).status_code, 404, path)

    def test_an_export_is_an_attachment_that_is_never_cached(self):
        response = self.export()
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertTrue(response.headers["Content-Disposition"].endswith('.lumabak"'))
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertNotIn(CANARY.encode(), response.data)

    def test_a_weak_passphrase_refuses_the_export(self):
        self.assertEqual(self.export("short").status_code, 400)

    def test_the_passphrase_is_never_logged(self):
        with self.assertLogs(level="INFO") as logs:
            self.export("a very identifiable passphrase 55")
            self.m.logging.info("sentinel")
        self.assertNotIn("identifiable passphrase", "\n".join(logs.output))

    def test_inspect_shows_the_contents_and_changes_nothing(self):
        blob = self.export().data
        before = [a["username"] for a in self.store.list_accounts()]
        body = self.upload("/api/backup/inspect", blob).get_json()
        self.assertTrue(body["ok"])
        names = {s["name"] for s in body["summary"]["sections"]}
        self.assertTrue({"accounts", "device_names", "display_groups"} <= names)
        self.assertEqual([a["username"] for a in self.store.list_accounts()], before)

    def test_a_wrong_passphrase_is_refused_and_changes_nothing(self):
        blob = self.export().data
        response = self.upload("/api/backup/restore", blob, passphrase="not the passphrase")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.store.list_accounts()), 2)

    def test_a_restore_brings_back_accounts_names_groups_and_credentials(self):
        self.m._save_persisted_config()          # the device credentials, as Settings saves them
        blob = self.export().data
        # break everything the backup is meant to repair
        alex = next(a["id"] for a in self.store.list_accounts() if a["username"] == "alex")
        self.store.delete(alex)
        self.remote.remote.aliases.set_alias(A, "")
        self.remote.remote.groups.delete(self.group["id"])
        self.m.CONFIG["password"] = "something-else-entirely"
        self.m._save_persisted_config()
        body = self.upload("/api/backup/restore", blob).get_json()
        self.assertEqual(body["status"], "RESTORED")
        self.assertIn("alex", [a["username"] for a in self.store.list_accounts()])
        self.assertEqual(self.remote.remote.aliases.alias_for(A), "Main Stage")
        self.assertEqual(self.remote.remote.groups.get(self.group["id"])["members"], [D1, D2])
        self.assertEqual(self.m.CONFIG["password"], CANARY)

    def test_a_restore_that_fails_partway_puts_the_previous_configuration_back(self):
        blob = self.export().data
        self.remote.remote.aliases.set_alias(A, "Changed After Backup")
        calls = []
        original = self.m._restore_write

        def fail_on_the_third_file(path, content):
            calls.append(path)
            if len(calls) == 3:
                raise OSError("disk full")
            return original(path, content)

        self.m._restore_write = fail_on_the_third_file
        try:
            response = self.upload("/api/backup/restore", blob)
        finally:
            self.m._restore_write = original
        body = response.get_json()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(body["status"], "FAILED \u2014 ROLLED BACK")
        self.assertEqual(self.remote.remote.aliases.alias_for(A), "Changed After Backup")

    def test_the_restored_account_table_cannot_lock_the_local_technician_out(self):
        blob = self.export().data
        self.upload("/api/backup/restore", blob)
        # the local operator still reaches administration with no password
        self.assertEqual(self.local.get("/api/admin/users").status_code, 200)

    def test_generated_machine_specific_files_are_not_in_the_export(self):
        keyfile = os.path.join(self.m.DATA_DIR, "remote_key.pem")
        open(keyfile, "w").write("-----BEGIN PRIVATE KEY-----\nSECRETKEYMATERIAL\n")
        self.addCleanup(lambda: os.path.exists(keyfile) and os.remove(keyfile))
        blob = self.export().data
        header, payload = luma_backup.unseal(blob, "a good backup passphrase")
        self.assertNotIn("SECRETKEYMATERIAL", json.dumps(payload))
        self.assertNotIn("security_audit", json.dumps(sorted(payload["sections"])))


if __name__ == "__main__":
    unittest.main()
