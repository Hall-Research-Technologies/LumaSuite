"""The product unification pass: scan bounds, adapter ranking, retention, breakaway,
the firmware schedule, the HTTP default, and the session facts the shell relies on.

Nothing here can reach a device or the network: `_support` installs the hardware
fence before `lumaserver` is imported, the firmware lookup is a mock, and every
device is an in-memory model that says what a device REPORTS, including the one
that accepts a command and does not hold it.
"""
import copy
import json
import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

import _support
from _support import RealApp, System, ADMIN_PASSWORD, USER_PASSWORD, SOURCE_A, DISPLAY_1, identity

import luma_adapters
import luma_audit_text
import luma_breakaway
import luma_remote
import luma_retention
import luma_scanplan
import luma_users


# ===================================================================== scan bounds

class ScanPlanTests(unittest.TestCase):
    def test_every_form_is_counted_without_expanding_it(self):
        self.assertEqual(luma_scanplan.check("192.0.2.17"), 1)
        self.assertEqual(luma_scanplan.check("192.0.2.1-254"), 254)
        self.assertEqual(luma_scanplan.check("192.0.2.0/24"), 254)
        self.assertEqual(luma_scanplan.check("198.51.100.1-198.51.101.254"), 510)
        self.assertEqual(luma_scanplan.check("192.0.2.1-10, 198.51.100.0/24"), 264)
        self.assertEqual(luma_scanplan.check(""), 0)
        self.assertEqual(luma_scanplan.check(None), 0)

    def test_a_giant_range_is_refused_arithmetically_and_at_once(self):
        # The address space is never materialised to discover that it is too large.
        for text in ("10.0.0.0/8", "0.0.0.0/0", "172.16.0.0/12", "10.0.0.1-10.255.255.254"):
            started = time.perf_counter()
            with self.assertRaises(luma_scanplan.ScanRangeError) as caught:
                luma_scanplan.check(text)
            self.assertEqual(caught.exception.reason, "range_too_large", text)
            self.assertLess(time.perf_counter() - started, 0.05, text)

    def test_the_bound_is_exact(self):
        self.assertEqual(luma_scanplan.MAX_RANGE_ADDRESSES, 4096)
        self.assertEqual(luma_scanplan.MAX_SCAN_ADDRESSES, 8192)
        self.assertEqual(luma_scanplan.MAX_RANGES, 32)
        self.assertEqual(luma_scanplan.check("192.0.2.0/20"), 4094)           # a /20 fits
        with self.assertRaises(luma_scanplan.ScanRangeError):
            luma_scanplan.check("192.0.2.0/19")                                  # a /19 does not

    def test_a_request_is_bounded_in_total_and_in_count(self):
        with self.assertRaises(luma_scanplan.ScanRangeError) as caught:
            luma_scanplan.check("192.0.2.0/20,198.51.100.0/20,203.0.113.0/20")
        self.assertEqual(caught.exception.reason, "scan_too_large")
        many = ",".join("192.0.2.%d" % n for n in range(1, 34))
        with self.assertRaises(luma_scanplan.ScanRangeError) as caught:
            luma_scanplan.check(many)
        self.assertEqual(caught.exception.reason, "too_many_ranges")

    def test_every_malformed_expression_fails_in_the_modules_own_way(self):
        for text in ("192.0.2.1-300", "abc", "192.0.2.0/33", "192.0.2", "::1", "2001:db8::/32",
                     "192.0.2.256", "192.0.2.1-", "999.1.1.1-5", "192.0.2.0/-1"):
            with self.assertRaises(luma_scanplan.ScanRangeError, msg=text):
                luma_scanplan.check(text)

    def test_a_saved_list_is_deduplicated_validated_and_never_partially_saved(self):
        self.assertEqual(luma_scanplan.normalise("198.51.100.0/24, 198.51.100.0/24\n203.0.113.1-9"),
                         ["198.51.100.0/24", "203.0.113.1-9"])
        self.assertEqual(luma_scanplan.normalise(["198.51.100.0/24", "203.0.113.5"]),
                         ["198.51.100.0/24", "203.0.113.5"])
        self.assertEqual(luma_scanplan.normalise(None), [])
        for bad in (["198.51.100.0/24", "nonsense"], [1], 5, True, {"a": 1}):
            with self.assertRaises(luma_scanplan.ScanRangeError):
                luma_scanplan.normalise(bad)


class ScanEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.probed = []
        self.app.patch("_scan_one", lambda ip: self.probed.append(ip))
        self.app.patch("_persist_units_cache", lambda: None)
        self.client = self.m.APP.test_client()
        saved = None
        try:
            with open(self.m.CONFIG_FILE, "rb") as handle:
                saved = handle.read()
        except OSError:
            pass
        self.addCleanup(self._restore_config_file, saved)

    def _restore_config_file(self, saved):
        if saved is None:
            try:
                os.remove(self.m.CONFIG_FILE)
            except OSError:
                pass
        else:
            with open(self.m.CONFIG_FILE, "wb") as handle:
                handle.write(saved)

    def scan(self, targets):
        return self.client.post("/api/scan", json={"targets": targets})

    def test_an_oversized_scan_is_refused_before_anything_is_probed(self):
        for targets in ("10.0.0.0/8", "192.0.2.0/19", "192.0.2.0/20,198.51.100.0/20,203.0.113.0/20"):
            response = self.scan(targets)
            self.assertEqual(response.status_code, 400, targets)
            self.assertIn(response.get_json()["reason"], ("range_too_large", "scan_too_large"))
            self.assertEqual(self.probed, [])

    def test_garbage_is_refused_rather_than_silently_scanning_nothing(self):
        response = self.scan("not-an-address")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason"], "invalid_range")

    def test_the_adapter_network_and_a_routed_network_are_one_scan(self):
        # Two networks that share nothing, neither required to be the adapter's own.
        response = self.scan("192.0.2.1-3,198.51.100.1-2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sorted(self.probed), ["192.0.2.1", "192.0.2.2", "192.0.2.3",
                                               "198.51.100.1", "198.51.100.2"])

    def test_a_scan_of_one_network_with_a_slash_8_in_the_expander_is_still_bounded(self):
        # The expander itself keeps its own ceiling (a second line of defence).
        self.assertEqual(len(self.m._expand_targets("10.0.0.0/8")), 4096)
        full = self.m._expand_targets("198.51.100.250-198.51.101.4")
        self.assertEqual(full[0], "198.51.100.250")
        self.assertEqual(full[-1], "198.51.101.4")
        self.assertEqual(len(full), 11)

    def test_additional_networks_are_remembered_by_the_server_and_survive_a_restart(self):
        response = self.client.post("/api/config", json={"scan_ranges": ["198.51.100.0/24", "203.0.113.1-50"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/config").get_json()["scan_ranges"],
                         ["198.51.100.0/24", "203.0.113.1-50"])
        with open(self.m.CONFIG_FILE, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["scan_ranges"], ["198.51.100.0/24", "203.0.113.1-50"])
        self.m.CONFIG["scan_ranges"] = []
        self.m._load_persisted_config()                           # what a restart does
        self.assertEqual(self.m.CONFIG["scan_ranges"], ["198.51.100.0/24", "203.0.113.1-50"])

    def test_an_unscannable_saved_value_is_refused_and_a_damaged_file_is_dropped(self):
        before = list(self.m.CONFIG.get("scan_ranges") or [])
        response = self.client.post("/api/config", json={"scan_ranges": ["10.0.0.0/8"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.m.CONFIG.get("scan_ranges") or [], before)
        with open(self.m.CONFIG_FILE, "w", encoding="utf-8") as handle:
            json.dump({"scan_ranges": ["10.0.0.0/8", "garbage"]}, handle)
        self.m._load_persisted_config()
        self.assertEqual(self.m.CONFIG["scan_ranges"], [])

    def test_the_page_scans_the_adapter_network_plus_the_additional_ones_and_keeps_both(self):
        source = _read("ui/index.html")
        compose = _function(source, "composeScanTargets")
        self.assertIn("chosen", compose)
        self.assertIn("extra", compose)
        self.assertNotIn("text||chosen", source)          # the old either/or is gone
        self.assertRegex(source, r'<label for="targets" class="small">[\s\S]*?>Targets</span></label>')   # as OmniSuite names it
        self.assertIn("Scanned together with the adapter", source)    # and says it adds to the adapter network
        self.assertIn("No adapter", source)               # a scan of only the additional ones


# ==================================================================== adapter ranking

def row(name, address, up=True, **extra):
    return dict({"name": name, "address": address, "up": up}, **extra)


class AdapterRankingTests(unittest.TestCase):
    def names(self, rows):
        return [r["name"] for r in luma_adapters.rank_adapters(rows)]

    def test_connected_physical_ethernet_beats_everything_that_is_not(self):
        rows = [row("vEthernet (Default Switch)", "198.51.100.1"),
                row("VMware Network Adapter VMnet8", "198.51.100.2"),
                row("Wi-Fi", "198.51.100.3"),
                row("Local Area Connection* 10", "198.51.100.4"),
                row("Tailscale", "198.51.100.5"),
                row("Ethernet", "192.0.2.68")]
        self.assertEqual(self.names(rows)[0], "Ethernet")
        self.assertEqual(self.names(rows)[1], "Wi-Fi")
        self.assertEqual(luma_adapters.default_adapter(rows)["name"], "Ethernet")

    def test_the_order_is_ethernet_then_wifi_then_other_physical_then_virtual(self):
        rows = [row("docker0", "198.51.100.9"), row("en0", "198.51.100.8"),
                row("wlan0", "198.51.100.7"), row("eth0", "198.51.100.6")]
        self.assertEqual(self.names(rows), ["eth0", "wlan0", "en0", "docker0"])

    def test_a_disconnected_adapter_never_outranks_a_connected_one_of_any_kind(self):
        rows = [row("Ethernet 2", "192.0.2.9", up=False), row("vEthernet (WSL)", "198.51.100.2")]
        self.assertEqual(self.names(rows)[0], "vEthernet (WSL)")

    def test_only_virtual_adapters_still_yield_a_default_rather_than_none(self):
        rows = [row("vEthernet (Default Switch)", "198.51.100.1")]
        self.assertEqual(luma_adapters.default_adapter(rows)["name"], "vEthernet (Default Switch)")

    def test_an_adapter_without_a_usable_address_is_never_the_default(self):
        rows = [row("Ethernet", "169.254.1.5"), row("Wi-Fi", "198.51.100.3")]
        self.assertEqual(luma_adapters.default_adapter(rows)["name"], "Wi-Fi")
        self.assertIsNone(luma_adapters.default_adapter([row("lo", "127.0.0.1")]))

    def test_kinds_are_recognised_across_platforms(self):
        kind = luma_adapters.adapter_kind
        self.assertEqual(kind({"name": "Ethernet 3"}), "ethernet")
        self.assertEqual(kind({"name": "enp3s0"}), "ethernet")
        self.assertEqual(kind({"name": "eno1"}), "ethernet")
        self.assertEqual(kind({"name": "Wi-Fi"}), "wifi")
        self.assertEqual(kind({"name": "wlp2s0"}), "wifi")
        self.assertEqual(kind({"name": "Wi-Fi Direct Virtual Adapter"}), "virtual")
        self.assertEqual(kind({"name": "Anything", "virtual": True}), "virtual")
        for virtual in ("tun0", "utun3", "awdl0", "br-1a2b", "virbr0", "veth123", "WireGuard Tunnel",
                        "Bluetooth Network Connection", "Loopback Pseudo-Interface 1",
                        "VirtualBox Host-Only Network", "zt5u4abcd", "tap0"):
            self.assertEqual(kind({"name": virtual}), "virtual", virtual)
        # macOS names its Wi-Fi en0; an unrecognised physical name is not guessed at.
        self.assertEqual(kind({"name": "en0"}), "other")

    def test_ranking_does_not_modify_its_input_and_is_deterministic(self):
        rows = [row("b", "198.51.100.2"), row("a", "198.51.100.1")]
        snapshot = copy.deepcopy(rows)
        first = luma_adapters.rank_adapters(rows)
        self.assertEqual(rows, snapshot)
        self.assertEqual(first, luma_adapters.rank_adapters(list(reversed(rows))))

    def test_enumeration_returns_the_ranked_list(self):
        found = [luma_adapters._entry("vEthernet (Default Switch)", "198.51.100.1", prefix=24, mac="02:00:5e:00:00:01"),
                 luma_adapters._entry("Ethernet", "192.0.2.68", prefix=24, mac="02:00:5e:00:00:02")]
        with mock.patch.object(luma_adapters.sys, "platform", "win32"), \
                mock.patch.object(luma_adapters, "_windows_powershell", lambda: copy.deepcopy(found)):
            ranked = luma_adapters.enumerate_adapters()
        self.assertEqual([a["name"] for a in ranked], ["Ethernet", "vEthernet (Default Switch)"])
        self.assertEqual(ranked[0]["kind"], "ethernet")

    def test_the_scan_adapter_list_uses_the_same_ranking(self):
        app = RealApp(self)
        rows = [{"name": "vEthernet (Default Switch)", "ip": "198.51.100.1", "cidr": "198.51.100.0/24", "scan": "x"},
                {"name": "Ethernet", "ip": "192.0.2.68", "cidr": "192.0.2.0/24", "scan": "y"}]
        self.assertEqual([r["name"] for r in app.m._sort_adapters(rows)], ["Ethernet", "vEthernet (Default Switch)"])

    def test_a_saved_selection_is_never_overridden_by_the_ranking(self):
        # The ranking only orders the list and picks when nothing was chosen.
        index = _read("ui/index.html")
        loader = _function(index, "loadAdapters")
        self.assertNotRegex(loader, r"virtual\|vethernet")                 # the old removal of a saved virtual adapter
        self.assertIn("if(savedOption) sel.value = saved", loader)
        remote = _read("ui/remote_settings.js")
        fill = _function_js(remote, "fillAdapters")
        self.assertIn("previous, state.configured_adapter", fill)           # a configured choice wins
        self.assertIn("wanted || adapters[0].id", fill)                     # only then the ranked first


# ===================================================================== HTTP default

class HttpDefaultTests(unittest.TestCase):
    ADAPTER = {"id": "adapter-one", "name": "Test Network", "address": "192.0.2.50", "mac": "02:00:5e:00:00:50"}

    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.calls = []

        def start(listener, host, port, ssl_context=None, allowed_names=()):
            self.calls.append({"host": host, "ssl": ssl_context})
            listener._server = object()
            listener._bound = (host, port)
            listener._scheme = "https" if ssl_context is not None else "http"
            return {"ok": True, "enabled": True, "host": host, "port": port, "scheme": listener._scheme,
                    "secure": ssl_context is not None, "url": "%s://%s:%s/" % (listener._scheme, host, port)}

        for patcher in (mock.patch.object(luma_remote.RemoteListener, "start", start),
                        mock.patch.object(luma_adapters, "enumerate_adapters", lambda *a, **k: [dict(self.ADAPTER)])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.s.admin("root")
        self.local = self.s.client(remote=False)

    def test_a_fresh_installation_defaults_to_http(self):
        state = self.local.get("/api/remote_access").get_json()
        self.assertEqual(state["transport"], "http")
        self.assertFalse(state["encrypted"])

    def test_http_still_needs_an_explicit_acknowledgement_in_the_request(self):
        body = {"enabled": True, "adapter_id": self.ADAPTER["id"], "transport": "http"}
        refused = self.local.post("/api/remote_access", json=body)
        self.assertEqual(refused.status_code, 400)
        self.assertTrue(refused.get_json()["needs_confirmation"])
        self.assertEqual(self.calls, [])
        # A truthy field of some other name does not count.
        self.assertEqual(self.local.post("/api/remote_access", json=dict(body, accept=True, insecure=True)).status_code, 400)
        accepted = self.local.post("/api/remote_access", json=dict(body, accept_insecure=True))
        self.assertEqual(accepted.status_code, 200)
        self.assertIsNone(self.calls[-1]["ssl"])

    def test_a_caller_that_names_no_transport_never_reaches_plaintext(self):
        # The API default stays the safe one: omission is not consent.
        response = self.local.post("/api/remote_access", json={"enabled": True, "adapter_id": self.ADAPTER["id"]})
        self.assertTrue(all(call["ssl"] is not None for call in self.calls), self.calls)
        self.assertIn(response.status_code, (200, 400, 409))

    def test_https_remains_available_and_encrypted(self):
        made = self.local.post("/api/admin/certificate", json={"action": "generate", "address": self.ADAPTER["address"]})
        self.assertEqual(made.status_code, 200, made.get_json())
        response = self.local.post("/api/remote_access", json={
            "enabled": True, "adapter_id": self.ADAPTER["id"], "transport": "https"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIsNotNone(self.calls[-1]["ssl"])
        self.assertTrue(self.local.get("/api/remote_access").get_json()["encrypted"])

    def test_the_settings_page_offers_http_first_and_says_it_is_not_encrypted(self):
        script = _read("ui/remote_settings.js")
        options = re.search(r"\[\['http',[^\]]+\],\s*\['https',[^\]]+\]\]", script)
        self.assertIsNotNone(options)
        self.assertIn("not encrypted", options.group(0))
        self.assertIn("(state.transport || 'http') === 'https'", script)
        self.assertNotIn("(state.transport || 'https')", script)


# ============================================================================ retention

def entry(age_days, now, size_pad=0, **extra):
    body = {"at": now - age_days * 86400, "event": "login.success", "actor": "alex",
            "actor_id": "u1", "detail": {"pad": "x" * size_pad}}
    body.update(extra)
    return body


class RetentionRuleTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def sizes(self, entries):
        return [luma_retention.entry_size(len(json.dumps(e, separators=(",", ":")))) for e in entries]

    def test_the_defaults_are_thirty_days_and_four_megabytes(self):
        self.assertEqual(luma_retention.defaults(), {"days": 30, "max_bytes": 4 * 1024 * 1024})

    def test_entries_older_than_the_period_are_removed(self):
        entries = [entry(45, self.NOW), entry(31, self.NOW), entry(29, self.NOW), entry(0, self.NOW)]
        kept, _ = luma_retention.prune(entries, self.sizes(entries), self.NOW, 30, 10 ** 9)
        self.assertEqual([round((self.NOW - e["at"]) / 86400) for e in kept], [29, 0])

    def test_when_the_size_limit_bites_the_oldest_go_first_and_the_newest_survive(self):
        entries = [entry(i, self.NOW, size_pad=2000, event="e%d" % i) for i in (9, 8, 7, 6, 5, 4, 3, 2, 1)]
        sizes = self.sizes(entries)
        limit = sum(sizes[-4:])
        kept, kept_sizes = luma_retention.prune(entries, sizes, self.NOW, 30, limit)
        self.assertEqual([e["event"] for e in kept], ["e4", "e3", "e2", "e1"])
        self.assertLessEqual(sum(kept_sizes), limit)

    def test_both_limits_apply_together(self):
        entries = [entry(40, self.NOW), entry(2, self.NOW, size_pad=5000), entry(1, self.NOW, size_pad=5000)]
        sizes = self.sizes(entries)
        kept, _ = luma_retention.prune(entries, sizes, self.NOW, 30, sizes[2] + 10)
        self.assertEqual(len(kept), 1)

    def test_an_entry_with_no_usable_time_is_treated_as_old_never_as_current(self):
        entries = [{"event": "x"}, {"at": "yesterday"}, {"at": True}, entry(1, self.NOW)]
        kept, _ = luma_retention.prune(entries, self.sizes(entries), self.NOW, 30, 10 ** 9)
        self.assertEqual(len(kept), 1)

    def test_the_inputs_are_not_modified(self):
        entries = [entry(40, self.NOW), entry(1, self.NOW)]
        sizes = self.sizes(entries)
        luma_retention.prune(entries, sizes, self.NOW, 30, 10 ** 9)
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(sizes), 2)

    def test_a_flood_of_tiny_entries_cannot_defeat_the_size_estimate(self):
        count = luma_retention.HARD_MAX_ENTRIES + 500
        entries = [{"at": self.NOW, "event": "e"}] * count
        kept, _ = luma_retention.prune(entries, [1] * count, self.NOW, 30, 10 ** 12)
        self.assertEqual(len(kept), luma_retention.HARD_MAX_ENTRIES)

    def test_settings_out_of_bounds_are_refused_not_clamped(self):
        bad = [(0, None), (-5, None), (366, None), ("abc", None), (True, None), (1.5, None), (float("nan"), None),
               (None, 1), (None, 100), (None, 10 ** 12), (None, "big"), (None, False), ([], None), ({}, None)]
        for days, size in bad:
            with self.assertRaises(luma_retention.RetentionError, msg=repr((days, size))):
                luma_retention.validate(days, size)

    def test_valid_settings_are_accepted_and_an_omitted_one_keeps_the_current_value(self):
        self.assertEqual(luma_retention.validate(7, 1024 * 1024), {"days": 7, "max_bytes": 1024 * 1024})
        self.assertEqual(luma_retention.validate("90", None, {"days": 30, "max_bytes": 2 * 1024 * 1024}),
                         {"days": 90, "max_bytes": 2 * 1024 * 1024})
        self.assertEqual(luma_retention.validate(365, luma_retention.MAX_MAX_BYTES)["days"], 365)

    def test_a_malformed_saved_setting_becomes_the_default_never_an_unbounded_log(self):
        for saved in (None, "x", [], {"days": "∞"}, {"days": 0}, {"days": 10 ** 9}, {"max_bytes": -1},
                      {"days": 30, "max_bytes": 10 ** 15}, {"days": True, "max_bytes": True}):
            self.assertEqual(luma_retention.from_saved(saved), luma_retention.defaults(), repr(saved))
        self.assertEqual(luma_retention.from_saved({"days": 7, "max_bytes": 512 * 1024}),
                         {"days": 7, "max_bytes": 512 * 1024})


class RetentionStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="luma-retention-")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.users = os.path.join(self.dir, "users.json")
        self.audit = os.path.join(self.dir, "audit.json")

    def store(self):
        from pathlib import Path
        return luma_users.UserStore(Path(self.users), Path(self.audit))

    def write(self, entries, retention=None):
        body = {"version": 1, "entries": entries}
        if retention is not None:
            body["retention"] = retention
        with open(self.audit, "w", encoding="utf-8") as handle:
            json.dump(body, handle)

    def test_old_entries_are_gone_when_the_log_is_loaded(self):
        now = time.time()
        self.write([entry(100, now, event="old"), entry(31, now, event="stale"), entry(1, now, event="fresh")])
        events = [e["event"] for e in self.store().audit_entries(limit=50)]
        self.assertEqual(events, ["fresh"])

    def test_a_log_that_is_too_large_is_cut_back_to_the_limit_on_load(self):
        now = time.time()
        limit = luma_retention.MIN_MAX_BYTES
        entries = [entry(0.001 * i, now, size_pad=4000, event="e%d" % i) for i in range(400, 0, -1)]
        self.write(entries, {"days": 30, "max_bytes": limit})
        store = self.store()
        info = store.audit_retention()
        self.assertLessEqual(info["used_bytes"], limit)
        self.assertGreater(info["entries"], 0)
        self.assertEqual(store.audit_entries(limit=1)[0]["event"], "e1")        # the newest survived

    def test_a_damaged_retention_block_is_the_default_and_the_log_still_loads(self):
        now = time.time()
        self.write([entry(1, now, event="kept")], {"days": "forever", "max_bytes": -1})
        store = self.store()
        self.assertEqual(store.audit_retention()["days"], 30)
        self.assertEqual([e["event"] for e in store.audit_entries(limit=5)], ["kept"])

    def test_changing_the_period_prunes_immediately_persists_and_is_itself_recorded(self):
        now = time.time()
        self.write([entry(20, now, event="twenty"), entry(2, now, event="two")])
        store = self.store()
        self.assertEqual(len(store.audit_entries(limit=10)), 2)
        result = store.set_audit_retention(days=7)
        self.assertEqual(result["days"], 7)
        events = [e["event"] for e in store.audit_entries(limit=10)]
        self.assertNotIn("twenty", events)
        self.assertIn("audit.retention_changed", events)
        reloaded = self.store()
        self.assertEqual(reloaded.audit_retention()["days"], 7)

    def test_a_refused_change_changes_nothing(self):
        store = self.store()
        before = store.audit_retention()
        for days, size in ((0, None), (400, None), (None, 5)):
            with self.assertRaises(luma_retention.RetentionError):
                store.set_audit_retention(days, size)
        self.assertEqual(store.audit_retention()["days"], before["days"])
        self.assertEqual([e["event"] for e in store.audit_entries(limit=5)], [])

    def test_the_log_never_grows_without_bound_while_it_is_written(self):
        store = self.store()
        store.set_audit_retention(days=1, max_bytes=luma_retention.MIN_MAX_BYTES)
        for index in range(3000):
            store.audit("login.failure", detail={"n": index, "pad": "z" * 200})
        info = store.audit_retention()
        self.assertLessEqual(info["used_bytes"], luma_retention.MIN_MAX_BYTES)
        self.assertEqual(store.audit_entries(limit=1)[0]["detail"]["n"], 2999)

    def test_no_secret_reaches_the_activity_log(self):
        store = self.store()
        store.audit("login.failure", detail={"username": "alex", "password": "hunter2", "token": "t0k",
                                             "nested": {"csrf_token": "c", "fallback_password": "f"}})
        text = json.dumps(store.audit_entries(limit=5))
        for secret in ("hunter2", "t0k", '"c"', '"f"'):
            self.assertNotIn(secret, text)
        self.assertIn("alex", text)


class RetentionApiTests(unittest.TestCase):
    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.s.admin("root")
        self.s.user("alex")
        self.admin = self.s.sign_in("root", ADMIN_PASSWORD)
        self.user = self.s.sign_in("alex", USER_PASSWORD)

    def post(self, client, body):
        return client.post("/api/admin/audit/retention", json=body,
                           headers={"X-LumaSuite-CSRF": client.csrf})

    def test_the_activity_list_reports_the_limits_and_what_is_in_use(self):
        retention = self.admin.get("/api/admin/audit").get_json()["retention"]
        self.assertEqual((retention["days"], retention["max_bytes"]), (30, 4 * 1024 * 1024))
        self.assertEqual(retention["defaults"], {"days": 30, "max_bytes": 4 * 1024 * 1024})
        self.assertIn("limits", retention)

    def test_an_administrator_can_change_it_and_a_restricted_user_cannot(self):
        ok = self.post(self.admin, {"days": 14, "max_bytes": 2 * 1024 * 1024})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["retention"]["days"], 14)
        refused = self.post(self.user, {"days": 365})
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self.s.store.audit_retention()["days"], 14)

    def test_out_of_bounds_values_come_back_as_a_sentence_and_change_nothing(self):
        for body in ({"days": 0}, {"days": 1000}, {"max_bytes": 12}, {"days": "x"}, {"days": True}, {}, []):
            response = self.post(self.admin, body)
            if body == {}:
                self.assertEqual(response.status_code, 200)            # nothing to change is not an error
                continue
            self.assertEqual(response.status_code, 400, body)
            self.assertTrue(response.get_json()["error"], body)
        self.assertEqual(self.s.store.audit_retention()["days"], 30)

    def test_the_change_is_in_the_activity_in_words(self):
        self.post(self.admin, {"days": 7, "max_bytes": 1024 * 1024})
        entries = self.admin.get("/api/admin/audit?event=audit.retention_changed").get_json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["label"], "Activity retention changed")
        self.assertEqual(entries[0]["text"], "Activity is now kept for 7 days, up to 1 MB.")
        self.assertEqual(entries[0]["actor"], "root")

    def test_the_new_event_has_a_label_and_is_written_through_at_once(self):
        self.assertIn("audit.retention_changed", luma_audit_text.EVENT_LABELS)
        self.assertIn("audit.retention_changed", luma_users.AUDIT_IMMEDIATE_EVENTS)


# ============================================================================ breakaway

#: video stream name -> the audio stream the same encoder transmits (they may differ)
ENCODER_AUDIO = {"alpha": "alpha-a", "bravo": "bravo-aa", "charlie": "charlie-a"}
ENCODERS = {"192.0.2.10": "alpha", "192.0.2.11": "bravo", "192.0.2.12": "charlie"}


class FakeLumaDecoder:
    """A LumaStream decoder as measured: Sets, follow semantics and the invalid-name quirk."""

    def __init__(self, video="alpha", audio=None, follow=True, ignore_video=False, ignore_audio=False,
                 known_audio=("alpha-a", "bravo-aa", "charlie-a")):
        self.video = video
        self.audio = audio if audio is not None else ENCODER_AUDIO[video]
        self.follow = follow
        self.known_audio = list(known_audio)
        self.ignore_video = ignore_video
        self.ignore_audio = ignore_audio
        self.calls = []
        self.reads = 0
        self.fail_reads = 0
        self.lose_replies = set()

    def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method.endswith(".Get"):
            self.reads += 1
            if self.fail_reads:
                self.fail_reads -= 1
                return None
        if method in self.lose_replies:
            # The write happened; only the answer was lost.
            self.lose_replies = self.lose_replies - {method} if False else self.lose_replies
            self._apply(method, params)
            return None
        return self._apply(method, params)

    def _apply(self, method, params):
        if method == "SelectVideoStream.Get":
            return {"result": {"streamname": self.video, "selecttype": 0,
                               "multiaddr": {"ipaddr": "239.70.0.1", "portnumber": 1000}}}
        if method == "SelectAudioStream.Get":
            return {"result": self._audio()}
        if method == "AudioStreamNameList.Get":
            return {"result": list(self.known_audio)}
        if method == "SelectVideoStream.Set":
            if not self.ignore_video:
                self.video = params["streamname"]
                if self.follow:
                    self.audio = ENCODER_AUDIO.get(self.video, self.audio)
            return {"result": {"streamname": self.video}}
        if method == "SelectAudioStream.Set":
            assert isinstance(params.get("followvideoenable"), bool), "the flag must always be sent explicitly"
            if not self.ignore_audio:
                self.follow = params["followvideoenable"]
                if self.follow:
                    self.audio = ENCODER_AUDIO.get(self.video, self.audio)      # the name sent is ignored
                else:
                    self.audio = params["streamname"]
            return {"result": self._audio()}
        return {"result": {}}

    #: A decoder that lists the name and still reads back no subscription: the second line of
    #: defence, which the up-front name check never lets an ordinary test reach.
    silent_audio = False

    def _audio(self):
        known = self.audio in self.known_audio or self.audio in ENCODER_AUDIO.values()
        known = known and not self.silent_audio
        return {"followvideoenable": self.follow, "streamname": self.audio, "selecttype": 0,
                "multiaddr": {"ipaddr": "239.80.0.1" if known else "0.0.0.0", "portnumber": 1000}}

    def writes(self):
        return [m for m, _ in self.calls if m.endswith(".Set")]


def encoder_rpc(ip, method, params):
    video = ENCODERS.get(ip)
    if video is None:
        return None
    if method == "VideoMultiIpStream.Get":
        return {"result": {"streamname": video}}
    if method == "AudioMultiIpStream.Get":
        return {"result": {"streamname": ENCODER_AUDIO[video]}}
    return None


def go(decoder, mode, video=None, audio=None, enc=encoder_rpc):
    return luma_breakaway.route(decoder.rpc, enc, mode, password="pw", video_encoder=video,
                                audio_encoder=audio, sleep=lambda _s: None)


class BreakawayTests(unittest.TestCase):
    def test_normal_av_changes_only_the_video_and_audio_follows_even_with_a_different_name(self):
        dec = FakeLumaDecoder(video="alpha")
        result = go(dec, "av", video="192.0.2.11")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(dec.writes(), ["SelectVideoStream.Set"])             # no audio command at all
        self.assertEqual((dec.video, dec.audio, dec.follow), ("bravo", "bravo-aa", True))   # names differ

    def test_av_that_is_already_correct_writes_nothing(self):
        dec = FakeLumaDecoder(video="alpha")
        result = go(dec, "av", video="192.0.2.10")
        self.assertTrue(result["ok"])
        self.assertEqual(dec.writes(), [])
        self.assertEqual(result["writes"], [])

    def test_returning_to_follow_is_one_audio_write_and_the_audio_resyncs_to_the_video(self):
        dec = FakeLumaDecoder(video="bravo", audio="alpha-a", follow=False)         # a breakaway in place
        result = go(dec, "av", video="192.0.2.11")
        self.assertTrue(result["ok"], result)
        self.assertEqual(dec.writes(), ["SelectAudioStream.Set"])                    # video already right
        self.assertEqual((dec.video, dec.audio, dec.follow), ("bravo", "bravo-aa", True))

    def test_av_on_a_breakaway_decoder_to_another_source_restores_follow_then_routes(self):
        dec = FakeLumaDecoder(video="bravo", audio="alpha-a", follow=False)
        result = go(dec, "av", video="192.0.2.12")
        self.assertTrue(result["ok"], result)
        self.assertEqual(dec.writes(), ["SelectAudioStream.Set", "SelectVideoStream.Set"])
        self.assertEqual((dec.video, dec.audio, dec.follow), ("charlie", "charlie-a", True))

    def test_video_only_pins_the_audio_first_and_leaves_it_alone(self):
        dec = FakeLumaDecoder(video="alpha")
        result = go(dec, "video", video="192.0.2.11")
        self.assertTrue(result["ok"], result)
        self.assertEqual(dec.writes(), ["SelectAudioStream.Set", "SelectVideoStream.Set"])
        self.assertEqual((dec.video, dec.audio, dec.follow), ("bravo", "alpha-a", False))
        self.assertEqual(dec.calls[[m for m, _ in dec.calls].index("SelectAudioStream.Set")][1]["followvideoenable"], False)

    def test_video_only_when_audio_is_already_independent_writes_the_video_alone(self):
        dec = FakeLumaDecoder(video="alpha", audio="charlie-a", follow=False)
        result = go(dec, "video", video="192.0.2.11")
        self.assertTrue(result["ok"], result)
        self.assertEqual(dec.writes(), ["SelectVideoStream.Set"])
        self.assertEqual(dec.audio, "charlie-a")

    def test_video_only_with_no_audio_to_keep_is_refused_and_nothing_is_written(self):
        dec = FakeLumaDecoder(video="alpha", audio="")
        dec.known_audio = []
        result = go(dec, "video", video="192.0.2.11")
        self.assertEqual((result["ok"], result["status"], result["reason"]), (False, "refused", "no_current_audio"))
        self.assertEqual(dec.writes(), [])

    def test_audio_only_changes_the_audio_and_leaves_the_video(self):
        dec = FakeLumaDecoder(video="alpha")
        result = go(dec, "audio", audio="192.0.2.12")
        self.assertTrue(result["ok"], result)
        self.assertEqual(dec.writes(), ["SelectAudioStream.Set"])
        self.assertEqual((dec.video, dec.audio, dec.follow), ("alpha", "charlie-a", False))

    def test_audio_only_uses_the_audio_name_the_source_reports_not_its_video_name(self):
        dec = FakeLumaDecoder(video="alpha")
        go(dec, "audio", audio="192.0.2.11")
        sent = [p for m, p in dec.calls if m == "SelectAudioStream.Set"][0]
        self.assertEqual(sent["streamname"], "bravo-aa")

    def test_an_audio_name_the_decoder_does_not_know_is_refused_before_any_write(self):
        # The device would accept it and subscribe to 0.0.0.0:0.
        dec = FakeLumaDecoder(video="alpha", known_audio=("alpha-a",))
        result = go(dec, "audio", audio="192.0.2.11")
        self.assertEqual((result["status"], result["reason"]), ("refused", "audio_not_available"))
        self.assertEqual(dec.writes(), [])

    def test_a_listed_audio_name_that_reads_back_no_address_is_not_verified_and_is_rolled_back(self):
        # Reached deliberately: the name check upstream passes, so only the read-back can refuse.
        for mode, kw in (("audio", {"audio": "192.0.2.11"}),
                         ("breakaway", {"video": "192.0.2.11", "audio": "192.0.2.12"}),
                         ("av", {"video": "192.0.2.11"})):
            dec = FakeLumaDecoder(video="alpha")
            dec.silent_audio = True
            result = go(dec, mode, **kw)
            self.assertFalse(result["ok"], mode)
            self.assertEqual((result["status"], result["reason"]), ("not_verified", "audio_not_available"), mode)
            self.assertEqual(result["rollback"], "verified", mode)
            self.assertEqual((dec.video, dec.audio, dec.follow), ("alpha", "alpha-a", True), mode)

    def test_breakaway_sends_the_audio_first_so_a_failure_leaves_the_video_alone(self):
        dec = FakeLumaDecoder(video="alpha")
        result = go(dec, "breakaway", video="192.0.2.11", audio="192.0.2.12")
        self.assertTrue(result["ok"], result)
        self.assertEqual(dec.writes(), ["SelectAudioStream.Set", "SelectVideoStream.Set"])
        self.assertEqual((dec.video, dec.audio, dec.follow), ("bravo", "charlie-a", False))

    def test_every_audio_write_carries_the_follow_flag_explicitly(self):
        for mode, kw in (("av", {"video": "192.0.2.11"}), ("video", {"video": "192.0.2.11"}),
                         ("audio", {"audio": "192.0.2.12"}), ("breakaway", {"video": "192.0.2.11", "audio": "192.0.2.12"})):
            dec = FakeLumaDecoder(video="alpha", follow=False if mode == "av" else True)
            go(dec, mode, **kw)          # the fake asserts the flag is a bool on every audio Set

    def test_a_write_the_device_accepts_but_does_not_hold_is_not_a_success_and_is_put_back(self):
        dec = FakeLumaDecoder(video="alpha", ignore_video=True)
        result = go(dec, "video", video="192.0.2.11")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_verified")
        self.assertEqual(result["rollback"], "verified")
        self.assertEqual((dec.video, dec.audio, dec.follow), ("alpha", "alpha-a", True))   # exactly as it was

    def test_a_restoration_that_cannot_be_confirmed_is_reported_as_incomplete(self):
        dec = FakeLumaDecoder(video="alpha", ignore_video=True)
        original = dec.rpc

        def stuck(method, params):
            reply = original(method, params)
            if method == "SelectAudioStream.Set":
                dec.audio, dec.follow = "charlie-a", False        # a restore that does not take
            return reply
        dec.rpc = stuck
        result = go(dec, "video", video="192.0.2.11")
        self.assertFalse(result["ok"])
        self.assertEqual(result["rollback"], "incomplete")

    def test_a_lost_write_reply_is_a_failure_with_a_rollback_never_a_blind_retry(self):
        dec = FakeLumaDecoder(video="alpha")
        dec.lose_replies = {"SelectVideoStream.Set"}
        result = go(dec, "video", video="192.0.2.11")
        self.assertEqual(result["status"], "failed")
        # The write itself was sent once; the only other video write is the restoration.
        self.assertEqual(dec.writes().count("SelectVideoStream.Set"), 2)
        self.assertEqual(result["rollback"], "verified")
        self.assertEqual((dec.video, dec.audio, dec.follow), ("alpha", "alpha-a", True))

    def test_a_decoder_that_does_not_answer_is_unreachable_and_nothing_is_written(self):
        dec = FakeLumaDecoder()
        dec.fail_reads = 100
        result = go(dec, "av", video="192.0.2.11")
        self.assertEqual(result["status"], "unreachable")
        self.assertEqual(dec.writes(), [])

    def test_reads_are_retried_up_to_three_times_and_no_more(self):
        dec = FakeLumaDecoder()
        dec.fail_reads = 100
        go(dec, "av", video="192.0.2.11")
        self.assertEqual(dec.reads, luma_breakaway.READ_ATTEMPTS)
        dec2 = FakeLumaDecoder()
        dec2.fail_reads = 2                      # two lost, third answers
        self.assertTrue(go(dec2, "av", video="192.0.2.10")["ok"])

    def test_a_source_that_does_not_answer_is_unreachable_and_nothing_is_written(self):
        dec = FakeLumaDecoder()
        result = go(dec, "av", video="198.51.100.99")
        self.assertEqual((result["status"], result["reason"]), ("unreachable", "source_unreachable"))
        self.assertEqual(dec.writes(), [])

    def test_unknown_modes_and_missing_sources_are_refused(self):
        dec = FakeLumaDecoder()
        self.assertEqual(go(dec, "teleport", video="192.0.2.10")["reason"], "unknown_mode")
        self.assertEqual(go(dec, "av")["reason"], "missing_source")
        self.assertEqual(go(dec, "audio")["reason"], "missing_source")
        self.assertEqual(go(dec, "breakaway", video="192.0.2.10")["reason"], "missing_source")
        self.assertEqual(dec.writes(), [])


class BreakawayEndpointTests(unittest.TestCase):
    DEC, ENC_A, ENC_B = "198.51.100.21", "192.0.2.10", "192.0.2.11"

    def setUp(self):
        self.app = RealApp(self)
        self.m = self.app.m
        self.device = FakeLumaDecoder(video="alpha")
        encoders = dict(ENCODERS)

        def call(ip, method, params=None, timeout=None):
            if ip == self.DEC:
                return self.device.rpc(method, params or {})
            return encoder_rpc(ip, method, params)
        self.app.patch("_ws_call_auth", call)
        self.app.patch("_persist_units_cache", lambda: None)
        for unit in (copy.deepcopy(DISPLAY_1),):
            unit["ip"] = self.DEC
            with self.m.cache_lock:
                self.m.cache_units[self.DEC] = unit
        self.client = self.m.APP.test_client()

    def route(self, **body):
        return self.client.post("/api/route_matrix", json=dict({"decoder_ip": self.DEC}, **body))

    def test_a_named_mode_reports_what_the_device_held_before_and_after_and_what_was_written(self):
        response = self.route(mode="breakaway", video_encoder_ip=self.ENC_B, audio_encoder_ip=self.ENC_A)
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body["verified"])
        self.assertEqual(body["before"]["video"], "alpha")
        self.assertEqual(body["after"]["video"], "bravo")
        self.assertEqual(body["after"]["audio"], "alpha-a")
        self.assertEqual(body["writes"], ["SelectAudioStream.Set", "SelectVideoStream.Set"])

    def test_the_cache_follows_what_the_decoder_reported(self):
        self.route(mode="video", encoder_ip=self.ENC_B)
        unit = self.app.unit(self.DEC)
        self.assertEqual(unit["dec"]["video"]["streamname"], "bravo")
        self.assertEqual(unit["dec"]["audio"]["streamname"], "alpha-a")
        self.assertFalse(unit["dec"]["audio"]["followvideoenable"])

    def test_a_route_with_no_mode_is_exactly_the_ordinary_route_it_always_was(self):
        with mock.patch.object(self.m, "_route_av", return_value={"ok": True, "status": "verified", "set_result": {}, "get_result": {}}) as ordinary, \
                mock.patch.object(self.m, "_route_breakaway") as breakaway:
            response = self.client.post("/api/route_matrix", json={"decoder_ip": self.DEC, "encoder_ip": self.ENC_A})
        self.assertEqual(response.status_code, 200)
        ordinary.assert_called_once()
        breakaway.assert_not_called()

    def test_a_failed_route_says_so_with_the_status_that_means_it(self):
        self.device.ignore_video = True
        response = self.route(mode="av", encoder_ip=self.ENC_B)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["status"], "not_verified")
        self.assertEqual(response.get_json()["rollback"], "verified")
        self.device.ignore_video = False
        self.device.fail_reads = 100
        self.assertEqual(self.route(mode="av", encoder_ip=self.ENC_B).status_code, 503)

    def test_a_refusal_is_a_400_and_writes_nothing(self):
        self.assertEqual(self.route(mode="teleport", encoder_ip=self.ENC_A).status_code, 400)
        self.assertEqual(self.route(mode="av").status_code, 400)
        response = self.route(mode="audio", encoder_ip=self.ENC_A)           # the decoder does not list that name
        self.device.known_audio = ["bravo-aa"]
        response = self.route(mode="audio", encoder_ip=self.ENC_A)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason"], "audio_not_available")

    def test_a_switcher_is_offered_audio_and_video_together_and_nothing_else(self):
        with self.m.cache_lock:
            self.m.cache_units[self.DEC]["type"] = "switcher"
        for mode in ("video", "audio", "breakaway"):
            response = self.route(mode=mode, encoder_ip=self.ENC_A, audio_encoder_ip=self.ENC_A)
            self.assertEqual(response.status_code, 400, mode)
            self.assertEqual(response.get_json()["reason"], "not_supported")
        self.assertEqual(self.device.writes(), [])
        with mock.patch.object(self.m, "_route_av", return_value={"ok": True, "status": "verified", "set_result": {}, "get_result": {}}) as ordinary:
            self.assertEqual(self.route(mode="av", encoder_ip=self.ENC_A).status_code, 200)
        ordinary.assert_called_once()

    def test_a_restricted_user_never_has_a_mode(self):
        s = System()
        self.addCleanup(s.close)
        s.turn_remote_on()
        s.admin("root")
        a, d = identity(SOURCE_A), identity(DISPLAY_1)
        s.user("alex", sources=[a], destinations=[d])
        client = s.sign_in("alex", USER_PASSWORD)
        # The route the gate protects for administrators is refused to a User outright ...
        refused = client.post("/api/route_matrix", json={"decoder_ip": "198.51.100.21", "encoder_ip": "192.0.2.10",
                                                         "mode": "audio"}, headers={"X-LumaSuite-CSRF": client.csrf})
        self.assertEqual(refused.status_code, 403)
        self.assertNotIn("route_matrix", s.hits)
        # ... and the one they do have routes audio and video together whatever the body says.
        routed = client.post("/api/user/route", json={"source": a, "destination": d, "mode": "audio",
                                                     "audio_encoder_ip": "192.0.2.11"},
                             headers={"X-LumaSuite-CSRF": client.csrf})
        self.assertEqual(routed.status_code, 200, routed.get_json())
        self.assertEqual(len(s.router.calls), 1)                              # (decoder, encoder): no mode exists to pass
        api = _read("luma_remote_api.py")
        user_route = api[api.index('"/api/user/route"'):api.index('"/api/user/route_group"')]
        # No `mode` key is ever read from the body. (The word appears in identifiers such as
        # `route_mode`, which is the engine's video-only call behind "Audio follows video"; only a
        # quoted key could be a request field.)
        self.assertIsNone(re.search(r"[\"']mode[\"']", re.sub(r"#.*", "", user_route)))
        script = _read("ui/user.js")
        self.assertIn("var body = {source: source, displays: displays, groups: groups,\n"
                      "                audio_follows_video: state.audioFollowsVideo !== false};", script)
        self.assertNotIn("audio_encoder", script)
        self.assertNotIn("video_encoder", script)


