"""Release candidates and look-alikes are never a production update.

The update check reads GitHub's "latest" release. That flag is a channel, not an identity, so the check also
requires the tag to be exactly v<major>.<minor>.<patch> and the download to be named for that exact tag.
Deterministic: no network (requests.get is replaced for the span of each test and restored).
"""
import unittest
from unittest import mock

import _support  # noqa: F401  (installs the hardware fence)

import lumaserver

BASE = "https://github.com/Hall-Research-Technologies/LumaSuite/releases/download/"


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def release(tag, names, **extra):
    out = {"tag_name": tag, "html_url": "https://github.com/Hall-Research-Technologies/LumaSuite/releases/tag/" + tag,
           "assets": [{"name": n, "browser_download_url": BASE + tag + "/" + n} for n in names]}
    out.update(extra)
    return out


def run_check(payload, installed="1.0.8", platform_name="win32", machine="AMD64"):
    with mock.patch.object(lumaserver, "__version__", installed), \
            mock.patch.object(lumaserver.requests, "get", return_value=_Resp(payload)), \
            mock.patch.object(lumaserver.sys, "platform", platform_name), \
            mock.patch.object(lumaserver.platform, "machine", return_value=machine):
        return lumaserver._run_update_check()


class ProductionIdentityTests(unittest.TestCase):
    GOOD = ["LumaSuite-v1.1.0-windows-x86_64.zip"]

    def test_the_exact_production_release_is_offered(self):
        out = run_check(release("v1.1.0", self.GOOD))
        self.assertEqual(out["state"], "update_available")
        self.assertTrue(out["asset_url"].endswith("LumaSuite-v1.1.0-windows-x86_64.zip"))

    def test_tags_that_are_not_exactly_vx_y_z_are_not_production(self):
        for tag in ("v1.1.0-rc4", "v1.1.0-weekend-rc4", "V1.1.0", "v1.1", "v1.1.0\n", "v1.1.0.1", "1.1.0", "v1.1.0-test", "v1.1.0-dev"):
            out = run_check(release(tag, self.GOOD))
            self.assertEqual(out["state"], "unable_to_check", tag)
            self.assertEqual(out["detail"], "not_a_production_release", tag)
            self.assertNotIn("asset_url", out, tag)

    def test_a_prerelease_or_draft_is_never_offered_even_with_a_clean_tag(self):
        for flag in ("prerelease", "draft"):
            out = run_check(release("v1.1.0", self.GOOD, **{flag: True}))
            self.assertEqual(out["state"], "unable_to_check", flag)

    def test_a_refusal_is_never_reported_as_current(self):
        self.assertNotEqual(run_check(release("v1.1.0-rc4", self.GOOD), installed="1.1.0")["state"], "current")

    def test_a_candidate_executable_cannot_match_a_production_release(self):
        names = ["LumaSuite-v1.1.0-weekend-rc4-windows-x86_64.exe"]
        out = run_check(release("v1.1.0", names))
        self.assertEqual(out["state"], "update_available")
        self.assertIsNone(out["asset_url"])          # the release page, never the candidate

    def test_an_asset_for_another_tag_is_not_picked(self):
        out = run_check(release("v1.1.0", ["LumaSuite-v1.0.8-windows-x86_64.zip"]))
        self.assertIsNone(out["asset_url"])

    def test_the_tag_check_is_exact_in_the_picker_too(self):
        pick = lumaserver._pick_platform_asset
        a = [{"name": "LumaSuite-v1.1.0-rc4-windows-x86_64.zip", "browser_download_url": BASE + "x"}]
        self.assertIsNone(pick(a, "win32", "AMD64", tag="v1.1.0"))
        a = [{"name": "LumaSuite-v1.1.0-windows-x86_64.zip", "browser_download_url": BASE + "y"}]
        self.assertEqual(pick(a, "win32", "AMD64", tag="v1.1.0"), BASE + "y")

    def test_the_source_version_has_no_candidate_suffix(self):
        self.assertRegex(lumaserver.__version__, r"\d+\.\d+\.\d+\Z")


if __name__ == "__main__":
    unittest.main()
