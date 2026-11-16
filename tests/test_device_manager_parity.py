"""Device Manager parity with OmniSuite's Device Info: the shared table assets, the column order,
the frozen identity columns, an opaque sticky header, the count bubbles, the Blink control and
the firmware-folder action.

Nothing here reaches a device or the network. The one host-acting route has its opener replaced
for the span of each test, and the hardware fence is in force regardless.
"""
import os
import re
import tempfile
import unittest
from unittest import mock

import _support
from _support import RealApp

import luma_access

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def _no_comments(text):
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    return re.sub(r"<!--[\s\S]*?-->", "", text)


class SharedTableAssetTests(unittest.TestCase):
    def test_the_page_loads_the_shared_table_modules(self):
        html = _read("ui/index.html")
        for needle in ('href="/ui/device-table.css', 'src="/ui/sticky-columns.js',
                       'src="/ui/mirror-scroll.js', 'src="/ui/device-table-chrome.js'):
            self.assertIn(needle, html)
        for name in ("device-table.css", "sticky-columns.js", "mirror-scroll.js", "device-table-chrome.js"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, "ui", name)), name)

    def test_the_table_is_a_device_table_with_three_frozen_columns_in_a_mirrored_scroller(self):
        html = _no_comments(_read("ui/index.html"))
        self.assertRegex(html, r'<table id="tbl" class="device-table" data-sticky-cols="3">')
        wrapper = re.search(r'<div class="table-wrapper" id="units_table_wrapper"([^>]*)>', html)
        self.assertTrue(wrapper)
        self.assertIn("data-mirror-scroll", wrapper.group(1))
        self.assertIn('data-mirror-strip="table_scroll_mirror"', wrapper.group(1))
        self.assertIn('id="table_scroll_mirror"', html)

    def test_columns_follow_omni_where_they_overlap(self):
        html = _no_comments(_read("ui/index.html"))
        head = re.search(r"<thead>([\s\S]*?)</thead>", html).group(1)
        labels = [re.sub(r"<[^>]+>", "", t).strip() for t in re.findall(r"<th[^>]*>([\s\S]*?)</th>", head)]
        self.assertEqual(labels[:5], ["", "IP", "Hostname", "MAC", "Link Speed"])
        self.assertLess(labels.index("Link Speed"), labels.index("Timezone"))

    def test_the_row_builder_emits_cells_in_the_header_order(self):
        html = _read("ui/index.html")
        self.assertIn("const row=[tdSel,tdIP,tdHost,tdMAC,tdLink,tdTimezone,", html)

    def test_the_sticky_header_and_frozen_cells_are_painted_opaque_from_tokens(self):
        css = _no_comments(_read("ui/device-table.css"))
        head = re.search(r"table\.device-table thead th \{[^}]*\}", css).group(0)
        self.assertIn("position: sticky", head)
        self.assertIn("background-color: var(--surface)", head)
        self.assertIn("linear-gradient(var(--surface-elevated)", head)
        corner = re.search(r"table\.device-table thead th:nth-child\(1\),[\s\S]*?\}", css).group(0)
        self.assertIn("z-index: 4", corner)               # the corner outranks header (3) and frozen cell (2)
        self.assertNotRegex(head, r"#[0-9a-fA-F]{3,8}\b")  # no literal colour, so every preset follows

    def test_no_header_cell_is_dimmed_with_opacity(self):
        # Opacity on a sticky cell makes the cell, background included, translucent, and the
        # cells travelling underneath show through it.
        css = _no_comments(_read("ui/index.html"))
        for rule in re.findall(r"(th[^{}]*)\{([^}]*)\}", css):
            if "opacity" in rule[1]:
                self.fail("a table header rule sets opacity: %s { %s }" % (rule[0].strip(), rule[1].strip()))
        js = _read("ui/index.html")
        self.assertNotIn("th.style.opacity", js)

    def test_the_banners_inside_the_scroller_stay_in_view_when_it_scrolls_sideways(self):
        css = _no_comments(_read("ui/index.html"))
        rule = re.search(r"\.table-wrapper > \.box \{([^}]*)\}", css).group(1)
        self.assertIn("position:sticky", rule.replace(" ", ""))
        self.assertIn("left:0", rule.replace(" ", ""))


