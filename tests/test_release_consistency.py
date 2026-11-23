"""One authoritative version, read by everything that shows it.

A literal copied into a page is correct on the day it is written and wrong from the next release
onward. The version lives in `lumaserver.py`; the build, the About text and Settings read it from
there, and the documents that must state it are checked here so they cannot drift apart.
"""
import os
import re
import unittest

import _support

ROOT = _support.ROOT


def text(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def declared(path, pattern):
    match = re.search(pattern, text(path), flags=re.M)
    assert match, "%s has no version matching %s" % (path, pattern)
    return match.group(1)


class ReleaseConsistencyTests(unittest.TestCase):
    VERSION = declared("lumaserver.py", r'^__version__ = "([^"]+)"')

    def test_the_launcher_declares_the_same_version(self):
        self.assertEqual(declared("app_launcher.py", r'^__version__ = "([^"]+)"'), self.VERSION)

    def test_the_readme_the_changelog_and_the_release_notes_state_it(self):
        self.assertEqual(declared("README.md", r"\*\*Current Version: ([^*]+)\*\*"), self.VERSION)
        self.assertEqual(declared("CHANGELOG.md", r"^## v([0-9][^ ]*) - "), self.VERSION)
        self.assertEqual(declared("RELEASE_NOTES.md", r"^# LumaSuite v([^ ]+) Release Notes"), self.VERSION)

    def test_the_project_reference_states_it(self):
        self.assertEqual(declared("PROJECT.md", r"Current version: \*\*([^*]+)\*\*"), self.VERSION)

    def test_the_build_reads_the_version_from_the_server_and_does_not_keep_its_own(self):
        build = text("build_executable.py")
        self.assertIn("lumaserver.py", build)
        self.assertIn("__version__", build)
        self.assertIsNone(re.search(r'^\s*(APP_)?VERSION\s*=\s*["\']\d', build, flags=re.M))

    def test_no_page_or_script_hard_codes_the_version(self):
        offenders = []
        pattern = re.compile(r"(?<![0-9.])v?%s(?![0-9])" % re.escape(self.VERSION))
        for name in sorted(os.listdir(os.path.join(ROOT, "ui"))):
            if name.endswith((".html", ".js", ".css")):
                for number, line in enumerate(text("ui", name).splitlines(), 1):
                    if pattern.search(line):
                        offenders.append("ui/%s:%d" % (name, number))
        self.assertEqual(offenders, [], "the version is read from /api/app_version, never typed into a page")

    def test_every_suite_in_the_repository_is_run_by_the_release_workflow(self):
        # This list drifted out of step with the workflow once before: a new test file existed and
        # was never run. A suite CI does not run protects nothing.
        workflow = text(".github", "workflows", "build-cross-platform.yml")
        missing = []
        for name in sorted(os.listdir(os.path.join(ROOT, "tests"))):
            if name.startswith("test_") and name.endswith(".py"):
                if "python tests/" + name not in workflow:
                    missing.append(name)
            elif name.endswith(".test.js") or name.endswith("_test.js"):
                if "node tests/" + name not in workflow:
                    missing.append(name)
        self.assertEqual(missing, [])

    def test_every_script_the_workflow_syntax_checks_exists_and_every_ui_script_is_checked(self):
        workflow = text(".github", "workflows", "build-cross-platform.yml")
        checked = set(re.findall(r"node --check (ui/[\w.-]+\.js)", workflow))
        for path in checked:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, path)), path)
        for name in ("user.js", "login.js", "csrf.js", "remember.js", "adminnav.js", "admin_users.js",
                     "remote_settings.js", "settings.js"):
            self.assertIn("ui/" + name, checked, name)

    def test_the_workflow_compiles_every_server_module(self):
        workflow = text(".github", "workflows", "build-cross-platform.yml")
        compiled = re.search(r"py_compile ([^\n]+)", workflow).group(1).split()
        for name in sorted(os.listdir(ROOT)):
            if name.startswith("luma_") and name.endswith(".py"):
                self.assertIn(name, compiled, name)

    def test_the_changelog_entry_for_this_version_describes_remote_access(self):
        top = text("CHANGELOG.md").split("## v")[1]
        self.assertIn("Remote Access", top)
        self.assertIn("Display Groups", top)

    def test_the_release_notes_name_what_still_needs_hardware(self):
        self.assertIn("To validate on hardware", text("RELEASE_NOTES.md"))

    def test_the_user_guide_covers_every_operator_visible_feature_of_the_release(self):
        guide = text("ui", "manual.html")
        for anchor in ("remote-access", "accounts", "friendly-names", "user-routing", "display-groups",
                       "direct-access", "clear-units", "backup-restore"):
            self.assertIn('id="%s"' % anchor, guide, anchor)

    def test_the_guide_does_not_describe_internals_an_operator_cannot_use(self):
        guide = re.sub(r"<[^>]+>", " ", text("ui", "manual.html")).lower()
        for word in ("luma_access", "luma_gate", "classify(", "before_request", "route_av", "scrypt",
                     "/api/user", "missing_feature", "resource_busy", "phase "):
            self.assertNotIn(word, guide, word)

    def test_a_release_tag_must_be_v_plus_the_application_version(self):
        workflow = text(".github", "workflows", "build-cross-platform.yml")
        step = workflow[workflow.index("Tag must be v plus the application version"):]
        step = step[:step.index("- name: The interpreter is the architecture this row claims")]
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", step)
        self.assertIn("lumaserver.py", step)
        # the tag reaches the check through the environment, never interpolated into the script
        self.assertIn("TAG_NAME: ${{ github.ref_name }}", step)
        self.assertNotIn("${{ github.ref_name }}", step.split("run: |", 1)[1])

    def test_the_macos_bundle_is_stamped_with_the_application_version(self):
        import plistlib
        import shutil
        import sys
        import tempfile
        from pathlib import Path
        sys.path.insert(0, ROOT)
        import build_executable
        folder = tempfile.mkdtemp()
        try:
            app = Path(folder) / "LumaSuite.app"
            (app / "Contents").mkdir(parents=True)
            with open(app / "Contents" / "Info.plist", "wb") as handle:
                plistlib.dump({"CFBundleShortVersionString": "0.0.0", "CFBundleVersion": "0.0.0",
                               "CFBundleIdentifier": "com.lumasuite.app"}, handle)
            self.assertTrue(build_executable.stamp_macos_version(app, self.VERSION))
            with open(app / "Contents" / "Info.plist", "rb") as handle:
                info = plistlib.load(handle)
            self.assertEqual(info["CFBundleShortVersionString"], self.VERSION)
            self.assertEqual(info["CFBundleVersion"], self.VERSION)
            self.assertEqual(info["CFBundleIdentifier"], "com.lumasuite.app")
            self.assertFalse(build_executable.stamp_macos_version(Path(folder) / "missing.app", self.VERSION))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_the_windows_resource_carries_the_application_version(self):
        import sys
        sys.path.insert(0, ROOT)
        import build_executable
        parts = [int(x) for x in re.findall(r"\d+", self.VERSION)[:4]]
        parts += [0] * (4 - len(parts))
        self.assertEqual(tuple(build_executable._version_to_win_tuple(self.VERSION)), tuple(parts))

    def test_a_step_that_runs_a_list_of_suites_stops_at_the_first_failure_on_every_platform(self):
        # On Windows a run step without a shell runs under PowerShell, which reports only the last
        # command's exit status: an earlier failing suite would be ignored. bash -e stops at the first.
        workflow = text(".github", "workflows", "build-cross-platform.yml")
        NL = chr(10)
        checked = 0
        for step in workflow.split(NL + "      - name: ")[1:]:
            head, _, script = step.partition("run: |")
            runs = [line for line in script.splitlines() if line.strip().startswith(("python ", "node "))]
            if len(runs) > 1:
                checked += 1
                self.assertIn("shell: bash", head, step.splitlines()[0])
        self.assertGreaterEqual(checked, 2, "the suite lists were not found")

    def test_only_the_publish_job_may_write_to_the_repository(self):
        workflow = text(".github", "workflows", "build-cross-platform.yml")
        NL = chr(10)
        head, publish = workflow.split("  publish-release:" + NL, 1)
        self.assertIn("permissions:" + NL + "  contents: read" + NL, head)
        self.assertNotIn("contents: write", head)
        self.assertIn("contents: write", publish)
        self.assertIn("if: needs.identity.outputs.publishable == 'true'", publish)
        self.assertIn("needs: [identity, build]", publish)


if __name__ == "__main__":
    unittest.main()
