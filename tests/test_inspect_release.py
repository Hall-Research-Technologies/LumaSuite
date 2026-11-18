"""The release inspector fails closed, and each of its rules fires.

Synthetic archives only: a clean package is laid out from the repository's own licence and
third-party material (the pinned pystray files are the real ones), with a stand-in executable.
Every defect is introduced into an otherwise clean archive and must produce a finding that names
it; the clean archive must produce none. Nothing here reads a real build or touches a device.
"""
import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import _support

ROOT = _support.ROOT
spec = importlib.util.spec_from_file_location("inspect_release", os.path.join(ROOT, "tools", "inspect_release.py"))
insp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(insp)

VERSION = "v9.8.7"
NAME = "LumaSuite-%s-windows-x86_64.zip" % VERSION
KEY_BLOCK = b"-----BEGIN " + b"RSA PRIVATE KEY-----\nabc"


def clean_members():
    """name -> bytes of a package that should pass."""
    out = {"LumaSuite.exe": b"MZ" + b"\0" * 2048}
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "REBUILD.md"):
        with open(os.path.join(ROOT, name), "rb") as handle:
            out[name] = handle.read()
    for tree in ("licenses", "third_party"):
        for folder, dirs, files in os.walk(os.path.join(ROOT, tree)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for file in files:
                full = os.path.join(folder, file)
                with open(full, "rb") as handle:
                    out[os.path.relpath(full, ROOT).replace(os.sep, "/")] = handle.read()
    return out


def build(directory, members, name=NAME):
    path = os.path.join(directory, name)
    if name.endswith(".zip"):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for member, data in members.items():
                archive.writestr(member, data)
    else:
        with tarfile.open(path, "w:gz") as archive:
            for member, data in members.items():
                info = tarfile.TarInfo(member)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return path


class InspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = clean_members()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def run_with(self, change=None, name=NAME, version=VERSION, diagnostic=False):
        members = dict(self.base)
        if change:
            change(members)
        return insp.problems_for(Path(build(self.dir.name, members, name)), version, diagnostic)

    def assertFlags(self, text, change=None, **kw):
        found = self.run_with(change, **kw)
        self.assertTrue(any(text in line for line in found),
                        "expected a finding containing %r, got %r" % (text, found))
        return found

    @staticmethod
    def append(member, extra):
        return lambda m: m.update({member: m[member] + extra})

    # ------------------------------------------------------------------ clean
    def test_a_clean_package_has_no_findings(self):
        self.assertEqual(self.run_with(), [])

    def test_the_clean_package_carries_the_required_lgpl_material(self):
        for needed in ("third_party/pystray-0.19.5/pystray-0.19.5-github-source.tar.gz",
                       "third_party/pystray-0.19.5/pystray-0.19.5-py2.py3-none-any.whl",
                       "third_party/pystray-0.19.5/source/tests/__init__.py"):
            self.assertIn(needed, self.base)   # upstream's own tests/ is part of that source, not leakage

    # ------------------------------------------------------------- the name
    def test_a_non_release_name_is_refused(self):
        for bad in ("LumaSuite-v9.8.7-rc1-windows-x86_64.zip", "LumaSuite-V9.8.7-windows-x86_64.zip",
                    "LumaSuite-v9.8.6-windows-x86_64.zip", "LumaSuite-9.8.7-windows-x86_64.zip",
                    "OmniSuite-v9.8.7-windows-x86_64.zip", "LumaSuite-ci-luma-hw2-windows-x86_64.zip"):
            self.assertFlags("is not this product's release archive", name=bad)
        self.assertFlags("not named for a platform build", name="LumaSuite-v9.8.7-windows.zip")

    def test_a_version_that_is_not_a_release_is_refused_even_with_the_flag(self):
        for bad in ("v9.8.7-rc1", "V9.8.7", "v9.8.7\n", "9.8.7", "v9.8"):
            found = self.run_with(version=bad, diagnostic=True)
            self.assertTrue(any("not a release version" in line for line in found), (bad, found))

    def test_diagnostic_relaxes_the_name_only(self):
        ci = "LumaSuite-ci-luma-hw2-windows-x86_64.zip"
        self.assertFlags("is not", name=ci)
        self.assertEqual(self.run_with(name=ci, diagnostic=True), [])
        self.assertEqual(self.run_with(name="LumaSuite-v9.8.7-diag3-windows-x86_64.zip", diagnostic=True), [])
        self.assertFlags("is not", name="LumaSuite-v9.8.7-rc1-windows-x86_64.zip", diagnostic=True)
        self.assertFlags("is not", name="LumaSuite-ci-X Y-windows-x86_64.zip", diagnostic=True)
        self.assertFlags("must not ship", lambda m: m.update({"tests/test_x.py": b"x"}), name=ci, diagnostic=True)

    def test_the_other_platform_names_are_accepted(self):
        mac = {n: d for n, d in self.base.items() if n != "LumaSuite.exe"}
        mac["LumaSuite.app/Contents/MacOS/LumaSuite"] = b"\xcf\xfa" + b"\0" * 512
        found = insp.problems_for(Path(build(self.dir.name, mac, "LumaSuite-v9.8.7-macos-arm64.zip")), VERSION)
        self.assertEqual(found, [])
        linux = {n: d for n, d in self.base.items() if n != "LumaSuite.exe"}
        linux["LumaSuite"] = b"\x7fELF" + b"\0" * 512
        found = insp.problems_for(Path(build(self.dir.name, linux, "LumaSuite-v9.8.7-linux-x86_64.tar.gz")), VERSION)
        self.assertEqual(found, [])

    def test_a_corrupt_archive_is_a_finding_not_a_crash(self):
        path = os.path.join(self.dir.name, NAME)
        with open(path, "wb") as handle:
            handle.write(b"not a zip")
        self.assertTrue(any("cannot be read" in line for line in insp.problems_for(Path(path), VERSION)))

    # ---------------------------------------------------------- required
    def test_each_required_member_is_required(self):
        for needed in ("LumaSuite.exe", "LICENSE", "THIRD_PARTY_NOTICES.md", "REBUILD.md", "licenses/MANIFEST.json",
                       "licenses/pystray/COPYING.LGPL", "third_party/pystray-0.19.5/PROVENANCE.md",
                       "third_party/pystray-0.19.5/pystray-0.19.5-github-source.tar.gz",
                       "third_party/pystray-0.19.5/pystray-0.19.5-py2.py3-none-any.whl",
                       "third_party/pystray-0.19.5/source/COPYING.LGPL"):
            self.assertFlags("no " + needed, lambda m, n=needed: m.pop(n))

    def test_the_pystray_material_must_be_the_pinned_upstream_bytes(self):
        self.assertFlags("is not the pinned upstream file", lambda m: m.update(
            {"third_party/pystray-0.19.5/pystray-0.19.5-py2.py3-none-any.whl": b"modified"}))

    def test_the_licence_must_carry_section_11a(self):
        self.assertFlags("section 11A", lambda m: m.update({"LICENSE": b"MIT"}))

    def test_the_windows_top_level_is_exact(self):
        self.assertFlags("top level must be exactly", lambda m: m.update({"README.txt": b"x"}))

    # -------------------------------------------------------- forbidden names
    def test_forbidden_members_are_refused(self):
        cases = {
            "tests/test_a.py": "test folder", "tools/release_guard.py": "tools folder",
            "lumaserver.py": "own source", "luma_users.py": "own source", "producer2_routes.py": "own source",
            "app.py": "Python source", "firmware/HW-LUMA-E4521.img": "firmware", "fw.bin": "firmware",
            "server.key": "key", "server.crt": "key", "cert.pem": "key", "id_rsa": "key", ".env": "key",
            "users.json": "state file", "units_cache.json": "state file", "remote_access.json": "state file",
            "security_audit.json": "state file", "video_walls.json": "state file",
            "producer_state.json": "state file", "ui/localdata/producer_cache.json": "runtime data",
            "x.lumabak": "backup", "units_cache.json.bak": "backup", "a.tmp": "backup", "x~": "backup",
            "app.log": "log", ".git/config": "version-control", "__pycache__/a.pyc": "compiled",
            "AGENTS.md": "development document", "V1.1.1_MANUAL_TEST_CHECKLIST.md": "development document",
            "soak_results.txt": "diagnostic", "mutation_report.txt": "diagnostic", "evidence/x.txt": "diagnostic",
            "second.zip": "nested archive", "LumaSuite-v9.8.7-linux-x86_64.tar.gz": "nested archive",
            "other.exe": "nested archive", ".github/workflows/x.yml": "development-environment",
        }
        for member, why in cases.items():
            found = self.run_with(lambda m, n=member: m.update({n: b"x"}))
            self.assertTrue(any(member in line and why in line for line in found), (member, why, found))

    def test_the_licence_tree_holds_nothing_but_licence_texts(self):
        self.assertFlags("only licence texts", lambda m: m.update({"licenses/x/secret.pem": b"x"}))
        self.assertFlags("only licence texts", lambda m: m.update({"licenses/x/units.log": b"x"}))

    def test_the_build_tools_own_members_are_not_flagged(self):
        mac = {n: d for n, d in self.base.items() if n != "LumaSuite.exe"}
        mac.update({"LumaSuite.app/Contents/MacOS/LumaSuite": b"MACHO" + bytes(512),
                    "LumaSuite.app/x/base_library.zip": b"x",
                    "LumaSuite.app/x/certifi/cacert.pem": b"-----BEGIN CERTIFICATE-----"})
        found = insp.problems_for(Path(build(self.dir.name, mac, "LumaSuite-v9.8.7-macos-arm64.zip")), VERSION)
        self.assertEqual(found, [])

    # ------------------------------------------------------------- content
    def test_credentials_and_key_blocks_are_refused(self):
        self.assertFlags("credential or private-key", self.append("REBUILD.md", KEY_BLOCK))
        self.assertFlags("credential or private-key", self.append("THIRD_PARTY_NOTICES.md", b"\npassword = 'hunter2hunter2'\n"))
        self.assertFlags("credential or private-key", self.append("REBUILD.md", b"\nghp_" + b"a" * 36))
        self.assertFlags("credential or private-key", self.append("LumaSuite.exe", KEY_BLOCK))
        # upstream material is scanned for credentials too
        self.assertFlags("credential or private-key",
                         lambda m: m.update({"third_party/pystray-0.19.5/source/README.rst": KEY_BLOCK}))

    def test_a_public_key_token_in_an_executable_manifest_is_not_a_credential(self):
        manifest = b'<assemblyIdentity publicKeyToken="6595b64144ccf1df"/>'
        self.assertEqual(self.run_with(self.append("LumaSuite.exe", manifest)), [])

    def test_lab_addresses_are_refused(self):
        self.assertFlags("lab-range address", self.append("REBUILD.md", b"\nhost 192.168.100.37\n"))
        self.assertEqual(self.run_with(self.append("REBUILD.md", b"\nuse 198.18.4.5\n")), [])

    def test_private_product_names_are_refused(self):
        for text in (b"OmniSuite Plus", b"LumaSuite-Pro", b"omnisuite-unified", b"internal repository"):
            self.assertFlags("private product", self.append("THIRD_PARTY_NOTICES.md", b"\n" + text + b"\n"))

    def test_build_machine_paths_are_refused_in_text_and_in_the_executable(self):
        for path in (b"C:\\softwareDEV\\work24\\x", b"C:\\Users\\Someone\\AppData\\Local\\Temp\\a", b"overnight_rc"):
            self.assertFlags("build-machine path", self.append("REBUILD.md", b"\n" + path))
            self.assertFlags("build-machine path", self.append("LumaSuite.exe", path))

    # ------------------------------------------------------------------ cli
    def test_the_command_line_fails_closed(self):
        good = build(self.dir.name, dict(self.base))
        self.assertEqual(insp.main([good, "--version", VERSION]), 0)
        bad = build(self.dir.name, {**self.base, "users.json": b"{}"}, "LumaSuite-v9.8.7-macos-arm64.zip")
        self.assertEqual(insp.main([bad, "--version", VERSION]), 1)


if __name__ == "__main__":
    unittest.main()