class CountBubbleTests(unittest.TestCase):
    def test_counts_are_pills_with_a_type_and_a_number_and_no_sentence(self):
        html = _no_comments(_read("ui/index.html"))
        for ident, label in (("encoder_count", "Encoders"), ("decoder_count", "Decoders"), ("switcher_count", "Switchers")):
            self.assertRegex(html, r'<span class="unit-count-pill"[^>]*>%s <strong id="%s">0</strong></span>' % (label, ident))
        self.assertNotIn("Units discovered", html)

    def test_a_hidden_pill_really_is_hidden(self):
        css = _no_comments(_read("ui/index.html"))
        self.assertRegex(css, r"\[hidden\]\s*\{\s*display:\s*none\s*!important")

    def test_the_totals_function_writes_the_bubbles_and_hides_an_empty_switcher_pill(self):
        html = _read("ui/index.html")
        at = html.index("function updateTotals(")
        body = html[at:html.index("// Poll encoder RTMP status", at)]
        for needle in ("'#encoder_count'", "'#decoder_count'", "'#switcher_count'", "pill.hidden = !swt"):
            self.assertIn(needle, body)
        self.assertNotIn("scan_status", body)


class BlinkControlTests(unittest.TestCase):
    def test_blink_is_omnis_action_button_state_for_state(self):
        css = _no_comments(_read("ui/index.html"))
        base = re.search(r"\.btn-blink \{([^}]*)\}", css).group(1)
        compact = base.replace(" ", "")
        for needle in ("background:transparent", "border:none", "width:32px", "height:32px", "border-radius:50%",
                       "font-size:16px", "padding:0"):
            self.assertIn(needle, compact, needle)
        self.assertRegex(css, r"\.btn-blink:hover:not\(:disabled\) \{[^}]*background:var\(--accent\)")
        self.assertRegex(css, r"\.btn-blink:active:not\(:disabled\)")
        self.assertRegex(css, r"\.btn-blink:focus-visible \{[^}]*outline:2px solid var\(--focus-ring\)")
        self.assertRegex(css, r"\.btn-blink:disabled")

    def test_the_blink_rules_use_no_literal_colour(self):
        css = _no_comments(_read("ui/index.html"))
        rules = "".join(re.findall(r"\.btn-blink[^{]*\{[^}]*\}", css))
        self.assertNotRegex(rules, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(rules, r"rgba?\(\s*239")

    def test_the_cs31_active_state_is_a_class_not_an_inline_colour(self):
        html = _read("ui/index.html")
        self.assertIn("btn.classList.add('is-on')", html)
        self.assertNotIn("btn.style.background", html)


class FirmwareFolderTests(unittest.TestCase):
    def setUp(self):
        self.app = RealApp(self)
        self.opened = []
        self.app.patch("_open_folder_on_host", lambda path: self.opened.append(path))
        self.client = self.app.m.APP.test_client()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def _post(self):
        return self.client.post("/api/open_firmware_folder", json={}, headers={"Origin": "http://localhost"})

    def test_the_selected_folder_is_opened_and_named_back(self):
        self.app.m.CONFIG["firmware_path"] = self.dir
        response = self._post()
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["path"], os.path.abspath(self.dir))
        self.assertEqual(self.opened, [os.path.abspath(self.dir)])

    def test_nothing_selected_opens_nothing(self):
        for value in (None, "", "   "):
            self.app.m.CONFIG["firmware_path"] = value
            response = self._post()
            self.assertEqual(response.status_code, 400, value)
            self.assertIn("No firmware folder is selected", response.get_json()["error"])
        self.assertEqual(self.opened, [])

    def test_a_drive_root_is_not_a_firmware_folder(self):
        # The stock default is a bare drive letter. Opening it would show an arbitrary part of the disk.
        root = os.path.abspath(os.sep)
        self.app.m.CONFIG["firmware_path"] = root
        self.assertEqual(self._post().status_code, 400)
        self.assertEqual(self.opened, [])

    def test_a_folder_that_does_not_exist_is_reported_not_opened(self):
        self.app.m.CONFIG["firmware_path"] = os.path.join(self.dir, "missing")
        response = self._post()
        self.assertEqual(response.status_code, 400)
        self.assertIn("does not exist", response.get_json()["error"])
        self.assertEqual(self.opened, [])

    def test_an_opener_that_fails_is_a_500_that_names_no_path(self):
        self.app.m.CONFIG["firmware_path"] = self.dir
        self.app.patch("_open_folder_on_host", mock.Mock(side_effect=OSError(self.dir)))
        response = self._post()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(self.dir, response.get_data(as_text=True))

    def test_a_remote_request_cannot_open_a_folder_on_the_host(self):
        self.app.m.CONFIG["firmware_path"] = self.dir
        remote = self.app.m.APP.test_client()
        remote.environ_base["luma.listener"] = "remote"
        response = remote.post("/api/open_firmware_folder", json={}, headers={"Origin": "http://localhost"})
        self.assertIn(response.status_code, (401, 403, 404), response.get_data(as_text=True)[:80])
        self.assertEqual(self.opened, [], "nothing may launch for a remote caller")

    def test_blink_says_sent_and_not_verified(self):
        html = _read("ui/index.html")
        self.assertIn("btn.title = 'Blink command sent'", html)
        self.assertNotRegex(html, r"(?i)blink[^\n]{0,40}verified")

    def test_opening_a_folder_on_the_host_is_a_local_only_route(self):
        self.assertIs(luma_access.ROUTES["/api/open_firmware_folder"], luma_access.Access.LOCAL_ONLY)

    def test_the_page_calls_the_route_the_server_has(self):
        html = _read("ui/index.html")
        self.assertIn('fetch("/api/open_firmware_folder", {method:"POST"})', html)
        rules = {r.rule for r in self.app.m.APP.url_map.iter_rules()}
        self.assertIn("/api/open_firmware_folder", rules)


