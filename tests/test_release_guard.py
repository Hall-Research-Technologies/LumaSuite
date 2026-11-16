"""The release guard fails closed.

Identity is decided before anything is built; assets are inspected before a draft exists; checksums come from
the final bytes. No network, no git: `decide_identity` takes its facts as arguments.
"""
import hashlib
import importlib.util
import os
import tarfile
import tempfile
import unittest
import zipfile

import _support

ROOT = _support.ROOT
spec = importlib.util.spec_from_file_location("release_guard", os.path.join(ROOT, "tools", "release_guard.py"))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

REPO = guard.EXPECTED_REPOSITORY


def decide(ref_type="tag", ref="v1.1.0", repo=REPO, version="1.1.0", on_main=True, event="push"):
    return guard.decide_identity(event, ref_type, ref, repo, version, on_main)


class IdentityTests(unittest.TestCase):
    def test_the_exact_tag_on_main_is_publishable(self):
        out = decide()
        self.assertTrue(out["publishable"])
        self.assertEqual(out["tag"], "v1.1.0")

    def test_non_release_tags_are_refused(self):
        for tag in ("v1.1.0-rc4", "v1.1.0-weekend-rc4", "V1.1.0", "v1.1", "v1.1.0\n", "1.1.0", "v1.1.0.1", "v1.1.0-test"):
            with self.assertRaises(guard.GuardError, msg=tag):
                decide(ref=tag)

    def test_tag_must_equal_the_application_version(self):
        with self.assertRaises(guard.GuardError):
            decide(ref="v1.1.1")

    def test_a_candidate_version_is_refused_even_if_tag_agrees(self):
        with self.assertRaises(guard.GuardError):
            decide(ref="v1.1.0-rc1", version="1.1.0-rc1")

    def test_tag_off_the_release_branch_is_refused(self):
        with self.assertRaises(guard.GuardError):
            decide(on_main=False)

    def test_wrong_repository_is_refused(self):
        with self.assertRaises(guard.GuardError):
            decide(repo="someone/LumaSuite")

    def test_branch_push_and_dispatch_are_diagnostic_and_cannot_publish(self):
        for event in ("push", "workflow_dispatch"):
            out = decide(ref_type="branch", ref="main", event=event)
            self.assertFalse(out["publishable"])
            self.assertEqual(out["tag"], "")


def make_assets(directory, tag="v1.1.0", skip=None, extra=None, windows_extra=None, junk=None, drop=None, tamper=None):
    """Build the four archives the workflow builds, with the real licence/notice/pystray files beside the app."""
    import shutil
    blob = os.urandom(1024 * 1100)
    work = tempfile.mkdtemp(prefix="stage-")
    try:
        for name in guard.expected_assets(tag):
            if name == skip:
                continue
            platform_name = "windows" if "windows" in name else "macos" if "macos" in name else "linux"
            dist = os.path.join(work, "dist-" + name)
            os.makedirs(dist)
            if platform_name == "windows":
                with open(os.path.join(dist, "LumaSuite.exe"), "wb") as h:
                    h.write(blob)
            elif platform_name == "macos":
                os.makedirs(os.path.join(dist, "LumaSuite.app", "Contents", "MacOS"))
                with open(os.path.join(dist, "LumaSuite.app", "Contents", "MacOS", "LumaSuite"), "wb") as h:
                    h.write(blob)
                os.makedirs(os.path.join(dist, "LumaSuite.app", "Contents", "Resources", "certifi"))
                with open(os.path.join(dist, "LumaSuite.app", "Contents", "Resources", "certifi", "cacert.pem"), "wb") as h:
                    h.write(b"public CA bundle")
            else:
                with open(os.path.join(dist, "LumaSuite"), "wb") as h:
                    h.write(blob)
            out = os.path.join(work, "out-" + name)
            guard.stage(platform_name, dist, out, root=ROOT)
            if windows_extra and platform_name == "windows":
                with open(os.path.join(out, windows_extra), "wb") as h:
                    h.write(b"x")
            if junk and platform_name == "macos":
                with open(os.path.join(out, junk), "wb") as h:
                    h.write(b"x")
            if drop:
                p = os.path.join(out, drop)
                (shutil.rmtree if os.path.isdir(p) else os.remove)(p)
            if tamper:
                with open(os.path.join(out, tamper), "ab") as h:
                    h.write(b"tampered")
            path = os.path.join(directory, name)
            if name.endswith(".zip"):
                with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
                    for cur, _dirs, files in os.walk(out):
                        for f in files:
                            full = os.path.join(cur, f)
                            z.write(full, os.path.relpath(full, out).replace(os.sep, "/"))
            else:
                with tarfile.open(path, "w:gz") as tar:
                    tar.add(out, arcname=".")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if extra:
        with open(os.path.join(directory, extra), "wb") as h:
            h.write(b"x" * 10)


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_four_exact_assets_pass(self):
        make_assets(self.dir)
        self.assertEqual(guard.inspect_assets(self.dir, "v1.1.0"), guard.expected_assets("v1.1.0"))

    def test_a_candidate_named_extra_asset_is_refused(self):
        make_assets(self.dir, extra="LumaSuite-v1.1.0-weekend-rc4-windows-x86_64.exe")
        with self.assertRaises(guard.GuardError):
            guard.inspect_assets(self.dir, "v1.1.0")

    def test_a_missing_asset_is_refused(self):
        make_assets(self.dir, skip="LumaSuite-v1.1.0-linux-x86_64.tar.gz")
        with self.assertRaises(guard.GuardError):
            guard.inspect_assets(self.dir, "v1.1.0")

    def test_assets_for_another_tag_are_refused(self):
        make_assets(self.dir, tag="v1.0.8")
        with self.assertRaises(guard.GuardError):
            guard.inspect_assets(self.dir, "v1.1.0")

    def test_the_windows_archive_may_hold_only_the_approved_top_level(self):
        make_assets(self.dir, windows_extra="notes.txt")
        with self.assertRaises(guard.GuardError):
            guard.inspect_assets(self.dir, "v1.1.0")

    def test_every_archive_carries_the_licence_notices_rebuild_steps_and_pystray_source(self):
        for missing in ("LICENSE", "THIRD_PARTY_NOTICES.md", "REBUILD.md", "licenses",
                        "third_party/pystray-0.19.5/pystray-0.19.5-py2.py3-none-any.whl",
                        "third_party/pystray-0.19.5/source"):
            with tempfile.TemporaryDirectory() as d:
                make_assets(d, drop=missing)
                with self.assertRaises(guard.GuardError, msg=missing):
                    guard.inspect_assets(d, "v1.1.0")

    def test_a_modified_pystray_file_is_refused(self):
        make_assets(self.dir, tamper="third_party/pystray-0.19.5/pystray-0.19.5-py2.py3-none-any.whl")
        with self.assertRaises(guard.GuardError):
            guard.inspect_assets(self.dir, "v1.1.0")

    def test_a_licence_without_section_11a_is_refused(self):
        # the staged LICENSE is the real one; check the guard's test of it directly
        with tempfile.TemporaryDirectory() as d:
            make_assets(d)
            win = os.path.join(d, "LumaSuite-v1.1.0-windows-x86_64.zip")
            self.assertEqual(guard.check_beside(win), "")
            old = guard.SECTION_11A
            guard.SECTION_11A = "11A. NOT PRESENT ANYWHERE"
            try:
                with self.assertRaises(guard.GuardError):
                    guard.check_beside(win)
            finally:
                guard.SECTION_11A = old

    def test_forbidden_members_are_refused(self):
        make_assets(self.dir, junk="LumaSuite.app/Contents/server.pem")
        with self.assertRaises(guard.GuardError):
            guard.inspect_assets(self.dir, "v1.1.0")

    def test_an_rc_tag_is_not_inspectable_as_production(self):
        with self.assertRaises(guard.GuardError):
            guard.inspect_assets(self.dir, "v1.1.0-rc4")


