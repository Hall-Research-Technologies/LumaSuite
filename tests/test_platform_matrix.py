"""The distributed platform matrix: Windows x86_64 and macOS Apple Silicon (arm64), and nothing else.

Three different things are kept apart on purpose:

* the TEST RUNNER platform: where the suite runs in CI;
* the DISTRIBUTED platform: what a release carries, names and offers to the updater;
* an unsupported legacy platform (macOS Intel, Linux): built by earlier releases and not since. Nothing is
  built, named, published or offered for it, and the updater says so plainly.

The workflow is read as text (comments stripped, so a comment that names the thing under test cannot satisfy
the assertion), the build and guard tables are read as data, and the macOS architecture verifier is exercised
on synthetic Mach-O headers. The licence material every package must carry is asserted on packages laid out
the way the workflow lays them out.
"""
import importlib.util
import os
import plistlib
import re
import struct
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import _support

import lumaserver

ROOT = Path(_support.ROOT)
sys.path.insert(0, str(ROOT / "tools"))

import release_guard as guard           # noqa: E402
import verify_macho_arch as macho       # noqa: E402


def _spec(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inspect = _spec("inspect_release_for_matrix_tests", "tools/inspect_release.py")
build = _spec("build_executable_for_matrix_tests", "build_executable.py")


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def workflow_text():
    return "\n".join(l for l in read(".github/workflows/build-cross-platform.yml").splitlines()
                     if not l.lstrip().startswith("#"))


def job(text, name):
    match = re.search(r"^  %s:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)" % re.escape(name), text, re.S | re.M)
    assert match, "no job named " + name
    return match.group(1)


class DistributedSetTests(unittest.TestCase):
    def test_the_distributed_set_is_exactly_two_and_every_table_agrees(self):
        self.assertEqual(guard.DISTRIBUTED_TAILS, ("windows-x86_64.zip", "macos-arm64.zip"))
        self.assertEqual(inspect.PLATFORM_TAILS, guard.DISTRIBUTED_TAILS)
        self.assertEqual(guard.expected_assets("v9.9.9"),
                         ["LumaSuite-v9.9.9-macos-arm64.zip", "LumaSuite-v9.9.9-windows-x86_64.zip"])
        # the updater's tables name the same two, by the same tails
        tails = set()
        for plat, machine in (("win32", "AMD64"), ("darwin", "arm64")):
            tails.update(lumaserver._platform_asset_suffixes(plat, machine))
        self.assertEqual(tails - {"-windows-x86_64.exe"}, {"-" + t for t in guard.DISTRIBUTED_TAILS})

    def test_every_name_states_its_architecture(self):
        for tail in guard.DISTRIBUTED_TAILS:
            self.assertTrue("x86_64" in tail or "arm64" in tail, tail)

    def test_the_workflow_packages_exactly_the_distributed_tails(self):
        text = workflow_text()
        tails = re.findall(r"LumaSuite-\$\{\{ github\.ref_name \}\}-(windows-x86_64\.zip|macos-\$\{\{ matrix\.mac_arch \}\}\.zip)", text)
        self.assertEqual(sorted(tails), ["macos-${{ matrix.mac_arch }}.zip", "windows-x86_64.zip"])
        self.assertEqual(re.findall(r"mac_arch: (\S+)", text), ['""', "arm64"])

    def test_the_stage_function_cannot_lay_out_a_retired_platform(self):
        with tempfile.TemporaryDirectory() as folder:
            for retired in ("linux", "macos-intel", "macos_x86_64"):
                with self.assertRaises(guard.GuardError):
                    guard.stage(retired, folder, str(Path(folder) / ("out-" + retired)), root=str(ROOT))

    def test_the_release_gate_requires_exactly_the_two_and_refuses_a_retired_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in guard.expected_assets("v9.9.9"):
                (Path(folder) / name).write_bytes(b"x")
            for stray in ("LumaSuite-v9.9.9-macos-x86_64.zip", "LumaSuite-v9.9.9-linux-x86_64.tar.gz"):
                (Path(folder) / stray).write_bytes(b"x")
                with self.assertRaises(guard.GuardError):
                    guard.inspect_assets(folder, "v9.9.9")
                (Path(folder) / stray).unlink()
            (Path(folder) / "LumaSuite-v9.9.9-macos-arm64.zip").unlink()
            with self.assertRaises(guard.GuardError):
                guard.inspect_assets(folder, "v9.9.9")

    def test_an_intel_mac_or_a_wrong_architecture_request_refuses_to_build(self):
        for machine in ("x86_64", "i386", "", "AMD64"):
            with self.subTest(machine=machine), self.assertRaises(SystemExit):
                build.resolve_macos_arch(None, machine)
        for requested in ("x86_64", "universal2", "intel", "arm"):
            with self.subTest(requested=requested), self.assertRaises(SystemExit):
                build.resolve_macos_arch(requested, "arm64")
        for machine in ("arm64", "ARM64", "aarch64"):
            self.assertEqual(build.resolve_macos_arch(None, machine), "arm64")
        self.assertEqual(build.resolve_macos_arch("arm64", "arm64"), "arm64")

    def test_the_build_script_has_no_linux_target(self):
        source = read("build_executable.py")
        code = "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("def build_linux", code)
        self.assertNotIn("dist/Linux", code)
        self.assertNotIn("'linux'", code)
        self.assertNotIn("x86_64'", code.replace("resolve_macos_arch", ""))

    def test_the_macos_bundle_is_signed_again_after_the_version_is_stamped(self):
        code = read("build_executable.py")
        stamp = code.index("stamp_macos_version(dist_target")
        sign = code.index("'codesign', '--force', '--deep', '--sign', '-'")
        verify = code.index("'codesign', '--verify', '--deep', '--strict'")
        self.assertLess(stamp, sign)
        self.assertLess(sign, verify)


class WorkflowMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = workflow_text()
        cls.raw = read(".github/workflows/build-cross-platform.yml")
        cls.build = job(cls.text, "build")
        cls.publish = job(cls.text, "publish-release")

    def test_no_job_builds_or_tests_on_intel_macos_or_linux(self):
        for forbidden in ("macos-26-intel", "macos-15-intel", "macos-13", "macos-12", "macos-latest", "macos-26",
                          "macos-intel", "macOS-intel", "x86_64 \n", "linux-x86_64", "tar.gz", "Linux", "xvfb",
                          "apt-get", "build_executable.py linux", "stage linux"):
            self.assertNotIn(forbidden, self.build, forbidden)
        self.assertNotIn("ubuntu", self.build)

    def test_the_runners_are_windows_x86_64_and_a_named_apple_silicon_runner(self):
        self.assertEqual(re.findall(r"- os: (\S+)", self.build), ["windows-latest", "macos-15"])
        self.assertEqual(re.findall(r"artifact_name: (\S+)", self.build),
                         ["LumaSuite-Windows-x86_64", "LumaSuite-macOS-arm64"])

    def test_the_architecture_is_asserted_rather_than_trusted(self):
        self.assertIn("Windows-X64|macOS-ARM64", self.build)
        self.assertIn('uname -m', self.build)
        self.assertIn("universal2", self.build)
        self.assertIn("lipo -archs", self.build)
        self.assertIn('test "$archs" = "arm64"', self.build)
        self.assertIn("tools/verify_macho_arch.py --path", self.build)
        self.assertIn("tools/verify_macho_arch.py --zip", self.build)
        self.assertIn("tools/verify_macho_arch.py --bundle-identity", self.build)
        self.assertIn("sys.exit(0 if machine == 0x8664 else", self.build)   # the Windows PE machine type is REQUIRED
        # an x86_64-only interpreter must be refused, not merely mentioned; the universal2 installer runs its arm64
        # slice and is allowed because the build is thinned to arm64 and the package is verified thin
        self.assertIn("sys.exit(1 if ('x86_64' in p and 'universal2' not in p) else 0)", self.build)
        self.assertIn("PYI_MACOS_ARCH: ${{ matrix.mac_arch }}", self.build)

    def test_the_macos_signature_state_is_asserted_as_documented(self):
        # Ad hoc, not Developer ID, not notarised: what README, the release notes, BUILD_STANDALONE and the
        # User Guide say. A change to that state has to change all of them.
        self.assertIn("codesign --verify --deep --strict", self.build)
        self.assertIn("Signature=adhoc", self.build)
        self.assertIn("Authority=", self.build)
        for relative in ("README.md", "RELEASE_NOTES.md", "BUILD_STANDALONE.md"):
            text = " ".join(read(relative).split())
            self.assertIn("ad hoc", text, relative)
            self.assertRegex(text, r"not Developer ID signed|not Developer ID signed|Developer ID signed and not notarised",
                             relative)
        guide = " ".join(re.sub(r"<[^>]+>", " ", read("ui/manual.html")).split())
        self.assertIn("ad hoc signature", guide)
        self.assertIn("not notarised", guide)

    def test_the_package_is_inspected_and_smoke_tested_on_every_distributed_row(self):
        self.assertLess(self.build.index("tools/inspect_release.py"), self.build.index("tests/smoke_packaged.py"))
        # a tagged run inspects the archive under its release tag; any other run inspects it as a diagnostic
        self.assertIn('python tools/inspect_release.py "$zip" --version "$RUN_REF_NAME"', self.build)
        self.assertIn("--diagnostic", self.build)
        self.assertIn('python tests/smoke_packaged.py "$exe"', self.build)
        self.assertIn("tools/release_guard.py inspect-exe", self.build)

    def test_only_zip_archives_are_collected_inspected_and_published(self):
        self.assertNotIn("tar.gz", self.text)
        self.assertIn("tools/release_guard.py inspect release-assets", self.publish)

    def test_the_orchestration_jobs_say_they_ship_nothing_built_on_their_runner(self):
        self.assertIn("SUPPORTED DISTRIBUTED PLATFORMS", self.raw)
        self.assertIn("TEST RUNNER", self.raw)
        self.assertIn("DISTRIBUTED", self.raw)
        self.assertIn("ubuntu-latest", job(self.text, "identity"))
        self.assertIn("ubuntu-latest", self.publish)

    def test_the_whole_matrix_still_gates_publication(self):
        self.assertIn("needs: [identity, build]", self.publish)
        self.assertIn("needs: identity", self.build)
        self.assertIn("fail-fast: false", self.build)               # every row reports; a partial set cannot publish

    def test_every_suite_this_file_relies_on_runs_in_ci(self):
        for suite in ("tests/test_platform_matrix.py", "tests/test_updater_proof.py", "tests/test_inspect_release.py",
                      "tests/test_release_guard.py", "tests/test_update_assets.py", "tests/test_dependency_hygiene.py"):
            self.assertIn("python " + suite, self.build, suite)


def _thin(cpu):
    return struct.pack("<Ii", 0xFEEDFACF, cpu) + b"\0" * 24


def _fat(*cpus):
    out = struct.pack(">II", 0xCAFEBABE, len(cpus))
    for cpu in cpus:
        out += struct.pack(">IIIII", cpu, 0, 0x1000, 0x1000, 14)
    return out + b"\0" * 32


ARM, X86 = macho.CPU_ARM64, macho.CPU_X86_64
MAIN = "LumaSuite.app/Contents/MacOS/LumaSuite"


class MachOVerifierTests(unittest.TestCase):
    def problems(self, entries, main="Contents/MacOS/LumaSuite"):
        return macho.problems_for(entries, "arm64", main)

    def test_a_thin_arm64_bundle_passes(self):
        self.assertEqual(self.problems([(MAIN, _thin(ARM)), ("LumaSuite.app/Contents/Frameworks/a.dylib", _thin(ARM)),
                                        ("LumaSuite.app/Contents/Resources/ui/index.html", b"<html>")]), [])

    def test_an_x86_64_launcher_is_refused(self):
        found = self.problems([(MAIN, _thin(X86))])
        self.assertTrue(any("x86_64" in p for p in found), found)

    def test_a_universal_launcher_is_refused_even_though_it_contains_arm64(self):
        found = self.problems([(MAIN, _fat(ARM, X86))])
        self.assertTrue(any("universal" in p for p in found), found)
        self.assertTrue(any("main executable" in p for p in found), found)

    def test_one_intel_library_inside_an_arm64_bundle_is_refused(self):
        found = self.problems([(MAIN, _thin(ARM)), ("LumaSuite.app/Contents/Frameworks/b.so", _thin(X86))])
        self.assertTrue(any("b.so" in p and "x86_64" in p for p in found), found)

    def test_a_universal_library_is_refused(self):
        found = self.problems([(MAIN, _thin(ARM)), ("x/libz.dylib", _fat(X86, ARM))])
        self.assertTrue(any("libz.dylib" in p and "universal" in p for p in found), found)

    def test_a_32_bit_or_unknown_cpu_is_refused_not_ignored(self):
        old = struct.pack("<Ii", 0xFEEDFACE, 7) + b"\0" * 24
        self.assertTrue(self.problems([(MAIN, _thin(ARM)), ("old", old)]))
        self.assertTrue(self.problems([(MAIN, _thin(0x01000099))]))

    def test_an_empty_or_wrong_package_is_not_a_pass(self):
        self.assertTrue(self.problems([]))
        self.assertTrue(self.problems([("a.txt", b"hello"), ("b.py", b"print()")]))
        found = self.problems([("LumaSuite.app/Contents/Frameworks/a.dylib", _thin(ARM))])
        self.assertTrue(any("main executable" in p for p in found), found)

    def test_an_empty_package_fails_on_its_own_even_when_no_main_executable_is_named(self):
        # The "nothing was found" rule is separate from the "main executable missing" rule; with no main named
        # only the first can refuse an empty or non-Mach-O package.
        found = macho.problems_for([], "arm64", None)
        self.assertTrue(any("no Mach-O file" in p for p in found), found)
        found = macho.problems_for([("a.txt", b"hello")], "arm64", None)
        self.assertTrue(any("no Mach-O file" in p for p in found), found)
        self.assertEqual(macho.problems_for([("x", _thin(ARM))], "arm64", None), [])

    def test_a_java_class_file_is_not_mistaken_for_a_universal_binary(self):
        java = struct.pack(">IHH", 0xCAFEBABE, 0, 52) + b"\0" * 32
        self.assertIsNone(macho.classify(java))
        self.assertEqual(self.problems([(MAIN, _thin(ARM)), ("x/Foo.class", java)]), [])

    def test_a_truncated_header_is_never_a_crash(self):
        for head in (b"", b"\xcf\xfa", b"\xca\xfe\xba\xbe", _fat(ARM, X86)[:10]):
            macho.classify(head)

    def test_zip_input_is_read_without_extracting(self):
        with tempfile.TemporaryDirectory() as folder:
            good = Path(folder) / "LumaSuite-v1.1.1-macos-arm64.zip"
            with zipfile.ZipFile(good, "w") as archive:
                archive.writestr(MAIN, _thin(ARM))
            self.assertEqual(macho.main(["--zip", str(good)]), 0)
            bad = Path(folder) / "bad.zip"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr(MAIN, _fat(ARM, X86))
            self.assertEqual(macho.main(["--zip", str(bad)]), 1)

    def test_directory_input_and_bundle_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "LumaSuite.app"
            (app / "Contents" / "MacOS").mkdir(parents=True)
            (app / "Contents" / "MacOS" / "LumaSuite").write_bytes(_thin(ARM))
            self.assertEqual(macho.main(["--path", str(app)]), 0)
            self.assertEqual(macho.main(["--bundle-identity", str(app)]), 1)        # no Info.plist yet
            info = {"CFBundleIdentifier": "com.lumasuite.app", "CFBundleExecutable": "LumaSuite",
                    "CFBundleShortVersionString": "1.1.1"}
            (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
            self.assertEqual(macho.main(["--bundle-identity", str(app)]), 0)
            info["CFBundleIdentifier"] = "SomethingElse"
            (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
            self.assertEqual(macho.main(["--bundle-identity", str(app)]), 1)

    def test_bundle_identity_decisions(self):
        good = {"CFBundleIdentifier": "com.lumasuite.app", "CFBundleExecutable": "LumaSuite",
                "CFBundleShortVersionString": "1.1.1"}
        self.assertEqual(macho.bundle_identity_problems(good), [])
        for key in good:
            broken = dict(good)
            broken.pop(key)
            self.assertTrue(macho.bundle_identity_problems(broken), key)
        broken = dict(good, CFBundleShortVersionString="0.0.0")
        self.assertTrue(macho.bundle_identity_problems(broken))
        # the identifier the verifier expects is the one the build gives the bundle
        self.assertIn("--osx-bundle-identifier=" + macho.BUNDLE_IDENTIFIER, read("build_executable.py"))


class LicenceMaterialTests(unittest.TestCase):
    """What a distribution carries for the licence and the LGPL component, asserted on the packages as the
    release workflow lays them out (`release_guard.stage`), with this product's inspector as the judge."""

    VERSION = "v9.9.9"

    def assemble(self, folder, platform_name):
        dist = Path(folder) / ("dist-" + platform_name)
        dist.mkdir()
        if platform_name == "windows":
            (dist / "LumaSuite.exe").write_bytes(b"MZ" + b"\0" * 2048)
            name = "LumaSuite-%s-windows-x86_64.zip" % self.VERSION
        else:
            (dist / "LumaSuite.app" / "Contents" / "MacOS").mkdir(parents=True)
            (dist / "LumaSuite.app" / "Contents" / "MacOS" / "LumaSuite").write_bytes(_thin(ARM))
            name = "LumaSuite-%s-macos-arm64.zip" % self.VERSION
        out = Path(folder) / ("stage-" + platform_name)
        guard.stage(platform_name, str(dist), str(out), root=str(ROOT))
        target = Path(folder) / name
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(out.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(out).as_posix())
        return target

    def test_both_distributed_packages_pass_the_strict_inspector_with_every_licence_requirement(self):
        for platform_name in ("windows", "macos"):
            with self.subTest(platform=platform_name), tempfile.TemporaryDirectory() as folder:
                target = self.assemble(folder, platform_name)
                self.assertEqual(inspect.problems_for(target, self.VERSION), [])
                names = {n for n, _ in inspect.members(target)}
                for required in guard.BESIDE_FILES:
                    self.assertIn(required, names)
                self.assertTrue({"LICENSE", "THIRD_PARTY_NOTICES.md", "REBUILD.md"} <= names)

    def test_losing_any_licence_requirement_fails_the_inspector(self):
        for missing in guard.BESIDE_FILES:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as folder:
                target = self.assemble(folder, "windows")
                stripped = Path(folder) / "stripped" / target.name
                stripped.parent.mkdir()
                with zipfile.ZipFile(target) as source, zipfile.ZipFile(stripped, "w") as out:
                    for info in source.infolist():
                        if info.filename != missing:
                            out.writestr(info, source.read(info))
                self.assertTrue(inspect.problems_for(stripped, self.VERSION), missing)

    def test_the_licence_is_the_hall_licence_with_the_approved_lgpl_section(self):
        text = read("LICENSE")
        self.assertIn("Hall Research Technologies Source-Available Software License", text)
        self.assertIn("11A. COMPONENTS LICENSED UNDER THE GNU LESSER GENERAL PUBLIC LICENSE", text)
        self.assertIn("you may replace an LGPL Component", " ".join(text.split()))
        self.assertNotIn("Business Edition", text)

    def test_the_gpl_and_lgpl_texts_and_the_exact_pystray_source_travel_with_the_package(self):
        gpl = read("licenses/pystray/COPYING")
        lgpl = read("licenses/pystray/COPYING.LGPL")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", gpl.upper())
        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE", lgpl.upper())
        info = read("third_party/pystray-0.19.5/source/lib/pystray/_info.py")
        self.assertIn("__version__ = (0, 19, 5)", info)
        provenance = read("third_party/pystray-0.19.5/PROVENANCE.md")
        self.assertIn("0.19.5", provenance)
        for digest in guard.PINNED_SHA256.values():
            self.assertIn(digest, provenance)
        self.assertIn("pystray==0.19.5", read("requirements-build.txt"))
        # rebuild/replacement instructions and provenance are in every package's required set
        for needed in ("REBUILD.md", "third_party/pystray-0.19.5/PROVENANCE.md"):
            self.assertIn(needed, guard.BESIDE_FILES)
        self.assertIn("pystray", read("REBUILD.md"))
        self.assertIn("python build_executable.py windows", read("REBUILD.md"))

    def test_no_ai_is_named_as_a_copyright_holder_anywhere_in_the_distributed_materials(self):
        holder = re.compile(r"copyright[^\n]{0,80}\b(claude|anthropic|openai|chatgpt|copilot|gemini)\b", re.I)
        attribution = re.compile(r"(co-authored-by|generated (with|by))[^\n]{0,60}\b(claude|anthropic|openai|chatgpt)\b",
                                 re.I)
        texts = [ROOT / "LICENSE", ROOT / "THIRD_PARTY_NOTICES.md", ROOT / "README.md", ROOT / "REBUILD.md",
                 ROOT / "ui" / "manual.html", ROOT / "third_party" / "pystray-0.19.5" / "PROVENANCE.md",
                 ROOT / "RELEASE_NOTES.md"]
        texts += sorted((ROOT / "licenses").rglob("*"))
        for path in texts:
            if not path.is_file():
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(holder.search(body), path.name)
            self.assertIsNone(attribution.search(body), path.name)
        self.assertIn("Copyright", read("LICENSE"))
        self.assertIn("Hall Research Technologies LLC", read("LICENSE"))


class WindowsTableOfContentsTests(unittest.TestCase):
    """The Windows build reads the executable's own table of contents. The reader is replaced by a fake that
    returns a chosen table, so the enforcement is exercised without building anything."""

    EVERY = ["LICENSE", "THIRD_PARTY_NOTICES.md", "licenses/MANIFEST.json", "licenses/pystray/COPYING",
             "licenses/pystray/COPYING.LGPL"]

    def check(self, names):
        reader = types.SimpleNamespace(CArchiveReader=lambda path: types.SimpleNamespace(
            toc={name: (0, 0, 0, 0, 0, "x") for name in names}))
        modules = {"PyInstaller": types.ModuleType("PyInstaller"),
                   "PyInstaller.archive": types.ModuleType("PyInstaller.archive"),
                   "PyInstaller.archive.readers": reader}
        with mock.patch.dict(sys.modules, modules):
            return guard.inspect_executable("LumaSuite.exe", "1.1.1")

    def test_the_complete_table_passes(self):
        self.assertEqual(self.check(self.EVERY + ["ui\\index.html", "licenses\\flask\\LICENSE.txt"]),
                         len(self.EVERY) + 2)

    def test_losing_any_licence_text_fails_the_build(self):
        for lost in self.EVERY:
            with self.subTest(lost=lost), self.assertRaises(guard.GuardError):
                self.check([n for n in self.EVERY if n != lost])

    def test_an_empty_table_is_a_failure_not_a_pass(self):
        # The refusal names the emptiness itself: a table that read as nothing would also lack the licence
        # texts, so only the message tells "parsed nothing" from "parsed something incomplete".
        with self.assertRaisesRegex(guard.GuardError, "empty"):
            self.check([])
        with self.assertRaisesRegex(guard.GuardError, "does not carry"):
            self.check(["ui/index.html"])

    def test_forbidden_content_in_the_table_fails_the_build(self):
        for name in ("server.pem", "firmware/x.bin", "units_cache.json", "archive/old.html"):
            with self.subTest(name=name), self.assertRaises(guard.GuardError):
                self.check(self.EVERY + [name])

    def test_the_windows_build_runs_the_check_right_after_pyinstaller(self):
        code = read("build_executable.py")
        windows = code[code.index("def build_windows"):code.index("def stamp_macos_version")]
        self.assertLess(windows.index("run_command(pyinstaller_cmd)"),
                        windows.index("assert_windows_package_contents(dist_target / 'LumaSuite.exe')"))
        self.assertIn("sys.exit(1)", code[code.index("def assert_windows_package_contents"):code.index("def build_windows")])


class UpdaterWordingTests(unittest.TestCase):
    def test_the_two_sentences_are_plain_and_name_no_internal_state(self):
        for sentence in (lumaserver.UPDATE_NO_PACKAGE_UNSUPPORTED, lumaserver.UPDATE_NO_PACKAGE_IN_RELEASE):
            self.assertNotRegex(sentence, r"unable_to_check|update_available|asset_url|_")
        self.assertIn("Windows (x86-64)", lumaserver.UPDATE_NO_PACKAGE_UNSUPPORTED)
        self.assertIn("Apple Silicon", lumaserver.UPDATE_NO_PACKAGE_UNSUPPORTED)

    def test_settings_shows_the_sentence_and_the_check_never_says_current_without_a_package_decision(self):
        source = read("ui/settings.js")
        function = source[source.index("function renderUpdateState"):source.index("function runUpdateCheck")]
        self.assertIn("data.package_message", function)
        self.assertLess(function.index("update_available"), function.index("package_message"))


if __name__ == "__main__":
    unittest.main()