class FirmwareCheckPolicyTests(unittest.TestCase):
    """Decided: a check at every launch, then every seven days while running, and no switch."""

    def setUp(self):
        import lumaserver
        self.m = lumaserver
        with self.m._hall_firmware_lock:
            self._saved = (self.m._hall_firmware_fetched_at, self.m._hall_firmware_fetch_state)
        self.addCleanup(self._restore)

    def _restore(self):
        with self.m._hall_firmware_lock:
            self.m._hall_firmware_fetched_at, self.m._hall_firmware_fetch_state = self._saved

    def test_a_recent_success_does_not_skip_the_launch_check(self):
        import time as _time
        waits, fetches = [], []

        class StopAfterOne:
            def is_set(self):
                return len(waits) >= 1
            def wait(self, timeout=None):
                waits.append(timeout)
                return False
            def set(self): pass
            def clear(self): pass

        with self.m._hall_firmware_lock:
            self.m._hall_firmware_fetched_at = _time.time() - 60      # answered a minute ago, last run
            self.m._hall_firmware_fetch_state = "cached"
        with mock.patch.object(self.m, "_hall_firmware_stop", StopAfterOne()),                 mock.patch.object(self.m, "_fetch_hall_firmware_catalog", lambda: fetches.append(1)):
            self.m._hall_firmware_loop()
        self.assertEqual(fetches, [1], "launch checks regardless of when the last success was")

    def test_the_interval_is_seven_days_and_there_is_no_way_to_turn_the_check_off(self):
        self.assertEqual(self.m.HALL_FIRMWARE_CHECK_INTERVAL, 7 * 24 * 3600)
        source = _no_comments(_read("lumaserver.py"))
        loop = source[source.index("def _hall_firmware_loop"):source.index("def _ensure_hall_firmware_lookup_started")]
        self.assertNotIn("os.environ", loop)
        self.assertNotIn("CONFIG", loop)
        self.assertNotRegex(source, r"(?i)os\.environ[^\n]*FIRMWARE_CHECK")
        for page in ("ui/settings.js", "ui/index.html"):
            text = _read(page)
            self.assertNotRegex(text, r'(?i)id="[^"]*(firmware_check|check_firmware|firmware_auto)[^"]*"[^>]*type="checkbox"')
            self.assertNotRegex(text, r"(?i)(disable|turn off|stop)[^<\n]{0,30}firmware (check|status)")


