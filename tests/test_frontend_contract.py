"""The browser scripts and the server's route table agree about every endpoint.

A page that calls a path the server does not have fails silently at runtime; a page
that calls an endpoint of the wrong class gets refused. This pins each script's
endpoints against the route table, so neither can drift without a test noticing.
"""
import os
import re
import unittest

import _support

import luma_access
from luma_access import Access

UI = os.path.join(_support.ROOT, "ui")
LITERAL = re.compile(r"""['"](/(?:api|access|login|user|admin)[A-Za-z0-9_/<>.-]*)['"]""")

#: Which classes of route each script is allowed to call.
EXPECTED = {
    "login.js": {Access.PUBLIC},
    "user.js": {Access.USER, Access.AUTHENTICATED},
    # The one administrative path it names is the Remote Access page it links to (a
    # navigation target, shown only to an administrator), not an endpoint it calls.
    "adminnav.js": {Access.AUTHENTICATED, Access.ADMIN},
    "csrf.js": {Access.AUTHENTICATED},
    "admin_users.js": {Access.ADMIN},
    "remote_settings.js": {Access.ADMIN, Access.LOCAL_ONLY, Access.AUTHENTICATED},
    # The local application port block calls only the local-only port endpoints.
    "port_settings.js": {Access.LOCAL_ONLY},
}


def route_patterns():
    patterns = []
    for rule, access in luma_access.ROUTES.items():
        regex = re.sub(r"<[^>]+>", "[^/]+", re.escape(rule).replace("\\<", "<").replace("\\>", ">"))
        patterns.append((rule, access, re.compile("^" + regex + "$")))
    return patterns


def endpoints(script):
    text = open(os.path.join(UI, script), encoding="utf-8").read()
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    found = set()
    for m in LITERAL.finditer(text):
        literal = m.group(1).split("?")[0]
        if literal.startswith(("/login", "/user")) and not literal.startswith("/user/"):
            continue                                    # page navigation, not an API call
        found.add(literal)
    return sorted(found)


def classify_literal(literal):
    """The route classes a literal can be: a full path, or a prefix completed at run time."""
    matches = []
    for rule, access, regex in route_patterns():
        if regex.match(literal):
            matches.append((rule, access))
        elif literal.endswith("/") and rule.startswith(literal):
            matches.append((rule, access))
    return matches


