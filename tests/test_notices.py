"""Third-party notices describe what is packaged, the texts travel in the package, and /license serves them.

LumaSuite's own licence is undecided, so none is shipped and none is invented here. What is shipped
is a list of the components inside the executable and the licence text each one distributes.
"""
import json
import os
import re
import unittest

import _support

ROOT = _support.ROOT


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_pins():
    pins = {}
    for line in read("requirements-build-lock.txt").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^;\s]+)", line.split("#", 1)[0].strip())
        if match:
            pins.setdefault(canonical(match.group(1)), set()).add(match.group(2))
    return pins


MANIFEST = json.loads(read("licenses", "MANIFEST.json"))


def manifest_files():
    out = {}
    for entry in MANIFEST["shipped"]:
        for name in entry["files"]:
            out["%s/%s" % (entry["name"], name)] = os.path.join(ROOT, "licenses", entry["name"], *name.split("/"))
        for sub in entry["bundled"]:
            for name in sub["files"]:
                out["%s/%s" % (entry["name"], name)] = os.path.join(ROOT, "licenses", entry["name"], *name.split("/"))
    for entry in MANIFEST["runtime"]:
        for name in entry["files"]:
            out["_runtime/%s" % name] = os.path.join(ROOT, "licenses", "_runtime", *name.split("/"))
    return out


class NoticesCoverTheLockTests(unittest.TestCase):
    def test_every_locked_distribution_is_either_shipped_or_named_as_a_build_tool(self):
        shipped = {e["name"] for e in MANIFEST["shipped"]}
        build_only = set(MANIFEST["installed_not_packaged"])
        self.assertEqual(shipped & build_only, set())
        missing = sorted(set(lock_pins()) - shipped - build_only)
        self.assertEqual(missing, [], "a locked package that is neither listed as shipped nor as a build tool")
        self.assertEqual(sorted((shipped | build_only) - set(lock_pins())), [], "listed but not in the lock")

    def test_the_manifest_versions_are_the_locked_versions(self):
        pins = lock_pins()
        for entry in MANIFEST["shipped"]:
            self.assertIn(entry["version"], pins[entry["name"]], entry["name"])

    def test_the_runtime_packages_the_build_depends_on_are_shipped_not_skipped(self):
        # PyInstaller's own tooling is not packaged; the web stack, TLS, imaging and tray libraries are.
        shipped = {e["name"] for e in MANIFEST["shipped"]}
        for name in ("flask", "werkzeug", "requests", "urllib3", "certifi", "cryptography", "pillow",
                     "psutil", "pystray", "websocket-client", "jinja2", "markupsafe"):
            self.assertIn(name, shipped, name)
        for name in ("pyinstaller", "pefile", "altgraph"):
            self.assertNotIn(name, shipped, name)

    def test_the_notices_file_lists_every_shipped_distribution_and_version(self):
        notices = read("THIRD_PARTY_NOTICES.md")
        for entry in MANIFEST["shipped"]:
            self.assertRegex(notices, r"\|\s*\[?%s\]?[^|]*\|\s*%s\s*\|" % (re.escape(entry["name"]), re.escape(entry["version"])),
                             entry["name"])
            for sub in entry["bundled"]:
                self.assertIn("bundled in %s: %s" % (entry["name"], sub["name"]), notices)
        for entry in MANIFEST["runtime"]:
            self.assertIn(entry["name"], notices)

    def test_the_notices_are_honest_about_what_is_not_decided_or_not_inspected(self):
        notices = read("THIRD_PARTY_NOTICES.md")
        self.assertIn("Source-Available Software License", notices)
        self.assertNotIn("open item", notices)
        self.assertNotIn("not stated", notices)
        self.assertIn("LGPL", notices)
        self.assertIn("have not been inspected", " ".join(notices.split()))


class LicenceTextsAreInThePackageTests(unittest.TestCase):
    def test_every_listed_text_exists_and_is_not_empty(self):
        for name, path in manifest_files().items():
            self.assertTrue(os.path.isfile(path), name)
            self.assertGreater(os.path.getsize(path), 20, name)

    def test_no_text_in_licenses_is_unlisted(self):
        listed = {os.path.normpath(p) for p in manifest_files().values()}
        listed.add(os.path.normpath(os.path.join(ROOT, "licenses", "MANIFEST.json")))
        found = set()
        for current, _dirs, files in os.walk(os.path.join(ROOT, "licenses")):
            for filename in files:
                found.add(os.path.normpath(os.path.join(current, filename)))
        self.assertEqual(sorted(found - listed), [])

    def test_every_build_packages_the_texts_and_the_notices(self):
        build = read("build_executable.py")
        self.assertEqual(build.count("{LICENSES_DIR}{add_data_sep}licenses"), 3, "Windows, macOS and Linux")
        self.assertEqual(build.count("{NOTICES_FILE}{add_data_sep}."), 3)
        self.assertIn("THIRD_PARTY_NOTICES.md", build)

    def test_the_texts_are_not_excluded_from_version_control(self):
        ignore = read(".gitignore")
        self.assertNotRegex(ignore, r"(?m)^/?licenses/?\s*$")

    def test_no_licence_is_invented(self):
        # A LICENSE file is the owner's decision. If one is added this test stands aside and the
        # route serves it first; until then nothing here may pretend to be one.
        self.assertNotIn("[Add your license here]", read("README.md"))
        self.assertIn("Source-Available Software License", read("README.md"))


class LicenceRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import lumaserver
        cls.client = lumaserver.APP.test_client()

    def test_every_listed_text_is_served_and_matches_the_file(self):
        for name, path in manifest_files().items():
            response = self.client.get("/licenses/" + name)
            self.assertEqual(response.status_code, 200, name)
            with open(path, encoding="utf-8", errors="replace") as handle:
                self.assertEqual(response.get_data(as_text=True), handle.read(), name)

    def test_only_listed_paths_are_served(self):
        for name in ("MANIFEST.json", "flask/NOPE", "nope/LICENSE", "../lumaserver.py", "flask/../../lumaserver.py",
                     "%2e%2e/lumaserver.py", "flask", "", "flask/LICENSE.txt/", "/etc/passwd"):
            response = self.client.get("/licenses/" + name, follow_redirects=True)
            self.assertEqual(response.status_code, 404, name)

    def test_the_routes_are_classified_public_and_the_pages_link_to_them(self):
        import luma_access
        self.assertIs(luma_access.ROUTES["/license"], luma_access.Access.PUBLIC)
        self.assertIs(luma_access.ROUTES["/licenses/<path:name>"], luma_access.Access.PUBLIC)
        self.assertIn('href="/license"', read("ui", "settings.js"))
        self.assertIn('href="/license"', read("ui", "manual.html"))


if __name__ == "__main__":
    unittest.main()
