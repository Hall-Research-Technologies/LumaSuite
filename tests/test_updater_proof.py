"""The update check, proved against scripted GitHub release feeds, with the old logic running verbatim.

Two implementations of the application update check are put in front of the same scripted GitHub:

* NEW      lumaserver._run_update_check, this tree (the code that ships in v1.1.1);
* V1.1.0   the check exactly as it stands in the tag v1.1.0, extracted with `git show` into
           tests/fixtures/legacy_update_check_v1_1_0.py and pinned by SHA-256 so it cannot be edited to agree
           with today's behaviour.

Which old installed versions exist in the field, and what each can do (proved below, not asserted):

* v1.0.8 and every earlier release has NO update check at all: no request to GitHub, no route, no Settings
  control. Nothing about a release feed can reach it. (`test_no_release_before_v1_1_0_can_check_for_updates`.)
* v1.1.0 was never published; its check exists on test machines only. It reads `/releases/latest`, the most
  recently CREATED published, non-draft, non-prerelease release (not the highest version), and picks the
  download by exact name `LumaSuite-<tag><platform tail>`. It cannot be changed, so what is published has to
  keep working with it: that is the contract these tests hold.

The scripted GitHub models what GitHub's `/releases/latest` does: it serves the newest-created release that is
neither a draft nor a prerelease. The scripted transport is the real `requests.get` replaced, so each
implementation's own URL, status handling and JSON handling are exercised. No network is touched.

The three outcomes are never confused: an update is offered only for a production release, "current" is
claimed only when the answer is proved, and everything else (a network failure, a refusal, a malformed answer)
is `unable_to_check`, never "up to date".
"""
import hashlib
import importlib.util
import os
import subprocess
import types
import unittest
from unittest import mock

import _support

import lumaserver

ROOT = _support.ROOT
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "legacy_update_check_v1_1_0.py")
MARKER = "# ===== VERBATIM FROM v1.1.0 BELOW THIS LINE =====\n"
#: SHA-256 of the frozen block (LF line endings), as extracted from tag v1.1.0.
FROZEN_V110_SHA256 = "091591ca0250fed2a47533e1dd839bf66ddaa7609123ecc12372b05023215929"

REPO = "Hall-Research-Technologies/LumaSuite"
LATEST_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
DL = "https://github.com/%s/releases/download/" % REPO

WIN = "LumaSuite-v1.1.1-windows-x86_64.zip"
MAC = "LumaSuite-v1.1.1-macos-arm64.zip"
SUMS = "SHA256SUMS.txt"

# (label, sys.platform, platform.machine())
WIN_X64 = ("Windows x86-64", "win32", "AMD64")
MAC_ARM = ("macOS arm64", "darwin", "arm64")
WIN_ARM = ("Windows on Arm", "win32", "ARM64")
MAC_INTEL = ("macOS Intel", "darwin", "x86_64")
LINUX_X64 = ("Linux x86-64", "linux", "x86_64")
LINUX_ARM = ("Linux arm64", "linux", "aarch64")
UNKNOWN = ("an unidentified OS", "freebsd14", "amd64")


# ----------------------------------------------------------------- the scripted GitHub
class _NotJson:
    pass


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is _NotJson:
            raise ValueError("not json")
        return self._payload


class ScriptedGitHub:
    """What `requests.get` answers. `feed` is in CREATION order, oldest first; GitHub's /releases/latest
    is the newest-created release that is neither a draft nor a prerelease."""

    def __init__(self, feed=None, status=200, raises=None, payload="auto"):
        self.feed, self.status, self.raises, self.payload = feed or [], status, raises, payload
        self.calls = []

    def latest(self):
        for item in reversed(self.feed):
            if isinstance(item, dict) and not item.get("draft") and not item.get("prerelease"):
                return item
        return None

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        if url != LATEST_URL:
            return Resp(404, {"message": "Not Found"})
        if self.payload != "auto":
            return Resp(self.status, self.payload)
        found = self.latest()
        if found is None:
            return Resp(404, {"message": "Not Found"})
        return Resp(self.status, found)