# =================================================================== firmware schedule

def hall_payload(*versions):
    return {"data": {"firmware": [{"version": [{"type": "paragraph", "text": v, "spans": []}],
                                   "release_date": "2026-04-16",
                                   "firmware_link": {"link_type": "Web", "url": "https://example.invalid/never"}}
                                  for v in versions]}}


def ok_response(payload):
    resp = mock.Mock()
    resp.status_code = 200
    resp.content = b"{}"
    resp.json = mock.Mock(return_value=payload)
    return resp


class FirmwareScheduleTests(unittest.TestCase):
    def setUp(self):
        import lumaserver
        self.m = lumaserver
        self._stop_all()
        self.m._hall_firmware_stop.clear()
        with self.m._hall_firmware_lock:
            self.m._hall_firmware_cache.clear()
            self.m._hall_firmware_fetch_state = "pending"
            self.m._hall_firmware_fetched_at = None
            self.m._hall_firmware_fetch_started = False
            self.m._hall_firmware_last_attempt = None
            self.m._hall_firmware_last_error = ""
        self._forget_file()
        with self.m.cache_lock:
            self._units = copy.deepcopy(dict(self.m.cache_units))
            self.m.cache_units.clear()
        self.addCleanup(self._cleanup)
        self.client = self.m.APP.test_client()

    def _cleanup(self):
        self._stop_all()
        self.m._hall_firmware_stop.clear()
        self._forget_file()
        with self.m.cache_lock:
            self.m.cache_units.clear()
            self.m.cache_units.update(self._units)

    def _stop_all(self):
        self.m._hall_firmware_stop.set()
        for thread in list(threading.enumerate()):
            if thread.name == "hall-firmware":
                thread.join(15)

    def _forget_file(self):
        try:
            os.remove(self.m.HALL_FIRMWARE_STATE_FILE)
        except OSError:
            pass

    def seed(self, ip, model, version):
        with self.m.cache_lock:
            self.m.cache_units[ip] = {"ip": ip, "model": model, "version": version}

    def fetch(self, *versions, error=None):
        if error is not None:
            with mock.patch.object(self.m.requests, "get", side_effect=error) as get:
                self.m._fetch_hall_firmware_catalog()
        else:
            with mock.patch.object(self.m.requests, "get", return_value=ok_response(hall_payload(*versions))) as get:
                self.m._fetch_hall_firmware_catalog()
        return get

    def status(self):
        return self.client.get("/api/firmware_status").get_json()

    # ---- when it runs
    def test_one_check_at_launch_then_every_seven_days(self):
        self.assertEqual(self.m.HALL_FIRMWARE_CHECK_INTERVAL, 7 * 24 * 3600)
        waits = []

        class StopAfterOne:
            def is_set(self):
                return len(waits) >= 1
            def wait(self, timeout=None):
                waits.append(timeout)
                return False
            def set(self): pass
            def clear(self): pass

        fetches = []
        with mock.patch.object(self.m, "_hall_firmware_stop", StopAfterOne()), \
                mock.patch.object(self.m, "_fetch_hall_firmware_catalog", lambda: fetches.append(1)):
            with self.m._hall_firmware_lock:
                self.m._hall_firmware_fetch_state = "ready"
            self.m._hall_firmware_loop()
        self.assertEqual(fetches, [1])                          # exactly one check at launch
        self.assertEqual(waits, [7 * 24 * 3600])                # and the next one a week later

    def test_a_check_that_found_nothing_is_retried_in_hours_not_in_a_week_and_never_in_a_loop(self):
        waits = []

        class StopAfterOne:
            def is_set(self): return len(waits) >= 1
            def wait(self, timeout=None):
                waits.append(timeout)
                return False

        with mock.patch.object(self.m, "_hall_firmware_stop", StopAfterOne()), \
                mock.patch.object(self.m, "_fetch_hall_firmware_catalog", lambda: None):
            with self.m._hall_firmware_lock:
                self.m._hall_firmware_fetch_state = "unavailable"
            self.m._hall_firmware_loop()
        self.assertEqual(waits, [self.m.HALL_FIRMWARE_RETRY_INTERVAL])
        self.assertGreaterEqual(self.m.HALL_FIRMWARE_RETRY_INTERVAL, 3600)
        self.assertLess(self.m.HALL_FIRMWARE_RETRY_INTERVAL, self.m.HALL_FIRMWARE_CHECK_INTERVAL)

    def test_starting_the_checks_is_idempotent_and_runs_one_background_thread(self):
        with mock.patch.object(self.m.requests, "get", return_value=ok_response(hall_payload("1.0.2"))) as get:
            self.m.start_firmware_checks()
            self.m.start_firmware_checks()
            self.m.start_firmware_checks()
            for _ in range(100):
                if self.m._hall_firmware_fetch_state != "pending":
                    break
                time.sleep(0.05)
            threads = [t for t in threading.enumerate() if t.name == "hall-firmware"]
            self.assertEqual(len(threads), 1)
            self.assertTrue(threads[0].daemon)
            self._stop_all()
        self.assertEqual(get.call_count, 3)                      # one per product, once

    def test_the_launcher_and_the_server_both_start_the_checks_and_never_at_import(self):
        self.assertIn("start_firmware_checks()", _read("app_launcher.py"))
        main = _read("lumaserver.py")
        self.assertIn("    start_firmware_checks()\n", main.replace("\r\n", "\n").split('if __name__ == "__main__":')[1])
        before = [t for t in threading.enumerate() if t.name == "hall-firmware"]
        self.assertEqual(before, [])                              # importing started nothing

    def test_the_lookup_is_bounded_https_to_the_one_host_from_a_fixed_table(self):
        self.assertTrue(self.m.HALL_FIRMWARE_API_BASE.startswith("https://hallresearch.com/api/products/"))
        self.assertEqual(self.m.HALL_FIRMWARE_FETCH_TIMEOUT, (3, 5))
        self.assertLessEqual(self.m.HALL_FIRMWARE_MAX_RESPONSE_BYTES, 5 * 1024 * 1024)
        get = self.fetch("1.0.2")
        urls = sorted(call.args[0] for call in get.call_args_list)
        self.assertEqual(urls, sorted(self.m.HALL_FIRMWARE_API_BASE + slug for _p, slug in self.m.HALL_FIRMWARE_PRODUCT_MAP))
        for call in get.call_args_list:
            self.assertEqual(call.kwargs["timeout"], (3, 5))

    # ---- persistence and offline
    def test_the_last_good_answer_is_written_and_comes_back_after_a_restart(self):
        self.fetch("1.0.3")
        with open(self.m.HALL_FIRMWARE_STATE_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["catalog"]["hw-luma-e4211"]["version"], "1.0.3")
        with self.m._hall_firmware_lock:                          # a new process knows nothing
            self.m._hall_firmware_cache.clear()
            self.m._hall_firmware_fetched_at = None
            self.m._hall_firmware_fetch_state = "pending"
        self.m._load_hall_firmware_state()
        self.seed("198.51.100.30", "HW-LUMA-E4211", "V1.0.2.3")
        self.m._hall_firmware_fetch_started = True                # no new check in this test
        body = self.status()
        self.assertEqual(body["hall_lookup"]["state"], "cached")
        self.assertEqual(body["results"]["198.51.100.30"]["state"], "update_available")
        self.assertTrue(body["results"]["198.51.100.30"]["stale"])

    def test_a_check_that_finds_nothing_keeps_the_last_good_answer_and_marks_it_stale(self):
        self.fetch("1.0.2")
        self.seed("198.51.100.31", "HW-LUMA-D4111", "1.0.1")
        self.m._hall_firmware_fetch_started = True
        self.assertFalse(self.status()["results"]["198.51.100.31"]["stale"])
        self.fetch(error=ConnectionError("offline"))
        body = self.status()
        self.assertEqual(body["hall_lookup"]["state"], "unavailable")
        self.assertEqual(body["results"]["198.51.100.31"]["state"], "update_available")    # not "Unknown"
        self.assertTrue(body["results"]["198.51.100.31"]["stale"])
        self.assertTrue(body["hall_lookup"]["error"])

    def test_a_timeout_and_a_dead_site_never_raise_and_never_blank_a_known_status(self):
        import requests
        self.fetch("1.0.2")
        self.seed("198.51.100.32", "HW-LUMA-E4211", "1.0.2")
        self.m._hall_firmware_fetch_started = True
        for error in (requests.exceptions.Timeout("slow"), requests.exceptions.ConnectionError("down"), ValueError("html")):
            self.fetch(error=error)
            self.assertEqual(self.status()["results"]["198.51.100.32"]["state"], "up_to_date")

    def test_one_product_failing_keeps_that_products_previous_answer(self):
        self.fetch("1.0.2")
        slugs = [slug for _p, slug in self.m.HALL_FIRMWARE_PRODUCT_MAP]
        calls = []

        def partial(url, timeout=None):
            calls.append(url)
            if url.endswith(slugs[0]):
                raise ConnectionError("one product down")
            return ok_response(hall_payload("1.0.9"))
        with mock.patch.object(self.m.requests, "get", side_effect=partial):
            self.m._fetch_hall_firmware_catalog()
        catalog = self.m._hall_firmware_cache
        self.assertEqual(catalog[slugs[0]]["version"], "1.0.2")           # kept
        self.assertEqual(catalog[slugs[1]]["version"], "1.0.9")           # refreshed

    def test_an_answer_older_than_a_schedule_period_is_flagged_stale(self):
        self.fetch("1.0.2")
        self.seed("198.51.100.33", "HW-LUMA-E4211", "1.0.2")
        self.m._hall_firmware_fetch_started = True
        with self.m._hall_firmware_lock:
            self.m._hall_firmware_fetched_at = time.time() - 8 * 86400
        self.assertTrue(self.status()["results"]["198.51.100.33"]["stale"])

    def test_a_damaged_remembered_file_is_ignored_never_trusted(self):
        garbage = ["not json", "[]", "{}", '{"catalog": []}', '{"catalog": {"hw-luma-e4211": "1.0.2"}}',
                   '{"catalog": {"hw-luma-e4211": {"version": 5}}}',
                   '{"catalog": {"hw-luma-e4211": {"version": "banana"}}}',
                   '{"catalog": {"unknown-slug": {"version": "9.9.9"}}}', ""]
        for text in garbage:
            with self.m._hall_firmware_lock:
                self.m._hall_firmware_cache.clear()
                self.m._hall_firmware_fetch_state = "pending"
            with open(self.m.HALL_FIRMWARE_STATE_FILE, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.m._load_hall_firmware_state()
            self.assertEqual(dict(self.m._hall_firmware_cache), {}, text)
            self.assertEqual(self.m._hall_firmware_fetch_state, "pending", text)

    # ---- what is shown
    def test_every_known_model_gets_a_real_state_never_not_applicable(self):
        self.seed("198.51.100.40", "HW-LUMA-E4211", "V1.0.2.3")
        self.seed("198.51.100.41", "HW-LUMA-D4111", "V1.0.2.6")
        self.seed("198.51.100.42", "AT-OME-CS31-SA-HDBT", "1.0.2")
        self.seed("198.51.100.43", "AT-OME-CS31-SA", "1.0.1")
        self.m._hall_firmware_fetch_started = True
        self.fetch("1.0.1", "1.0.2")
        results = self.status()["results"]
        self.assertEqual(sorted(results), ["198.51.100.40", "198.51.100.41", "198.51.100.42", "198.51.100.43"])
        self.assertEqual({k: v["state"] for k, v in results.items()},
                         {"198.51.100.40": "up_to_date", "198.51.100.41": "up_to_date",
                          "198.51.100.42": "up_to_date", "198.51.100.43": "update_available"})

    def test_an_offline_first_check_is_a_truthful_unknown_with_a_reason_for_known_models_only(self):
        self.seed("198.51.100.44", "AT-OME-CS31-SA-HDBT", "1.0.2")
        self.seed("198.51.100.45", "SOME-OTHER-BOX", "1.0")
        self.m._hall_firmware_fetch_started = True
        self.fetch(error=ConnectionError("offline"))
        results = self.status()["results"]
        self.assertEqual(results["198.51.100.44"]["state"], "unknown")
        self.assertEqual(results["198.51.100.44"]["reason"], "published_version_unavailable")
        self.assertNotIn("198.51.100.45", results)           # a model the table does not cover says nothing

    def test_versions_compare_numerically_and_an_unreadable_one_is_unknown_not_an_update(self):
        cases = [("V1.0.2.3", "1.0.2", "up_to_date"), ("1.0.10", "1.0.9", "up_to_date"),
                 ("1.0.9", "1.0.10", "update_available"), ("2.0.9", "2.0.10", "update_available"),
                 ("1.0.2", "1.0.2", "up_to_date"), ("garbage", "1.0.2", "unknown")]
        for installed, published, expected in cases:
            with self.m._hall_firmware_lock:
                self.m._hall_firmware_cache.clear()
            self.seed("198.51.100.50", "HW-LUMA-D4111", installed)
            self.m._hall_firmware_fetch_started = True
            self.fetch(published)
            self.assertEqual(self.status()["results"]["198.51.100.50"]["state"], expected, (installed, published))

    def test_checking_availability_never_installs_anything(self):
        self.seed("198.51.100.51", "HW-LUMA-E4211", "1.0.0")
        self.m._hall_firmware_fetch_started = True
        with mock.patch.object(self.m, "_ws_call_auth", side_effect=AssertionError("no device I/O")), \
                mock.patch.object(self.m, "_ws_trigger_upgrade", side_effect=AssertionError("never upgrades")), \
                mock.patch.object(self.m.requests, "post", side_effect=AssertionError("never posts")):
            self.fetch("9.9.9")
            self.assertEqual(self.status()["results"]["198.51.100.51"]["state"], "update_available")
        source = _read("lumaserver.py")
        checker = source[source.index("def _fetch_hall_firmware_catalog"):source.index("def _save_hall_firmware_state")]
        code = re.sub(r"#.*", "", re.sub(r'""".*?"""', "", checker, flags=re.S))
        self.assertNotIn("firmware_link", code)          # a Hall-supplied URL is never read

    def test_the_local_files_check_button_is_gone_and_the_upgrade_workflow_is_not(self):
        page = _read("ui/index.html")
        self.assertNotIn("check_firmware_sel", page)
        self.assertNotIn("Check Local Firmware Files", page)
        self.assertNotIn("checkFirmwareSelected", page)
        for kept in ('id="files"', 'id="upgrade_sel"', "async function upgradeSelected"):
            self.assertIn(kept, page)

    def test_the_badge_says_up_to_date_available_or_unknown_and_explains_staleness(self):
        page = _read("ui/index.html")
        badge = _function(page, "renderHallFirmwareBadge")
        for word in ('"Available"', '"Up to Date"', '"Unknown"', "result.stale", "N/A"):
            if word == "N/A":
                self.assertNotIn(word, badge)
            else:
                self.assertIn(word, badge)


# ============================================================ session facts for the shell

class SessionFactTests(unittest.TestCase):
    def test_the_session_tells_the_shell_whether_remote_access_is_on(self):
        s = System()
        self.addCleanup(s.close)
        s.admin("root")
        local = s.client(remote=False)
        self.assertFalse(local.get("/api/auth/session").get_json()["remote_enabled"])
        s.turn_remote_on()
        self.assertTrue(local.get("/api/auth/session").get_json()["remote_enabled"])
        admin = s.sign_in("root", ADMIN_PASSWORD)
        body = admin.get("/api/auth/session").get_json()
        self.assertTrue(body["remote_enabled"])
        self.assertEqual(body["role"], "admin")

    def test_the_session_carries_no_permission_or_address(self):
        s = System()
        self.addCleanup(s.close)
        s.turn_remote_on()
        s.admin("root")
        client = s.sign_in("root", ADMIN_PASSWORD)
        text = json.dumps(client.get("/api/auth/session").get_json())
        self.assertNotIn("192.0.2.", text)
        self.assertNotIn("permissions", text)


class DirectAccessLifecycleTests(unittest.TestCase):
    """The administrator can enable, see, regenerate and disable one link per account."""

    def setUp(self):
        self.s = System()
        self.addCleanup(self.s.close)
        self.s.turn_remote_on()
        self.boss = self.s.admin("root")
        self.account = self.s.user("alex")
        self.admin = self.s.sign_in("root", ADMIN_PASSWORD)

    def act(self, action):
        return self.admin.post("/api/admin/users/%s/direct_access" % self.account["id"], json={"action": action},
                               headers={"X-LumaSuite-CSRF": self.admin.csrf})

    def link(self):
        return self.admin.get("/api/admin/users/%s/direct_access" % self.account["id"]).get_json()

    def token_of(self, body):
        return body["link"].rsplit("/", 1)[-1]

    def test_off_until_it_is_turned_on(self):
        body = self.link()
        self.assertFalse(body["direct_access"]["enabled"])
        self.assertEqual(body["link"], "")

    def test_enable_shows_the_link_again_later_and_there_is_exactly_one(self):
        first = self.act("enable").get_json()
        self.assertTrue(first["direct_access"]["enabled"])
        self.assertTrue(first["link"])
        self.assertEqual(self.link()["link"], first["link"])          # copyable a month later

    def test_regenerate_replaces_the_link_and_the_old_one_stops_working_at_once(self):
        first = self.token_of(self.act("enable").get_json())
        second = self.token_of(self.act("regenerate").get_json())
        self.assertNotEqual(first, second)
        self.assertIsNone(self.s.store.resolve_direct_token(first))
        self.assertIsNotNone(self.s.store.resolve_direct_token(second))
        self.assertEqual(self.s.client().get("/access/" + first).status_code, 404)

    def test_disable_ends_it_and_the_link_is_gone(self):
        token = self.token_of(self.act("enable").get_json())
        body = self.act("disable").get_json()
        self.assertFalse(body["direct_access"]["enabled"])
        self.assertEqual(body["link"], "")
        self.assertIsNone(self.s.store.resolve_direct_token(token))

    def test_an_administrator_account_never_gets_one(self):
        response = self.admin.post("/api/admin/users/%s/direct_access" % self.boss["id"], json={"action": "enable"},
                                   headers={"X-LumaSuite-CSRF": self.admin.csrf})
        self.assertEqual(response.status_code, 400)

    def test_the_link_opens_only_the_restricted_controls_never_the_admin_api(self):
        token = self.token_of(self.act("enable").get_json())
        client = self.s.client()
        client.get("/access/" + token)
        self.assertEqual(client.get("/api/admin/users").status_code, 403)
        self.assertEqual(client.get("/api/auth/session").get_json()["method"], "direct_link")


# ================================================================= clear units, nav, theme

class ClearUnitsTests(unittest.TestCase):
    def test_opening_the_page_asks_nothing_about_clearing_units(self):
        page = _read("ui/index.html")
        for gone in ("Clear Units For Optimal Performance", "clear_units_prompt_seen", "CLEAR_UNITS_PROMPT_SESSION_KEY",
                     "shouldPromptClearUnits", "Clear cached units before scanning"):
            self.assertNotIn(gone, page)
        start = page[page.index("applyTheme();\n  await Promise.allSettled"):]
        init = start[:start.index("refreshHallFirmwareStatus();")]
        self.assertNotIn("lumaConfirm", init)
        self.assertNotIn("sessionStorage", init)

    def test_clear_units_is_still_an_intentional_action_with_its_own_confirmation(self):
        page = _read("ui/index.html")
        self.assertIn('id="clear_units"', page)
        # The question and the request live in one shared module (Device Manager and the Remote
        # Access page both call it); the page hands over its request helper and redraws.
        handler = page[page.index('$("#clear_units").addEventListener'):][:2500]
        handler = handler[:handler.index("// Targets input clear button")] if "// Targets input" in handler else handler
        self.assertIn("lumaClearUnits.run", handler)
        self.assertNotIn("lumaConfirm", handler)           # no second copy of the question
        self.assertNotIn("keptNote(", handler)
        module = re.sub(r"^\s*//.*$", "", _read("ui/clear_units.js"), flags=re.M)
        module = re.sub(r'"\s*\+\s*"', "", module)         # adjacent string literals are one sentence
        self.assertIn("/api/clear_units", module)
        self.assertIn("Clear discovered units?", module)
        self.assertIn("removes their assignments from users and display groups", module)
        self.assertIn('cancelText: "Cancel"', module)
        self.assertIn("Clear Unassigned Units", module)
        self.assertIn("View Assignments", module)
        self.assertIn('<script src="/ui/clear_units.js"></script>', page)


# ----------------------------------------------------------------------------- helpers

def _read(rel):
    with open(os.path.join(_support.ROOT, rel), encoding="utf-8") as handle:
        return handle.read().replace("\r\n", "\n")


def _function(source, name):
    """The text of `function name(...) { ... }` / `async function name`, balanced, comments stripped."""
    at = re.search(r"(?:async\s+)?function\s+%s\s*\(" % re.escape(name), source)
    if not at:
        raise AssertionError("function %s not found" % name)
    return _balanced(source, at.start())


def _function_js(source, name):
    return _function(source, name)


def _balanced(source, start):
    open_at = source.index("{", start)
    depth = 0
    for i in range(open_at, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return re.sub(r"^\s*//.*$", "", source[start:i + 1], flags=re.M)
    raise AssertionError("unbalanced")


class MatrixButtonRoleTests(unittest.TestCase):
    """A bare button in matrix.css takes the accent fill; utilities must say secondary."""

    def test_refresh_and_filter_clear_are_secondary(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "matrix.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        for element_id in ("refreshBtn", "encFilterClearBtn", "decFilterClearBtn"):
            tag = re.search(r"<button[^>]*id=\"%s\"[^>]*>" % element_id, html)
            self.assertIsNotNone(tag, element_id)
            self.assertRegex(tag.group(0), r'class="[^"]*\bsecondary\b', element_id)


class PageButtonRoleTests(unittest.TestCase):
    """Wall and Producer pages: utilities are secondary, no literal role colours."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_producer_refresh_is_secondary(self):
        tag = re.search(r"<button[^>]*id=\"refreshBtn\"[^>]*>", self._read("ui", "producer.html"))
        self.assertIsNotNone(tag)
        self.assertRegex(tag.group(0), r'class="[^"]*\bsecondary\b')

    def test_producer_icon_only_close_has_an_accessible_name(self):
        self.assertRegex(self._read("ui", "producer.html"),
                         r'<button onclick="closeFloatingPreview\(\)"\s+aria-label="Close preview"')

    def test_legacy_secondary_class_is_the_family_role(self):
        css = self._read("ui", "family.css")
        start = css.index(".btn.secondary")
        self.assertIn("button.btn-secondary", css[start:css.index("{", start)])

    def test_wall_has_no_literal_danger_colour_rule(self):
        self.assertNotRegex(self._read("ui", "wall.html"), r"\.btn-danger\s*\{[^}]*#")


class AccountEditorOrderTests(unittest.TestCase):
    def test_role_then_username_then_password_then_direct_access(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "admin_users.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        order = [html.index('id="%s"' % k) for k in ("u_role", "u_name", "u_password", "direct_section", "assign_sources")]
        self.assertEqual(order, sorted(order))


if __name__ == "__main__":
    unittest.main()
