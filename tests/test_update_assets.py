"""The update check offers the download built for this computer, or none.

It used to offer the first file of the newest release, which on a release carrying four archives is the
Windows one for everybody. An artifact name is a contract with whatever selects it, so these tests are
tied to what the workflow and the build script actually produce, not to a list typed here: the archive
names are read from the workflow's packaging steps, and the executable that goes into the Windows
archive is the one `build_executable.py` writes.
"""
import os
import re
import unittest

import _support

import lumaserver

ROOT = _support.ROOT
BASE = "https://github.com/Hall-Research-Technologies/LumaSuite/releases/download/v9.9.9/"


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def workflow_asset_names(tag="v9.9.9"):
    """The archive names the workflow's packaging steps produce, with the tag filled in."""
    text = read(".github", "workflows", "build-cross-platform.yml")
    out = []
    for template in re.findall(
            r"LumaSuite-\$\{\{ github\.ref_name \}\}-(macos-\$\{\{ matrix\.mac_arch \}\}\.zip|[^\"\s]+)", text):
        if "matrix.mac_arch" in template:
            for arch in re.findall(r"mac_arch: (\S+)", text):
                arch = arch.strip('"')
                if arch:
                    out.append("LumaSuite-%s-%s" % (tag, template.replace("${{ matrix.mac_arch }}", arch)))
        else:
            out.append("LumaSuite-%s-%s" % (tag, template))
    return out


def assets(names):
    return [{"name": n, "browser_download_url": BASE + n} for n in names]


class AssetSelectionTests(unittest.TestCase):
    def pick(self, names, platform_name, machine):
        return lumaserver._pick_platform_asset(assets(names), platform_name, machine)

    def test_the_workflow_produces_four_archives_and_each_platform_picks_its_own(self):
        names = workflow_asset_names()
        self.assertEqual(sorted(names), sorted([
            "LumaSuite-v9.9.9-windows-x86_64.zip", "LumaSuite-v9.9.9-macos-x86_64.zip",
            "LumaSuite-v9.9.9-macos-arm64.zip", "LumaSuite-v9.9.9-linux-x86_64.tar.gz"]))
        for order in (names, list(reversed(names))):
            self.assertEqual(self.pick(order, "win32", "AMD64"), BASE + "LumaSuite-v9.9.9-windows-x86_64.zip")
            self.assertEqual(self.pick(order, "darwin", "x86_64"), BASE + "LumaSuite-v9.9.9-macos-x86_64.zip")
            self.assertEqual(self.pick(order, "darwin", "arm64"), BASE + "LumaSuite-v9.9.9-macos-arm64.zip")
            self.assertEqual(self.pick(order, "linux", "x86_64"), BASE + "LumaSuite-v9.9.9-linux-x86_64.tar.gz")

    def test_every_asset_names_its_operating_system_and_architecture(self):
        for name in workflow_asset_names():
            self.assertRegex(name, r"-(windows|macos|linux)-(x86_64|arm64)\.(zip|tar\.gz)$", name)

    def test_the_windows_archive_holds_the_executable_the_build_really_writes(self):
        workflow = read(".github", "workflows", "build-cross-platform.yml")
        packaged = re.search(r"Compress-Archive -Path (\S+) ", workflow).group(1)
        self.assertEqual(packaged, "stage/*")
        self.assertIn("release_guard.py stage windows dist/Windows stage", workflow)
        build = read("build_executable.py")
        self.assertIn("dist' / 'Windows'", build)
        self.assertIn("name=LumaSuite", build)
        # the same file handed out bare, as a release candidate is, is the same artifact to the matcher
        self.assertEqual(self.pick(["LumaSuite-v1.1.0-weekend-rc2-windows-x86_64.exe"], "win32", "AMD64"),
                         BASE + "LumaSuite-v1.1.0-weekend-rc2-windows-x86_64.exe")
        # and a release carrying both offers the archive
        both = ["LumaSuite-v1.1.0-windows-x86_64.exe", "LumaSuite-v1.1.0-windows-x86_64.zip"]
        self.assertTrue(self.pick(both, "win32", "AMD64").endswith(".zip"))

    def test_a_computer_nothing_was_built_for_is_offered_no_download(self):
        names = workflow_asset_names()
        for platform_name, machine in (("win32", "ARM64"), ("linux", "aarch64"), ("freebsd14", "amd64"),
                                       ("linux", "unknown"), ("darwin", "ppc")):
            self.assertIsNone(self.pick(names, platform_name, machine), (platform_name, machine))

    def test_a_release_without_this_platforms_archive_offers_none_not_another(self):
        self.assertIsNone(self.pick(["LumaSuite-v9.9.9-windows-x86_64.zip"], "linux", "x86_64"))
        self.assertIsNone(self.pick([], "win32", "AMD64"))

    def test_the_old_names_and_look_alikes_are_not_picked(self):
        for name in ("notes-windows-x86_64.zip", "LumaSuite-v9.9.9-windows-x86_64.zip.sig",
                     "LumaSuite-v9.9.9-windows.zip", "LumaSuite-v9.9.9-windows-arm64.zip",
                     "LumaSuite-v9.9.9-windows-x86_64.msi"):
            self.assertIsNone(self.pick([name], "win32", "AMD64"), name)

    def test_an_asset_without_a_download_address_is_skipped(self):
        broken = [{"name": "LumaSuite-v9.9.9-windows-x86_64.zip"}]
        self.assertIsNone(lumaserver._pick_platform_asset(broken, "win32", "AMD64"))

    def test_the_update_check_uses_the_picker_and_not_the_first_asset(self):
        import inspect
        source = inspect.getsource(lumaserver._run_update_check)
        self.assertIn("_pick_platform_asset", source)
        self.assertNotIn('asset_url = asset["browser_download_url"]', source)


if __name__ == "__main__":
    unittest.main()