def release(tag, names=(WIN, MAC, SUMS), **extra):
    out = {"tag_name": tag, "name": tag, "html_url": "https://github.com/%s/releases/tag/%s" % (REPO, tag),
           "assets": [{"name": n, "browser_download_url": DL + tag + "/" + n} for n in names]}
    out.update(extra)
    return out


# ----------------------------------------------------------------- the two implementations
def _read_fixture():
    with open(FIXTURE, "rb") as handle:
        return handle.read()


def _load_frozen():
    spec = importlib.util.spec_from_file_location("legacy_update_check_v1_1_0", FIXTURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_new(script, installed, target):
    _label, plat, machine = target
    with mock.patch.object(lumaserver, "__version__", installed), \
            mock.patch.object(lumaserver.requests, "get", script.get), \
            mock.patch.object(lumaserver.sys, "platform", plat), \
            mock.patch.object(lumaserver.platform, "machine", return_value=machine):
        return lumaserver._run_update_check()


def run_old(script, installed, target):
    _label, plat, machine = target
    module = _load_frozen()
    module.requests = types.SimpleNamespace(get=script.get)
    module.sys = types.SimpleNamespace(platform=plat)
    module.platform = types.SimpleNamespace(machine=lambda: machine)
    module.__version__ = installed
    return module._run_update_check()


IMPLEMENTATIONS = (("NEW", run_new), ("V1.1.0", run_old))


def asset_name(result):
    url = (result or {}).get("asset_url")
    return url.rsplit("/", 1)[-1] if url else None


class Both(unittest.TestCase):
    """Run one scenario through every implementation and hand back {name: result}."""

    def run_all(self, feed=None, installed="1.1.0", target=WIN_X64, **script_args):
        out = {}
        for name, runner in IMPLEMENTATIONS:
            script = ScriptedGitHub(feed, **script_args)
            out[name] = runner(script, installed, target)
            self.assertTrue(script.calls, name + " never asked")
            self.assertEqual(script.calls[0]["url"], LATEST_URL, name)
        return out

    def each(self, results, check):
        for name, result in results.items():
            with self.subTest(implementation=name):
                check(result)


# ----------------------------------------------------------------- the frozen fixture is what it says
class FixtureIntegrityTests(unittest.TestCase):
    def test_the_frozen_block_is_pinned_by_hash(self):
        text = _read_fixture().decode("utf-8").replace("\r\n", "\n")
        block = text.split(MARKER, 1)[1]
        self.assertEqual(hashlib.sha256(block.encode("utf-8")).hexdigest(), FROZEN_V110_SHA256)

    def test_the_frozen_block_is_identical_to_the_tag_when_the_tag_is_available(self):
        try:
            shown = subprocess.run(["git", "show", "v1.1.0:lumaserver.py"], cwd=ROOT, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git is not available here; the pinned hash above is the check")
        if shown.returncode != 0:
            self.skipTest("the v1.1.0 tag is not in this clone (a shallow CI checkout); the pinned hash is the check")
        source = shown.stdout.decode("utf-8").replace("\r\n", "\n")
        start = source.index("# -------- APPLICATION UPDATE CHECK")
        end = source.index('@APP.get("/api/update_check"')
        block = _read_fixture().decode("utf-8").replace("\r\n", "\n").split(MARKER, 1)[1]
        self.assertEqual(source[start:end], block)

    def test_no_release_before_v1_1_0_can_check_for_updates(self):
        """v1.0.8 is the published `latest` and has no update check to feed: no request to GitHub at all."""
        problems = []
        for tag in ("v1.0.8", "v1.0.7"):
            try:
                names = subprocess.run(["git", "ls-tree", "-r", "--name-only", tag], cwd=ROOT, capture_output=True,
                                       timeout=30)
            except (OSError, subprocess.SubprocessError):
                self.skipTest("git is not available here")
            if names.returncode != 0:
                self.skipTest("the %s tag is not in this clone (a shallow CI checkout)" % tag)
            for path in names.stdout.decode("utf-8").split():
                if path.endswith((".py", ".js", ".html")):
                    body = subprocess.run(["git", "show", "%s:%s" % (tag, path)], cwd=ROOT, capture_output=True,
                                          timeout=30).stdout.decode("utf-8", "replace")
                    for needle in ("api.github.com", "releases/latest", "/api/update_check", "update_check"):
                        if needle in body:
                            problems.append("%s:%s mentions %s" % (tag, path, needle))
        self.assertEqual(problems, [])

    def test_the_frozen_check_reads_latest_and_nothing_else(self):
        script = ScriptedGitHub([release("v1.1.1")])
        run_old(script, "1.1.0", WIN_X64)
        self.assertEqual([c["url"] for c in script.calls], [LATEST_URL])


# ----------------------------------------------------------------- the headline: v1.1.0 -> v1.1.1
class OfferTests(Both):
    def test_v1_1_0_is_offered_v1_1_1_with_its_own_download_on_both_platforms(self):
        for target, expected in ((WIN_X64, WIN), (MAC_ARM, MAC)):
            results = self.run_all([release("v1.1.0"), release("v1.1.1")], installed="1.1.0", target=target)

            def check(result, expected=expected):
                self.assertEqual(result["state"], "update_available")
                self.assertEqual(result["latest_version"], "1.1.1")
                self.assertEqual(asset_name(result), expected)
                self.assertIsNone(result.get("package_message"))
            with self.subTest(target=target[0]):
                self.each(results, check)

    def test_the_download_is_chosen_by_name_in_any_order_and_among_unrelated_files(self):
        names = [WIN, MAC, SUMS]
        for order in (names, list(reversed(names)), [SUMS, MAC, WIN]):
            for target, expected in ((WIN_X64, WIN), (MAC_ARM, MAC)):
                results = self.run_all([release("v1.1.1", order)], target=target)
                self.each(results, lambda r, e=expected: self.assertEqual(asset_name(r), e))

    def test_the_other_platforms_archive_is_never_offered(self):
        only_windows = self.run_all([release("v1.1.1", [WIN, SUMS])], target=MAC_ARM)
        only_mac = self.run_all([release("v1.1.1", [MAC, SUMS])], target=WIN_X64)
        for results in (only_windows, only_mac):
            self.each(results, lambda r: (self.assertEqual(r["state"], "update_available"),
                                          self.assertIsNone(r["asset_url"])))

    def test_unrelated_and_look_alike_assets_are_ignored(self):
        junk = ["notes.md", SUMS, "OmniSuite-v1.1.1-windows-x86_64.zip", "LumaSuite-v1.1.1-rc1-windows-x86_64.zip",
                "LumaSuite-v1.1.1-diag3-windows-x86_64.zip", "LumaSuite-v1.1.0-windows-x86_64.zip",
                "LumaSuite-v1.1.2-windows-x86_64.zip", "LumaSuite-v1.1.1-windows-x86_64.zip.sig",
                "LumaSuite-v1.1.1-windows-x86_64.zip.asc", "LumaSuite-v1.1.1-windows.zip",
                "LumaSuite-v1.1.1-windows-arm64.zip", "LumaSuite-v1.1.1-macos-x86_64.zip",
                "LumaSuite-v1.1.1-macos-universal.zip", "LumaSuite-v1.1.1-linux-x86_64.tar.gz",
                "LumaSuite-v1.1.1-weekend-rc4-windows-x86_64.exe", "xLumaSuite-v1.1.1-windows-x86_64.zip",
                "LumaSuite-v1.1.1-windows-x86_64.msi"]
        for target in (WIN_X64, MAC_ARM):
            results = self.run_all([release("v1.1.1", junk)], target=target)
            self.each(results, lambda r: self.assertIsNone(r["asset_url"]))
        # and the real archive among them is the one chosen
        results = self.run_all([release("v1.1.1", junk + [WIN])], target=WIN_X64)
        self.each(results, lambda r: self.assertEqual(asset_name(r), WIN))

    def test_a_download_address_off_this_repository_or_off_https_is_not_handed_to_the_browser(self):
        for bad in ("http://github.com/%s/releases/download/v1.1.1/%s" % (REPO, WIN),
                    "https://evil.example/%s" % WIN,
                    "https://github.com/someone-else/LumaSuite/releases/download/v1.1.1/%s" % WIN,
                    "https://github.com.evil.example/%s/x" % REPO):
            item = release("v1.1.1")
            item["assets"] = [{"name": WIN, "browser_download_url": bad}]
            results = self.run_all([item])
            self.each(results, lambda r: self.assertIsNone(r["asset_url"]))

    def test_the_machine_without_a_matching_archive_is_told_the_truth_not_up_to_date(self):
        results = self.run_all([release("v1.1.1", [WIN, SUMS])], target=MAC_ARM)
        new = results["NEW"]
        self.assertEqual(new["state"], "update_available")
        self.assertIsNone(new["asset_url"])
        self.assertEqual(new["package_message"], lumaserver.UPDATE_NO_PACKAGE_IN_RELEASE)
        self.assertIn("no download for this computer", new["package_message"])


# ----------------------------------------------------------------- current, newer, downgrade
class CurrentTests(Both):
    def test_the_newest_release_is_current_on_every_platform(self):
        for target in (WIN_X64, MAC_ARM, WIN_ARM, MAC_INTEL, LINUX_X64):
            results = self.run_all([release("v1.1.1")], installed="1.1.1", target=target)
            self.each(results, lambda r: (self.assertEqual(r["state"], "current"),
                                          self.assertIsNone(r.get("package_message"))))

    def test_a_newer_local_build_is_never_downgraded(self):
        for installed in ("1.1.2", "1.2.0", "2.0.0", "1.1.10"):
            results = self.run_all([release("v1.1.1")], installed=installed)
            self.each(results, lambda r: self.assertEqual(r["state"], "current"))

    def test_numeric_not_textual_comparison(self):
        results = self.run_all([release("v1.1.10", [WIN.replace("1.1.1", "1.1.10")])], installed="1.1.9")
        self.each(results, lambda r: (self.assertEqual(r["state"], "update_available"),
                                      self.assertEqual(r["latest_version"], "1.1.10")))
        results = self.run_all([release("v1.1.9", [WIN.replace("1.1.1", "1.1.9")])], installed="1.1.10")
        self.each(results, lambda r: self.assertEqual(r["state"], "current"))
        results = self.run_all([release("v1.10.0", [WIN.replace("1.1.1", "1.10.0")])], installed="1.9.0")
        self.each(results, lambda r: self.assertEqual(r["state"], "update_available"))

    def test_the_installed_versions_spelling_does_not_change_the_answer(self):
        answers = set()
        for installed in ("1.1.0", "v1.1.0", " 1.1.0 "):
            results = self.run_all([release("v1.1.1")], installed=installed)
            for name, result in results.items():
                answers.add((name, result["state"], asset_name(result)))
        self.assertEqual(answers, {("NEW", "update_available", WIN), ("V1.1.0", "update_available", WIN)})
        # An upper-case V on the installed version: the new check reads it; the frozen v1.1.0 cannot and says
        # so (unable_to_check), never an update. The application's own version has no prefix, so this is
        # only a record of the difference.
        results = self.run_all([release("v1.1.1")], installed="V1.1.0")
        self.assertEqual(results["NEW"]["state"], "update_available")
        self.assertEqual(results["V1.1.0"]["state"], "unable_to_check")
        # The release TAG is another matter: only a lower-case v tag is this product's, in both.
        results = self.run_all([release("V1.1.1")], installed="1.1.0")
        self.each(results, lambda r: self.assertEqual(r["state"], "unable_to_check"))

    def test_an_installed_version_that_cannot_be_parsed_is_never_an_update(self):
        for installed in ("", "unknown", "dev"):
            results = self.run_all([release("v1.1.1")], installed=installed)
            self.each(results, lambda r: self.assertEqual(r["state"], "unable_to_check"))


# ----------------------------------------------------------------- what is not a production release
class IdentityTests(Both):
    BAD_TAGS = ("v1.1.x", "1.1.1", "V1.1.1", "v1.1", "v1.1.1-rc1", "v1.1.1-diag1", "v1.1.1\n", "v1.1.1.1", "release-9",
                "v" + "9" * 30 + ".0.0.junk", "", "latest", "v1.1.1 ")

    def test_a_malformed_tag_is_never_offered_and_never_called_current(self):
        for tag in self.BAD_TAGS:
            results = self.run_all([release(tag)], installed="1.1.0")
            for name, result in results.items():
                with self.subTest(implementation=name, tag=tag):
                    self.assertEqual(result["state"], "unable_to_check")
                    self.assertNotIn("asset_url", result)

    def test_a_malformed_tag_is_not_current_even_when_it_looks_like_the_installed_version(self):
        for tag in ("V1.1.1", "v1.1.1-rc1", "1.1.1"):
            results = self.run_all([release(tag)], installed="1.1.1")
            self.each(results, lambda r: self.assertNotEqual(r["state"], "current"))

    def test_a_draft_or_a_prerelease_is_ignored_by_the_feed_and_refused_if_it_arrives_anyway(self):
        # GitHub's /releases/latest never serves one: the older production release is what is served.
        feed = [release("v1.1.0"), release("v1.1.1", draft=True), release("v1.2.0", prerelease=True)]
        results = self.run_all(feed, installed="1.0.8")
        self.each(results, lambda r: (self.assertEqual(r["state"], "update_available"),
                                      self.assertEqual(r["latest_version"], "1.1.0")))
        # Prerelease policy: a channel, never an identity. Delivered directly, it is refused by both.
        for flag in ("draft", "prerelease"):
            results = self.run_all(payload=release("v1.1.1", **{flag: True}), installed="1.1.0")
            self.each(results, lambda r: (self.assertEqual(r["state"], "unable_to_check"),
                                          self.assertEqual(r["detail"], "not_a_production_release")))

    def test_only_a_draft_means_nothing_is_published_and_that_is_not_up_to_date(self):
        results = self.run_all([release("v1.1.1", draft=True)], installed="1.1.0")
        self.each(results, lambda r: (self.assertEqual(r["state"], "unable_to_check"),
                                      self.assertEqual(r["detail"], "http_404")))


# ----------------------------------------------------------------- failure is never "up to date"
class FailureTests(Both):
    def test_a_network_failure_is_unable_to_check(self):
        import requests
        for error in (requests.ConnectionError("down"), requests.Timeout("slow"), OSError("unreachable"),
                      RuntimeError("anything"), requests.exceptions.SSLError("tls")):
            results = self.run_all(raises=error)
            for name, result in results.items():
                with self.subTest(implementation=name, error=type(error).__name__):
                    self.assertEqual(result["state"], "unable_to_check")
                    self.assertNotIn("asset_url", result)

    def test_every_http_refusal_is_unable_to_check(self):
        for status in (301, 400, 403, 404, 429, 500, 502, 503):
            results = self.run_all([release("v1.1.1")], status=status)
            self.each(results, lambda r, s=status: (self.assertEqual(r["state"], "unable_to_check"),
                                                    self.assertEqual(r["detail"], "http_%d" % s)))

    def test_an_invalid_answer_is_unable_to_check_and_never_a_crash(self):
        for payload in (_NotJson, [], [release("v1.1.1")], "text", 5, None, {}, {"tag_name": None}, {"tag_name": 7},
                        {"tag_name": ["v1.1.1"]}, {"message": "rate limited"}):
            results = self.run_all(payload=payload)
            for name, result in results.items():
                with self.subTest(implementation=name, payload=repr(payload)[:40]):
                    self.assertEqual(result["state"], "unable_to_check")

    def test_a_hostile_asset_list_is_survived(self):
        # The new check survives every shape. The frozen v1.1.0 raises on a feed whose assets are not a list of
        # objects (a KNOWN DEFECT of the published-candidate code, which cannot be changed; GitHub does not
        # send such a feed, so it matters only to a tampered one). It is recorded here, not hidden.
        survived_by_old = []
        for assets in (None, "x", 5, {}, {"a": 1}, ["x"], [5], [None], [{}], [{"name": None}],
                       [{"name": 5, "browser_download_url": 5}], [{"name": WIN}],
                       [{"browser_download_url": DL + WIN}]):
            item = release("v1.1.1")
            item["assets"] = assets
            with self.subTest(implementation="NEW", assets=repr(assets)[:40]):
                result = run_new(ScriptedGitHub([item]), "1.1.0", WIN_X64)
                self.assertIn(result["state"], ("update_available", "unable_to_check"))
                self.assertIsNone(result.get("asset_url"))
            try:
                result = run_old(ScriptedGitHub([item]), "1.1.0", WIN_X64)
            except (AttributeError, TypeError):
                continue
            survived_by_old.append(repr(assets))
            self.assertIsNone(result.get("asset_url"))
        self.assertEqual(survived_by_old, ["None", "{}", "[None]", "[{}]", "[{'name': None}]",
                                           "[{'name': 5, 'browser_download_url': 5}]", "[{'name': WIN}]".replace("WIN", repr(WIN)),
                                           "[{'browser_download_url': %r}]" % (DL + WIN)])

    def test_the_endpoint_never_reports_a_failed_check_as_current(self):
        client = lumaserver.APP.test_request_context("/api/update_check?force=1")
        saved = dict(lumaserver._update_check_cache)
        try:
            with mock.patch.object(lumaserver.requests, "get", ScriptedGitHub(raises=OSError("down")).get), client:
                body = lumaserver.api_update_check().get_json()
        finally:
            lumaserver._update_check_cache.clear()
            lumaserver._update_check_cache.update(saved)
        self.assertEqual(body["state"], "unable_to_check")
        self.assertTrue(body["ok"])


# ----------------------------------------------------------------- the platform matrix, in the updater
class PlatformTests(Both):
    UNSUPPORTED = (WIN_ARM, MAC_INTEL, LINUX_X64, LINUX_ARM, UNKNOWN)

    def test_an_unsupported_computer_is_told_there_is_no_supported_package(self):
        for target in self.UNSUPPORTED:
            # even from a release that still carries archives for retired platforms
            names = [WIN, MAC, SUMS, "LumaSuite-v1.1.1-macos-x86_64.zip", "LumaSuite-v1.1.1-linux-x86_64.tar.gz"]
            result = run_new(ScriptedGitHub([release("v1.1.1", names)]), "1.1.0", target)
            with self.subTest(target=target[0]):
                self.assertEqual(result["state"], "update_available")
                self.assertIsNone(result["asset_url"])
                self.assertEqual(result["package_message"], lumaserver.UPDATE_NO_PACKAGE_UNSUPPORTED)
                self.assertIn("no supported update package", result["package_message"])
                self.assertNotEqual(result["state"], "current")

    def test_a_supported_computer_is_never_given_the_unsupported_sentence(self):
        for target in (WIN_X64, MAC_ARM):
            result = run_new(ScriptedGitHub([release("v1.1.1")]), "1.1.0", target)
            self.assertIsNone(result["package_message"], target[0])

    def test_the_installed_v1_1_0_on_a_retired_platform_gets_no_download_from_a_v1_1_1_release(self):
        # V1.1.0 cannot be changed. On macOS Intel or Linux it finds none of its own archives in a release that
        # carries only the two distributed ones, so it can offer the release page and no download.
        for target in (MAC_INTEL, LINUX_X64):
            result = run_old(ScriptedGitHub([release("v1.1.1")]), "1.1.0", target)
            with self.subTest(target=target[0]):
                self.assertEqual(result["state"], "update_available")
                self.assertIsNone(result["asset_url"])
                self.assertTrue(result["html_url"])

    def test_the_two_tables_agree(self):
        self.assertEqual(lumaserver._platform_asset_suffixes("win32", "AMD64"),
                         ("-windows-x86_64.zip", "-windows-x86_64.exe"))
        self.assertEqual(lumaserver._platform_asset_suffixes("darwin", "arm64"), ("-macos-arm64.zip",))
        for plat, machine in (("win32", "ARM64"), ("darwin", "x86_64"), ("linux", "x86_64"), ("linux", "aarch64"),
                              ("darwin", "ppc"), ("freebsd14", "amd64")):
            self.assertEqual(lumaserver._platform_asset_suffixes(plat, machine), (), (plat, machine))


# ----------------------------------------------------------------- publication order and the consequences
class PublicationOrderTests(Both):
    """The installed V1.1.0 reads /releases/latest: the most recently CREATED release, not the highest version."""

    def test_latest_is_the_newest_created_not_the_highest_version(self):
        script = ScriptedGitHub([release("v1.1.1"), release("v1.1.0")])      # v1.1.0 was created last
        self.assertEqual(script.latest()["tag_name"], "v1.1.0")

    def test_publishing_an_older_release_after_a_newer_hides_the_newer_from_installed_applications(self):
        feed = [release("v1.1.1"), release("v1.1.0", [WIN.replace("1.1.1", "1.1.0")])]
        results = self.run_all(feed, installed="1.1.0")
        # Both implementations read the same answer, so both say "current": the new release is not offered.
        self.each(results, lambda r: self.assertEqual(r["state"], "current"))
        # Published in version order, the same feed offers it.
        results = self.run_all(list(reversed(feed)), installed="1.1.0")
        self.each(results, lambda r: self.assertEqual(r["state"], "update_available"))

    def test_the_draft_stays_invisible_until_a_person_publishes_it(self):
        feed = [release("v1.1.0"), release("v1.1.1", draft=True)]
        results = self.run_all(feed, installed="1.1.0")
        self.each(results, lambda r: self.assertEqual(r["state"], "current"))
        feed[1]["draft"] = False
        results = self.run_all(feed, installed="1.1.0")
        self.each(results, lambda r: self.assertEqual(r["state"], "update_available"))

    def test_the_release_must_carry_this_products_two_archives_and_nothing_a_v1_1_0_would_pick_wrongly(self):
        # V1.1.0 matches by exact name, so a stray archive of another platform cannot be picked for Windows.
        stray = [WIN, MAC, SUMS, "LumaSuite-v1.1.1-macos-x86_64.zip", "LumaSuite-v1.1.1-linux-x86_64.tar.gz"]
        results = self.run_all([release("v1.1.1", stray)], target=WIN_X64)
        self.each(results, lambda r: self.assertEqual(asset_name(r), WIN))


class RequestContractTests(unittest.TestCase):
    def test_the_new_check_asks_for_latest_once_with_a_bound_and_names_itself(self):
        script = ScriptedGitHub([release("v1.1.1")])
        run_new(script, "1.1.0", WIN_X64)
        self.assertEqual(len(script.calls), 1)
        call = script.calls[0]
        self.assertEqual(call["url"], LATEST_URL)
        self.assertEqual(call["timeout"], lumaserver._UPDATE_CHECK_TIMEOUT_SEC)
        self.assertLessEqual(call["timeout"], 10)
        self.assertTrue(call["headers"]["User-Agent"].startswith("LumaSuite/"))

    def test_the_repository_is_the_one_installed_v1_1_0_reads(self):
        self.assertEqual(lumaserver._UPDATE_CHECK_REPO, REPO)
        self.assertEqual(_load_frozen()._UPDATE_CHECK_REPO, REPO)


if __name__ == "__main__":
    unittest.main()
