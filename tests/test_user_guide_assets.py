"""The User Guide's pictures are part of the release, so they are checked like code.

The guide (ui/manual.html) ships inside the application with its screenshots in ui/guide/. A picture that
does not resolve, an image nothing refers to, a link to a section that is not there, figures that are not
numbered in order, or an address or path that belongs to a real network in a document: each of those is a
defect an operator would see, and none of them fails a build unless it is asserted here.
"""
import os
import re
import struct
import unittest

import _support

ROOT = _support.ROOT
UI = os.path.join(ROOT, "ui")
GUIDE = os.path.join(UI, "guide")
SHOTS = os.path.join(ROOT, "docs", "screenshots")

# Largest a single guide picture or the whole set may be: the screenshots are palette-optimised, and a
# raw capture dropped in unchanged would add megabytes to every download.
MAX_ONE = 220 * 1024
MAX_ALL = 1500 * 1024


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def png_size(path):
    with open(path, "rb") as handle:
        head = handle.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


class GuideFigureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read("ui", "manual.html")
        cls.images = re.findall(r'<img\b[^>]*\bsrc="guide/([^"]+)"[^>]*>', cls.html)
        cls.tags = re.findall(r'<img\b[^>]*\bsrc="guide/[^"]+"[^>]*>', cls.html)

    def test_the_guide_has_figures(self):
        self.assertGreaterEqual(len(self.images), 25)

    def test_every_picture_the_guide_names_is_in_the_package_folder(self):
        for name in self.images:
            self.assertTrue(os.path.isfile(os.path.join(GUIDE, name)), name)

    def test_no_picture_sits_in_the_folder_unused(self):
        used = set(self.images)
        present = {n for n in os.listdir(GUIDE) if n.lower().endswith((".png", ".jpg"))}
        self.assertEqual(sorted(present - used), [], "an image nothing in the guide refers to is dead weight in every download")

    def test_a_picture_is_used_once(self):
        self.assertEqual(len(self.images), len(set(self.images)))

    def test_every_picture_has_alt_text_and_its_real_size(self):
        for tag, name in zip(self.tags, self.images):
            alt = re.search(r'\balt="([^"]*)"', tag)
            self.assertTrue(alt and len(alt.group(1).split()) >= 5, name + ": alt text that says what the picture shows")
            width, height = png_size(os.path.join(GUIDE, name))
            self.assertIn('width="%d"' % width, tag, name)
            self.assertIn('height="%d"' % height, tag, name)

    def test_figures_are_numbered_in_order_and_each_has_a_target(self):
        numbers = [int(n) for n in re.findall(r"<strong>Figure (\d+)\.</strong>", self.html)]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertEqual(len(numbers), len(self.images))
        ids = re.findall(r'<figure class="figure" id="(fig-[\w-]+)"', self.html)
        self.assertEqual(len(ids), len(set(ids)))
        for target in re.findall(r'href="#(fig-[\w-]+)"', self.html):
            self.assertIn(target, ids)

    def test_the_numbers_a_sentence_quotes_are_the_figures_they_point_to(self):
        figure_number = {name: int(n) for name, n in
                         re.findall(r'<figure class="figure" id="fig-([\w-]+)">.*?<strong>Figure (\d+)\.</strong>',
                                    self.html, flags=re.S)}
        for name, text in re.findall(r'<a href="#fig-([\w-]+)">(\d+)</a>', self.html):
            self.assertEqual(figure_number[name], int(text), name)

    def test_every_in_page_link_goes_somewhere(self):
        ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        for target in re.findall(r'href="#([^"]+)"', self.html):
            self.assertIn(target, ids, target)

    def test_every_callout_list_belongs_to_a_figure(self):
        for block in re.findall(r"<ol class=\"callouts\">(.*?)</ol>", self.html, flags=re.S):
            self.assertGreaterEqual(block.count("<li>"), 1)
        # a legend is only ever inside a figure caption
        outside = re.sub(r"<figure class=\"figure\".*?</figure>", "", self.html, flags=re.S)
        self.assertNotIn('class="callouts"', outside)

    def test_the_pictures_are_small_enough_to_ship(self):
        total = 0
        for name in sorted(os.listdir(GUIDE)):
            size = os.path.getsize(os.path.join(GUIDE, name))
            total += size
            self.assertLessEqual(size, MAX_ONE, name)
        self.assertLessEqual(total, MAX_ALL)

    def test_the_guide_pictures_are_served_to_the_operator_pages_only(self):
        import luma_access
        self.assertIs(luma_access.classify_ui_file("guide/dm-overview.png"), luma_access.Access.ADMIN)
        # the public image list names files, never a folder
        for entry in luma_access.PUBLIC_UI_FILES:
            self.assertNotIn("guide", entry)

    def test_the_package_build_does_not_leave_the_pictures_out(self):
        build = read("build_executable.py")
        match = re.search(r"ignore_patterns\(([^)]*)\)", build)
        self.assertTrue(match)
        for pattern in re.findall(r"'([^']+)'", match.group(1)):
            self.assertNotIn(pattern, ("guide", "guide/*", "*.png", "*.png"), "the build would drop the guide's pictures")


class NoRealNetworkInTheDocumentsTests(unittest.TestCase):
    """Screenshots and text use documentation addresses (198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24)."""
    PRIVATE = re.compile(r"(?<![\d.])(192\.168\.\d+\.\d+|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)(?![\d])")
    WINDOWS_USER = re.compile(r"[A-Za-z]:\\Users\\(?!Operator\b|Public\b|Default\b)[^\\\s<\"']+", re.I)

    def test_the_documents_name_no_private_address_and_no_user_profile(self):
        for relative in ("README.md", "RELEASE_NOTES.md", "CHANGELOG.md", "ui/manual.html", "BUILD_STANDALONE.md"):
            text = read(*relative.split("/"))
            self.assertEqual(self.PRIVATE.findall(text), [], relative)
            self.assertEqual(self.WINDOWS_USER.findall(text), [], relative)


class ReadmeScreenshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = read("README.md")
        cls.refs = re.findall(r"!\[([^\]]*)\]\((docs/screenshots/[^)]+)\)", cls.readme)

    def test_the_readme_shows_its_screenshots_with_alt_text(self):
        self.assertGreaterEqual(len(self.refs), 3)
        for alt, ref in self.refs:
            self.assertGreaterEqual(len(alt.split()), 5, ref)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, *ref.split("/"))), ref)

    def test_every_published_screenshot_is_used(self):
        used = {os.path.basename(ref) for _alt, ref in self.refs}
        self.assertEqual(sorted(set(os.listdir(SHOTS)) - used), [])

    def test_published_documentation_is_not_packaged_and_packaged_assets_are_not_published_documentation(self):
        # two lists: what is in Git for readers of the repository, and what is inside the executable
        self.assertFalse(os.path.exists(os.path.join(UI, "screenshots")))
        for name in os.listdir(SHOTS):
            self.assertFalse(os.path.exists(os.path.join(GUIDE, name)), name + " would be the same file in both lists")


if __name__ == "__main__":
    unittest.main()