class FrontendContractTests(unittest.TestCase):
    def test_every_endpoint_a_script_calls_exists_in_the_route_table(self):
        for script in EXPECTED:
            for literal in endpoints(script):
                self.assertTrue(classify_literal(literal), "%s calls %s, which is not a route" % (script, literal))

    def test_each_script_calls_only_the_classes_it_is_meant_to(self):
        for script, allowed in EXPECTED.items():
            for literal in endpoints(script):
                classes = {access for _rule, access in classify_literal(literal)}
                self.assertTrue(classes & allowed,
                                "%s calls %s which is %s, not one of %s" % (
                                    script, literal, sorted(c.value for c in classes),
                                    sorted(c.value for c in allowed)))

    def test_the_routing_page_never_calls_an_administrative_or_host_endpoint(self):
        for literal in endpoints("user.js"):
            for rule, access in classify_literal(literal):
                self.assertNotIn(access, (Access.ADMIN, Access.LOCAL_ONLY), "%s -> %s" % (literal, rule))

    def test_the_scripts_a_restricted_user_loads_are_public_files(self):
        html = open(os.path.join(UI, "user.html"), encoding="utf-8").read()
        for src in re.findall(r'<(?:script|link)[^>]+(?:src|href)="/ui/([^"?]+)', html):
            self.assertIs(luma_access.classify_ui_file(src), Access.PUBLIC, src)

    def test_the_sign_in_page_loads_only_public_files(self):
        html = open(os.path.join(UI, "login.html"), encoding="utf-8").read()
        for src in re.findall(r'<(?:script|link|img)[^>]+(?:src|href)="/ui/([^"?]+)', html):
            self.assertIs(luma_access.classify_ui_file(src), Access.PUBLIC, src)

    def test_the_accounts_page_is_an_administrative_page_loaded_by_name(self):
        self.assertIs(luma_access.classify_ui_file("admin_users.js"), Access.ADMIN)
        self.assertIs(luma_access.ROUTES["/admin/users"], Access.ADMIN)

    def test_every_operator_page_loads_the_csrf_wrapper_before_its_own_scripts(self):
        for page in ("index.html", "matrix.html", "wall.html", "producer.html", "manual.html",
                     "admin_users.html", "user.html", "login.html"):
            html = open(os.path.join(UI, page), encoding="utf-8").read()
            scripts = re.findall(r'<script src="([^"]+)"', html)
            self.assertIn("/ui/csrf.js", scripts, page)
            self.assertEqual(scripts[0], "/ui/theme-boot.js", page)
            # nothing but the theme runs before the wrapper installs
            self.assertEqual(scripts.index("/ui/csrf.js"), 1 if scripts[0] == "/ui/theme-boot.js" else 0, page)

    def test_the_operator_pages_that_have_settings_also_have_the_sign_out_script(self):
        for page in ("index.html", "matrix.html", "wall.html", "producer.html", "manual.html", "admin_users.html"):
            html = open(os.path.join(UI, page), encoding="utf-8").read()
            self.assertIn("/ui/adminnav.js", html, page)

    def test_a_user_facing_page_script_has_no_native_dialog_or_markup_sink(self):
        for script in ("user.js", "login.js", "admin_users.js", "remote_settings.js", "adminnav.js",
                       "clear_units.js"):
            text = re.sub(r"//.*$", "", open(os.path.join(UI, script), encoding="utf-8").read(), flags=re.M)
            self.assertNotRegex(text.replace("lumaConfirm", "").replace("confirmAsk", "").replace("confirmValue", ""),
                                r"(?<![\w.])(alert|confirm|prompt)\s*\(", script)
            self.assertNotRegex(text, r"\.innerHTML\s*=|insertAdjacentHTML|document\.write", script)

    def test_every_device_manager_flow_that_clears_units_reloads_what_the_server_kept(self):
        # Clear Units keeps the devices accounts and display groups use, so the page must not
        # assume the list is empty. Each flow is bounded to its own handler (a slice that ran to
        # the end of the file would start asserting about whatever is added below it), and
        # comments are stripped so an explanatory comment cannot satisfy the assertion.
        html = open(os.path.join(UI, "index.html"), encoding="utf-8").read().replace("\r\n", "\n")
        code = re.sub(r"^\s*//.*$", "", html, flags=re.M)

        def region(start, length=1800):
            at = code.index(start)
            return code[at:at + length]

        # Opening the page asks nothing about Clear Units (see
        # test_unification.ClearUnitsTests); the intentional flows remain.
        # With Remote Access off a clear empties the inventory outright and the page empties
        # its list. With Remote Access on the server keeps assigned units, so the page reloads
        # what the server holds instead of assuming nothing remains -- and only in that branch.
        self.assertNotIn("keptNote(", code)
        handler = region('$("#clear_units").addEventListener', 2600)
        handler = handler[:handler.index("// Targets input clear button")] if "// Targets input" in handler else handler
        self.assertIn("reloadUnitsFromServer()", handler)
        self.assertRegex(handler, r'scope === "unassigned" && result\.kept')
        self.assertIn("renderRows([])", handler)
        self.assertNotIn("Review what would be cleared", code)

    def test_the_hidden_attribute_wins_over_any_class_that_sets_display(self):
        # `.notice { display: flex }` once defeated `hidden`, showing both transport statements.
        css = open(os.path.join(UI, "family.css"), encoding="utf-8").read()
        self.assertRegex(css, r"\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}")
        self.assertRegex(css, r"\.notice\s*\{[^}]*display:\s*flex")        # the rule that caused it exists

    def test_one_escaping_helper_per_module_that_builds_markup(self):
        for script in ("matrix.js",):
            text = open(os.path.join(UI, script), encoding="utf-8").read()
            self.assertEqual(len(re.findall(r"^function esc\(", text, flags=re.M)), 1)
            # it is defined at module scope, before the callback that uses it
            self.assertLess(text.index("function esc("), text.index("document.addEventListener"))


if __name__ == "__main__":
    unittest.main()