class ExecutableMemberTests(unittest.TestCase):
    def test_a_public_ca_bundle_is_not_key_material(self):
        self.assertEqual(guard.forbidden_executable_member("certifi\\cacert.pem"), "")
        self.assertEqual(guard.forbidden_executable_member("ui\\firmware-summary.js"), "")
        self.assertEqual(guard.forbidden_executable_member("licenses\\certifi\\LICENSE"), "")

    def test_private_and_runtime_material_is_refused(self):
        for name in ("server.pem", "ui/key.pfx", "units_cache.json", "scan_results.json", "firmware/x.bin",
                     "archive/old.html", "producer_config.json", "soak/run.csv", "x/.env"):
            self.assertNotEqual(guard.forbidden_executable_member(name), "", name)


class ChecksumTests(unittest.TestCase):
    def test_checksums_come_from_the_final_bytes_and_are_stable(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.bin"), "wb") as h:
                h.write(b"abc")
            lines = guard.write_checksums(d)
            self.assertEqual(lines, ["%s *a.bin\n" % hashlib.sha256(b"abc").hexdigest()])
            again = guard.write_checksums(d)           # the sums file itself is never listed
            self.assertEqual(lines, again)


class WorkflowWiringTests(unittest.TestCase):
    """The workflow cannot publish on its own: identity first, draft only, no auto-publish action."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, ".github", "workflows", "build-cross-platform.yml"), encoding="utf-8") as h:
            cls.text = h.read().replace("\r\n", "\n")

    def test_identity_job_runs_the_guard_and_builds_depend_on_it(self):
        self.assertIn("tools/release_guard.py identity", self.text)
        self.assertIn("needs: identity", self.text)

    def test_the_release_is_a_draft_and_never_auto_published(self):
        self.assertIn("--draft", self.text)
        self.assertNotIn("softprops/action-gh-release", self.text)
        self.assertNotIn("--latest", self.text)

    def test_publish_is_gated_on_the_guard_verdict_and_inspection(self):
        self.assertIn("needs.identity.outputs.publishable == 'true'", self.text)
        self.assertIn("tools/release_guard.py inspect", self.text)
        self.assertIn("tools/release_guard.py checksums", self.text)

    def test_the_tag_is_never_interpolated_into_a_shell_command(self):
        publish = self.text[self.text.index("  publish-release:"):]
        self.assertNotIn("github.ref_name", publish)
        self.assertNotIn("github.head_ref", publish)


if __name__ == "__main__":
    unittest.main()
