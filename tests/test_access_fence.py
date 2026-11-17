"""Route classification: every route is classified, and an unclassified one is denied."""
import os
import re
import unittest

import _support  # noqa: F401  (installs the hardware fence and scratch data dir first)

import lumaserver
import luma_access
from luma_access import Access

ROOT = _support.ROOT
UI = os.path.join(ROOT, "ui")

RESTRICTIVENESS = {Access.PUBLIC: 0, Access.AUTHENTICATED: 1, Access.USER: 2,
                   Access.ADMIN: 3, Access.LOCAL_ONLY: 4}


def registered_rules():
    return {r.rule for r in lumaserver.APP.url_map.iter_rules()}


class RouteClassificationFenceTests(unittest.TestCase):
    def test_every_registered_route_is_classified(self):
        missing = sorted(r for r in registered_rules() if r not in luma_access.ROUTES)
        self.assertEqual(missing, [], "unclassified routes are denied at runtime and fail here")

    def test_the_table_does_not_describe_routes_that_do_not_exist(self):
        stale = sorted(r for r in luma_access.ROUTES if r not in registered_rules())
        self.assertEqual(stale, [])

    def test_every_value_is_an_access_class(self):
        for rule, access in luma_access.ROUTES.items():
            self.assertIsInstance(access, Access, rule)

    def test_an_unclassified_route_denies_by_default(self):
        self.assertIs(luma_access.classify("/api/a-route-nobody-classified"), Access.LOCAL_ONLY)
        self.assertIs(luma_access.classify(None), Access.LOCAL_ONLY)
        self.assertIs(luma_access.classify(""), Access.LOCAL_ONLY)

    def test_a_method_override_only_ever_narrows(self):
        for (method, rule), override in luma_access.METHOD_ROUTES.items():
            base = luma_access.ROUTES[rule]
            self.assertGreaterEqual(RESTRICTIVENESS[override], RESTRICTIVENESS[base],
                                    "%s %s widens %s to %s" % (method, rule, base, override))

    def test_user_routes_are_exactly_the_projected_endpoints(self):
        user = sorted(r for r, a in luma_access.ROUTES.items() if a is Access.USER)
        self.assertEqual(user, ["/api/user/matrix", "/api/user/preview", "/api/user/revision", "/api/user/route",
                                "/api/user/route_check", "/api/user/route_group", "/api/user/route_multi",
                                "/api/user/state"])
        for rule in user:
            self.assertTrue(rule.startswith("/api/user/"), rule)

    def test_host_acting_routes_are_local_only(self):
        for rule in ("/api/list_dir", "/api/open_firmware_folder", "/api/bootstrap/admin", "/api/bootstrap/state",
                     "/api/backup/export", "/api/backup/inspect", "/api/backup/restore",
                     "/api/local_port", "/api/local_port/<action>"):
            self.assertIs(luma_access.ROUTES[rule], Access.LOCAL_ONLY, rule)

    def test_the_public_surface_is_exactly_what_signing_in_needs(self):
        public = sorted(r for r, a in luma_access.ROUTES.items() if a is Access.PUBLIC)
        self.assertEqual(public, ["/access/<token>", "/api/auth/login", "/favicon.ico", "/license",
                                  "/licenses/<path:name>", "/login", "/static/<path:filename>"])

    def test_every_device_mutating_route_is_administrative_or_local(self):
        for rule in ("/api/route_matrix", "/api/reset", "/api/set_hostname", "/api/set_streamname",
                     "/api/set_hdcp", "/api/set_dante_power", "/api/set_ntp", "/api/upgrade",
                     "/api/producer/jsonrpc", "/api/producer/change_password",
                     "/api/video_wall/source", "/api/video_wall/apply", "/api/config",
                     "/api/clear_units", "/api/remove_units", "/api/scan"):
            self.assertIn(luma_access.ROUTES[rule], (Access.ADMIN, Access.LOCAL_ONLY), rule)


