"""State-coloured text stays readable in every preset, in both modes.

A preset's green, amber, red and blue are FILLS: theme-boot.js writes them onto <html> and <body>, where they beat the
light-mode defaults in family.css. Used directly as text they measured 1.6:1 to 4.5:1 on a light page (the Link Speed
column in live use). Text therefore takes derived tokens: the preset colour mixed toward white on a dark page and toward
black on a light one. This test computes the contrast from the real preset values and the real mix recipe, so a change
to either is caught without a browser.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui")
DARK_SURFACE = (0x1D, 0x23, 0x2A)    # --surface on a dark page (measured: rgb(29, 35, 42))
LIGHT_SURFACE = (0xF7, 0xF8, 0xFB)   # --surface on a light page (measured: rgb(247, 248, 251))
ROLES = ("success", "warning", "error", "info")
MIN_CONTRAST = 4.5


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as handle:
        return handle.read()


def _hex(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, pa, b, pb):
    return tuple((x * pa + y * pb) / 100.0 for x, y in zip(a, b))


def _luminance(rgb):
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _presets():
    boot = _read("theme-boot.js")
    found = {}
    for block in re.finditer(r'\{\s*id:\s*"(\w+)".*?colors:\s*\{([^}]*)\}', boot, re.S):
        colors = dict(re.findall(r"(\w+):\s*\"(#[0-9a-fA-F]{6})\"", block.group(2)))
        found[block.group(1)] = colors
    return found


def _recipes(css):
    """role -> (dark recipe, light recipe), each (percent of the state colour, mixed-in colour, percent of the mix colour)."""
    out = {"dark": {}, "light": {}}
    dark = re.search(r":root,\s*body\s*\{([^}]*--state-success-text[^}]*)\}", css)
    light = re.search(r"html\.light,\s*body\.light\s*\{([^}]*--state-success-text[^}]*)\}", css)
    for mode, block in (("dark", dark), ("light", light)):
        if not block:
            continue
        for role, pa, colour, pb in re.findall(
                r"--state-(\w+)-text:\s*color-mix\(in srgb,\s*var\(--state-\1\)\s*(\d+)%,\s*(#[0-9a-fA-F]{6})\s*(\d+)%\)", block.group(1)):
            out[mode][role] = (int(pa), colour, int(pb))
    return out


class StateTextTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = _read("family.css")
        cls.recipes = _recipes(cls.css)
        cls.presets = _presets()

    def test_every_role_has_a_text_recipe_in_both_modes(self):
        for mode in ("dark", "light"):
            self.assertEqual(sorted(self.recipes[mode]), sorted(ROLES), "no %s-mode text recipe for every state colour" % mode)

    def test_every_mix_sums_to_100_so_nothing_goes_translucent(self):
        for mode, roles in self.recipes.items():
            for role, (pa, _colour, pb) in roles.items():
                self.assertEqual(pa + pb, 100, "%s/%s mix sums to %d" % (mode, role, pa + pb))

    def test_the_presets_were_found(self):
        self.assertGreaterEqual(len(self.presets), 7, sorted(self.presets))
        for name, colors in self.presets.items():
            for role in ROLES:
                self.assertIn(role, colors, "%s has no %s colour" % (name, role))

    def test_text_reaches_4_5_to_1_in_every_preset_and_mode(self):
        failures = []
        for name, colors in sorted(self.presets.items()):
            for mode, surface in (("dark", DARK_SURFACE), ("light", LIGHT_SURFACE)):
                for role in ROLES:
                    pa, mixed, pb = self.recipes[mode][role]
                    text = _mix(_hex(colors[role]), pa, _hex(mixed), pb)
                    ratio = _contrast(text, surface)
                    if ratio < MIN_CONTRAST:
                        failures.append("%s/%s/%s %.2f:1" % (name, mode, role, ratio))
        self.assertEqual(failures, [], "state text below %.1f:1" % MIN_CONTRAST)

    def test_the_raw_preset_fill_would_have_failed(self):
        """Proof the check can fail: the raw fills used as text, as before, miss the bar somewhere."""
        misses = []
        for name, colors in self.presets.items():
            for mode, surface in (("dark", DARK_SURFACE), ("light", LIGHT_SURFACE)):
                for role in ROLES:
                    if _contrast(_hex(colors[role]), surface) < MIN_CONTRAST:
                        misses.append((name, mode, role))
        self.assertTrue(misses, "the raw fills already pass; this test would prove nothing")


class StateTextRulesUseTheTokensTests(unittest.TestCase):
    RULES = (
        ("index.html", r"\.link-ok\s*\{\s*color:\s*var\(--state-success-text\)"),
        ("index.html", r"\.link-slow\s*\{\s*color:\s*var\(--state-error-text\)"),
        ("family.css", r"\.badge\.ok\s*\{[^}]*color:\s*var\(--state-success-text\)"),
        ("family.css", r"\.badge\.warn\s*\{[^}]*color:\s*var\(--state-warning-text\)"),
        ("family.css", r"\.modal-error\s*\{\s*color:\s*var\(--state-error-text\)"),
        ("admin_users.html", r"\.pill\.on\s*\{[^}]*color:\s*var\(--state-success-text\)"),
        ("admin_users.html", r"\.pill\.off\s*\{[^}]*color:\s*var\(--state-warning-text\)"),
        ("admin_users.html", r"\.inline-error\s*\{\s*color:\s*var\(--state-error-text\)"),
        ("remote.css", r"\.form-error\s*\{\s*color:\s*var\(--state-error-text\)"),
        ("remote.css", r"\.result-list \.ok\s*\{\s*color:\s*var\(--state-success-text\)"),
        ("remote.css", r"\.result-list \.bad\s*\{\s*color:\s*var\(--state-error-text\)"),
        ("remote.css", r"\.pick\.offline \.pick-sub\s*\{\s*color:\s*var\(--state-warning-text\)"),
        # The application's own version status: current green, update available red.
        ("family.css", r"\.update-status\.ok\s*\{\s*color:\s*var\(--state-success-text\)"),
        ("family.css", r"\.update-status\.update-available\s*\{[^}]*color:\s*var\(--state-error-text\)"),
    )

    def test_update_available_is_text_not_a_filled_badge(self):
        rule = re.search(r"\.update-status\.update-available\s*\{([^}]*)\}", re.sub(r"/\*.*?\*/", "", _read("family.css"), flags=re.S))
        self.assertTrue(rule)
        self.assertNotRegex(rule.group(1), r"background|--on-warning", "the status is coloured text, not a fill")

    def test_state_coloured_text_uses_the_derived_tokens(self):
        for name, pattern in self.RULES:
            self.assertRegex(_read(name), pattern, "%s: %s" % (name, pattern))

    def test_no_state_colour_is_used_directly_as_text(self):
        """A bare `color: var(--state-*)` (not border-color / background-color) is the defect this fixes."""
        offenders = []
        for name in os.listdir(UI):
            if not name.endswith((".html", ".css")):
                continue
            text = re.sub(r"/\*.*?\*/", "", _read(name), flags=re.S)
            for hit in re.finditer(r"(?<![\w-])color:\s*var\(--state-(success|warning|error|info)\)", text):
                offenders.append("%s: %s" % (name, text[max(0, hit.start() - 40):hit.end()].replace("\n", " ")))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