class ButtonRoleTests(unittest.TestCase):
    def test_the_primary_button_reads_its_text_from_the_on_accent_token(self):
        # color:var(--fg) is near-white on every accent; the preset system computes the readable colour.
        css = _no_comments(_read("ui/index.html"))
        rule = re.search(r"\.btn \{([^}]*)\}", css).group(1)
        self.assertIn("background:var(--action-primary,var(--accent))", rule.replace(" ", ""))
        self.assertIn("color:var(--on-accent,var(--fg))", rule.replace(" ", ""))

    def test_destructive_actions_are_never_the_accent(self):
        html = _no_comments(_read("ui/index.html"))
        for ident in ("clear_units", "remove_sel", "reboot_sel"):
            self.assertRegex(html, r'<button class="btn danger" id="%s"' % ident)
        self.assertRegex(html, r'<button class="btn danger" id="reboot_sel"')
        for ident in ("password_sync_btn", "ntp_sync_btn", "slate_manager_btn", "export_units"):
            self.assertRegex(html, r'<button class="btn secondary" id="%s"' % ident)


class PageTitleTests(unittest.TestCase):
    """Each page names itself, as Omni's do, so the tab and the header agree."""

    PAGES = {
        "ui/index.html": "LumaSuite Device Manager",
        "ui/matrix.html": "LumaSuite Matrix Routing",
        "ui/producer.html": "LumaSuite Production Studio",
        "ui/wall.html": "LumaSuite Video Wall",
        "ui/admin_users.html": "LumaSuite Remote Access",
    }

    def test_every_page_header_names_its_own_page(self):
        for path, title in self.PAGES.items():
            html = _no_comments(_read(path))
            self.assertRegex(html, r'<h1 class="title"[^>]*>%s</h1>' % title, path)

    def test_headers_are_not_all_the_same(self):
        self.assertEqual(len(set(self.PAGES.values())), len(self.PAGES))

    def test_tab_titles_follow_the_page(self):
        for path, name in (("ui/index.html", "Device Manager"), ("ui/matrix.html", "Matrix Routing"),
                           ("ui/producer.html", "Production Studio"), ("ui/wall.html", "Video Wall")):
            self.assertIn("<title>%s - LumaSuite</title>" % name, _read(path), path)


class ReadableLinkColourTests(unittest.TestCase):
    """Found live: a fixed pale blue (#93c5fd) measured 1.70:1 (IP links) and 1.59:1 (count numbers)
    on the light theme. The colour is now derived from the preset tokens, mixed toward the text colour,
    so it stays readable in both appearances and under every preset."""

    def _rule(self, selector):
        css = _no_comments(_read("ui/index.html"))
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        self.assertIsNotNone(match, selector)
        return match.group(1)

    def test_ip_link_and_count_numbers_use_tokens_not_a_fixed_blue(self):
        for selector in ("a.ip", ".unit-count-pill strong"):
            body = self._rule(selector)
            self.assertNotRegex(body, r"#[0-9a-fA-F]{3,8}", selector)
            self.assertIn("var(--accent)", body, selector)
            self.assertIn("var(--fg)", body, selector)

    def test_the_mix_sums_to_one_hundred_percent(self):
        for selector in ("a.ip", ".unit-count-pill strong"):
            parts = re.findall(r"(\d+)%", self._rule(selector))
            self.assertEqual(sum(int(x) for x in parts), 100, selector)


if __name__ == "__main__":
    unittest.main()