class UiFileClassificationTests(unittest.TestCase):
    def test_only_what_sign_in_and_the_routing_page_need_is_public(self):
        for name in ("family.css", "theme-boot.js", "dialogs.js", "csrf.js", "remote.css",
                     "login.js", "user.js", "remember.js", "favicon.ico",
                     "companylogo.png", "companylogo-light.png"):
            self.assertIs(luma_access.classify_ui_file(name), Access.PUBLIC, name)

    def test_operator_pages_and_scripts_are_administrative(self):
        for name in ("index.html", "matrix.html", "wall.html", "producer.html", "manual.html",
                     "settings.js", "matrix.js", "wall.js", "producer.js", "admin_users.html",
                     "admin_users.js", "remote_settings.js", "adminnav.js", "login.html",
                     "user.html", "LumaSuite_Producer_FINAL_BUILD.zip", "matrix.css"):
            self.assertIs(luma_access.classify_ui_file(name), Access.ADMIN, name)

    def test_a_file_added_tomorrow_is_administrative(self):
        self.assertIs(luma_access.classify_ui_file("new-page.html"), Access.ADMIN)

    def test_a_path_with_separators_or_parent_references_is_administrative(self):
        # The dangerous ones carry a public image prefix and a public image suffix around the
        # traversal: without the separator checks they would be taken for a logo.
        for name in ("../user.js", "a/../user.js", "imagestream/user.js", "logos\\x.png",
                     "..", "user.js\x00.html", "", None, "companylogo/../../index.html",
                     "companylogo/../secret.png", "companylogo\\..\\secret.png",
                     "favicon/../../secret.ico", "companylogo..png", "companylogo\x00.png",
                     "companylogo/x.png", "favicon\\x.ico"):
            self.assertIs(luma_access.classify_ui_file(name), Access.ADMIN, repr(name))

    def test_an_image_prefix_does_not_make_any_file_public(self):
        self.assertIs(luma_access.classify_ui_file("companylogo.html"), Access.ADMIN)
        self.assertIs(luma_access.classify_ui_file("companylogo.js"), Access.ADMIN)

    def test_every_public_file_actually_exists(self):
        for name in luma_access.PUBLIC_UI_FILES:
            self.assertTrue(os.path.isfile(os.path.join(UI, name)), name)

    def test_the_per_file_rule_is_what_the_classifier_uses(self):
        self.assertIs(luma_access.classify("/ui/<path:filename>", "GET", {"filename": "user.js"}),
                      Access.PUBLIC)
        self.assertIs(luma_access.classify("/ui/<path:filename>", "GET", {"filename": "index.html"}),
                      Access.ADMIN)
        self.assertIs(luma_access.classify("/ui/<path:filename>", "GET"), Access.ADMIN)


class TestsNeverSeeRealEquipmentTests(unittest.TestCase):
    def test_the_scratch_data_folder_holds_no_real_device_or_credential(self):
        # The application migrates runtime files from beside lumaserver.py into a new
        # data folder at start-up. The test package seeds that folder first, so
        # whatever sits in the developer's repository root never reaches a test.
        import json
        folder = os.environ["LUMA_DATA_DIR"]
        self.assertNotEqual(os.path.abspath(folder), os.path.abspath(ROOT))
        cache = json.load(open(os.path.join(folder, "units_cache.json"), encoding="utf-8"))
        for unit in cache:
            self.assertTrue(str(unit.get("ip", "")).startswith(("192.0.2.", "198.51.100.", "203.0.113.")),
                            "the unit cache holds a non-documentation address")
        config = json.load(open(os.path.join(folder, "producer_config.json"), encoding="utf-8"))
        self.assertNotIn("password", config)

    def test_the_application_started_with_no_units_but_the_ones_a_test_adds(self):
        with lumaserver.cache_lock:
            addresses = list(lumaserver.cache_units)
        for ip in addresses:
            self.assertTrue(ip.startswith(("192.0.2.", "198.51.100.", "203.0.113.")), ip)


class NoLiveValuesInProductionCodeTests(unittest.TestCase):
    """The fixtures prove genericity only if nothing real is hard-coded."""

    ADDRESS = re.compile(r"(?<![0-9.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![0-9.])")
    MAC = re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.I)
    ALLOWED = ("127.", "0.0.0.0", "192.0.2.", "198.51.100.", "203.0.113.", "255.", "1.0.0.",
               "240.")   # the reserved block, named only to be refused as a bind address

    def sources(self):
        for name in sorted(os.listdir(ROOT)):
            if name.startswith("luma_") and name.endswith(".py"):
                yield name
        for name in ("user.js", "login.js", "admin_users.js", "remote_settings.js",
                     "adminnav.js", "remember.js", "csrf.js"):
            yield os.path.join("ui", name)

    def test_no_device_address_or_hardware_address_is_hard_coded(self):
        for rel in self.sources():
            text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            for m in self.ADDRESS.finditer(text):
                literal = m.group(0)
                self.assertTrue(literal.startswith(self.ALLOWED), "%s contains %s" % (rel, literal))
            self.assertEqual(self.MAC.findall(text), [], rel)


if __name__ == "__main__":
    unittest.main()
