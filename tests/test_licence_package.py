"""LumaSuite's licence, section 11A, and the pystray source that travels with it.

The approved licensing direction: LumaSuite is under the Hall Research source-available licence with section 11A
(LGPL components), the exact corresponding pystray 0.19.5 source ships unmodified, and a recipient can rebuild with a
modified copy. These tests pin the files, the hashes and the packaging so none of it can drift silently.
"""
import hashlib
import io
import os
import tarfile
import unittest
import zipfile

import _support

ROOT = _support.ROOT
SRC = os.path.join(ROOT, "third_party", "pystray-0.19.5")

APPROVED_11A = """11A. COMPONENTS LICENSED UNDER THE GNU LESSER GENERAL PUBLIC LICENSE

Some Third-Party Components, identified in THIRD_PARTY_NOTICES.md, are
licensed under the GNU Lesser General Public License version 3 (each, an
"LGPL Component"). Each LGPL Component remains governed by its own LGPL
license and not by this license. Notwithstanding any other provision of this
license, but only to the extent necessary to preserve the rights that the LGPL
grants you:

  (a) nothing in this license prohibits you from modifying an LGPL Component
      as the LGPL permits;

  (b) nothing in this license prohibits you from reverse engineering the
      Software to the extent the LGPL requires you to be permitted to do so
      in order to debug such modifications; and

  (c) you may replace an LGPL Component contained in the Software with a
      modified, interface-compatible version, and rebuild or recombine the
      Software with it using the source code and build instructions that Hall
      publishes, for your own permitted use, as necessary to exercise those
      rights.

This section does not relicense, and grants no right in, the code and content
of the Software that Hall owns beyond what sections 3 through 9 grant. Every
other provision of this license, including sections 5, 7, 8 and 10, continues
to apply to the Software as a whole, except to the extent necessary to
preserve the rights described in this section. Nothing in this license
modifies the text or terms of any LGPL.
"""
TARBALL_SHA = "2ab6bd15c14b400c9e8271a82c9de8adc0f1a0cacfc14080bdd4fb42bb88ec92"
WHEEL_SHA = "a0c2229d02cf87207297c22d86ffc57c86c227517b038c0d3c59df79295ac617"  # the digest PyPI publishes


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def sha(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class LicenceTextTests(unittest.TestCase):
    def test_the_licence_is_the_hall_licence_for_lumasuite_with_the_approved_section_11a(self):
        text = read("LICENSE")
        self.assertTrue(text.startswith("Hall Research Technologies Source-Available Software License\nVersion 1.0"))
        self.assertIn("Copyright \u00a9 2026 Hall Research Technologies LLC", text)
        self.assertNotIn("OmniSuite", text)
        self.assertIn(APPROVED_11A, text)

    def test_section_11a_sits_after_section_11_and_before_section_12(self):
        text = read("LICENSE")
        self.assertLess(text.index("11. THIRD-PARTY COMPONENTS"), text.index("11A. COMPONENTS"))
        self.assertLess(text.index("11A. COMPONENTS"), text.index("12. NO WARRANTY"))

    def test_the_survival_list_names_11a(self):
        self.assertIn("11, 11A, 12, 13 and 15 survive termination", " ".join(read("LICENSE").split()))

    def test_the_licence_is_served_by_the_application(self):
        import lumaserver
        response = lumaserver.APP.test_client().get("/license")
        self.assertEqual(response.status_code, 200)
        self.assertIn("11A. COMPONENTS LICENSED UNDER", response.get_data(as_text=True))


class PystraySourceTests(unittest.TestCase):
    def test_the_shipped_upstream_files_are_the_published_bytes(self):
        self.assertEqual(sha(os.path.join(SRC, "pystray-0.19.5-github-source.tar.gz")), TARBALL_SHA)
        self.assertEqual(sha(os.path.join(SRC, "pystray-0.19.5-py2.py3-none-any.whl")), WHEEL_SHA)

    def test_the_extracted_source_equals_the_archive_member_for_member(self):
        archive = tarfile.open(os.path.join(SRC, "pystray-0.19.5-github-source.tar.gz"))
        seen = 0
        for member in archive.getmembers():
            if not member.isfile():
                continue
            relative = member.name.split("/", 1)[1]
            path = os.path.join(SRC, "source", *relative.split("/"))
            self.assertTrue(os.path.isfile(path), relative)
            self.assertEqual(open(path, "rb").read(), archive.extractfile(member).read(), relative)
            seen += 1
        self.assertGreaterEqual(seen, 20)

    def test_the_wheel_modules_equal_the_source_modules(self):
        wheel = zipfile.ZipFile(os.path.join(SRC, "pystray-0.19.5-py2.py3-none-any.whl"))
        compared = 0
        for name in wheel.namelist():
            if name.startswith("pystray/") and name.endswith(".py"):
                path = os.path.join(SRC, "source", "lib", *name.split("/"))
                self.assertEqual(wheel.read(name), open(path, "rb").read(), name)
                compared += 1
        self.assertEqual(compared, 13)

    def test_upstream_licence_files_are_preserved(self):
        for name in ("COPYING", "COPYING.LGPL"):
            self.assertGreater(os.path.getsize(os.path.join(SRC, "source", name)), 5000, name)
            upstream = open(os.path.join(SRC, "source", name), "rb").read().replace(b"\r\n", b"\n")
            collected = open(os.path.join(ROOT, "licenses", "pystray", name), "rb").read().replace(b"\r\n", b"\n")
            self.assertEqual(upstream, collected, name)

    def test_the_provenance_records_both_hashes_and_the_upstream_locations(self):
        text = read("third_party", "pystray-0.19.5", "PROVENANCE.md")
        for needle in (TARBALL_SHA, WHEEL_SHA, "github.com/moses-palmer/pystray", "pypi.org/project/pystray/0.19.5"):
            self.assertIn(needle, text)

    def test_the_build_pins_the_same_version(self):
        self.assertIn("pystray==0.19.5", read("requirements-build-lock.txt"))


class NoticesAndRebuildTests(unittest.TestCase):
    def test_the_notices_point_at_the_source_the_licence_and_the_rebuild_steps(self):
        notices = " ".join(read("THIRD_PARTY_NOTICES.md").split())
        for needle in ("third_party/pystray-0.19.5/", "REBUILD.md", "section 11A", "licenses/pystray/COPYING.LGPL"):
            self.assertIn(needle, notices)
        self.assertNotIn("open item", notices)

    def test_the_rebuild_steps_name_every_command(self):
        text = read("REBUILD.md")
        for needle in ("requirements-build-lock.txt", "pip install --no-deps --force-reinstall",
                       "build_executable.py windows", "third_party/pystray-0.19.5/source/", "Section 11A"):
            self.assertIn(needle if needle != "Section 11A" else "11A", text)

    def test_every_build_packages_the_licence(self):
        build = read("build_executable.py")
        self.assertEqual(build.count("{LICENSE_FILE}{add_data_sep}."), 2, "Windows and macOS")

    def test_third_party_source_is_kept_byte_for_byte_by_git(self):
        self.assertIn("third_party/** -text", read(".gitattributes"))


if __name__ == "__main__":
    unittest.main()
