"""No author folder layout and no operating-system username is published anywhere in the tree.

The release inspector finds a build-machine or user-profile path by its SHAPE (tools/inspect_release.py), so
nothing about one author's folders is written into the detector. This suite applies that same detector to the
whole repository: the customer documents, the interface, the production code, the workflow and the tools must
carry no such path, and the tests must carry no user-profile path other than the files that hold the
detector and its own synthetic samples. Deliberately synthetic names (Alice, acme, widgets) are used there.
"""
import importlib.util
import os
import re
import unittest

import _support

ROOT = _support.ROOT
spec = importlib.util.spec_from_file_location("inspect_release_private_paths", os.path.join(ROOT, "tools", "inspect_release.py"))
insp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(insp)

SKIP_DIRS = {".git", "third_party", "node_modules", "__pycache__", "build", "dist", ".venv", "venv", "archive"}
TEXT = (".md", ".txt", ".html", ".css", ".js", ".json", ".py", ".yml", ".yaml", ".toml", ".cfg", ".spec", ".terms",
        ".lgpl", ".rst", "")
#: The files that hold the detector itself (its pattern source reads as a path to its own pattern) and its
#: synthetic samples.
SAMPLES = {os.path.join("tools", "inspect_release.py"), os.path.join("tests", "test_inspect_release.py"),
           os.path.join("tests", "test_private_paths.py")}
USER_SHAPE = re.compile(rb"(?i)^[A-Za-z]:\\+Users|^/(Users|home)/")


def files():
    for folder, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            relative = os.path.relpath(os.path.join(folder, name), ROOT)
            if os.path.splitext(name)[1].lower() in TEXT and not name.endswith(".lumabak"):
                yield relative


class NoPrivatePathTests(unittest.TestCase):
    def test_the_customer_facing_and_production_files_carry_no_private_path(self):
        found = []
        for relative in files():
            if relative.startswith("tests" + os.sep) or relative in SAMPLES:
                continue
            with open(os.path.join(ROOT, relative), "rb") as handle:
                body = handle.read()
            for match in insp._BUILD_PATH.finditer(body):
                found.append("%s:%d %r" % (relative, body[:match.start()].count(b"\n") + 1, match.group(0)[:40]))
        self.assertEqual(found, [])

    def test_the_tests_carry_no_user_profile_path_outside_the_sample_files(self):
        found = []
        for relative in files():
            if not relative.startswith("tests" + os.sep) or relative in SAMPLES:
                continue
            with open(os.path.join(ROOT, relative), "rb") as handle:
                body = handle.read()
            for match in insp._BUILD_PATH.finditer(body):
                if USER_SHAPE.match(match.group(0)):
                    found.append("%s:%d %r" % (relative, body[:match.start()].count(b"\n") + 1, match.group(0)[:40]))
        self.assertEqual(found, [])

    def test_the_scan_really_reads_the_tree(self):
        names = {os.path.basename(f) for f in files()}
        for needed in ("README.md", "lumaserver.py", "manual.html", "inspect_release.py", "build-cross-platform.yml"):
            self.assertIn(needed, names)

    def test_the_scan_fails_when_a_private_path_is_present(self):
        # A scan that cannot fail proves nothing: hand it the shapes it exists to find.
        for sample in (b"C:\\Users\\Alice\\Desktop\\x", b"/home/dave/projects/x/", b"D:\\widgets\\tree\\x"):
            self.assertTrue(insp._BUILD_PATH.search(sample), sample)

    def test_user_facing_text_still_mentions_the_real_locations_it_documents(self):
        # The documented per-user locations use placeholders and environment names, and are not private paths.
        readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        self.assertIn("LUMA_DATA_DIR", readme)
        self.assertIsNone(insp._BUILD_PATH.search(readme.encode("utf-8")))


if __name__ == "__main__":
    unittest.main()
