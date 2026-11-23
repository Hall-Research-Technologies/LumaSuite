# FROZEN, DO NOT EDIT. The application update check exactly as it stands in the tag v1.1.0
# (commit 72000531ca22fbba78f10a292b4203d26b9100a4, lumaserver.py, the block that begins at the line
# "APPLICATION UPDATE CHECK" and ends before the /api/update_check route), extracted with `git show`.
# tests/test_updater_proof.py pins the SHA-256 of everything below the marker line, so it cannot be edited to agree
# with current behaviour. It is loaded as a module and driven against scripted GitHub feeds; the harness replaces
# `requests`, `sys`, `platform` and `__version__` per scenario. Nothing here runs on import.
import platform  # noqa: F401  (replaced by the harness)
import re
import sys  # noqa: F401  (replaced by the harness)
import threading
import time  # noqa: F401
import urllib.parse

requests = None
__version__ = None

# ===== VERBATIM FROM v1.1.0 BELOW THIS LINE =====
# -------- APPLICATION UPDATE CHECK (GitHub Releases) --------
# Family spec Part 2.2. This checks whether a newer LumaSuite *application*
# build has been published -- unrelated to device/firmware version checks
# (Part 3/13). Checked only on demand (Settings opened / "Check Now"
# clicked), never on a timer, so it never becomes background polling.
_UPDATE_CHECK_REPO = "Hall-Research-Technologies/LumaSuite"
_UPDATE_CHECK_TIMEOUT_SEC = 4
_UPDATE_CHECK_CACHE_TTL_SEC = 600
_update_check_cache = {"ts": 0.0, "result": None}
_update_check_lock = threading.Lock()

def _parse_version_tuple(text):
    """Best-effort 'vX.Y.Z' -> (X, Y, Z). Returns None if not parseable."""
    if not text:
        return None
    m = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text).strip())
    if not m:
        return None
    return tuple(int(g) if g else 0 for g in m.groups())

def _compare_versions(a, b):
    """Return -1/0/1 for a</==/> b, treating unparseable input as unknown (None)."""
    if a is None or b is None:
        return None
    return (a > b) - (a < b)

def _is_official_release_url(url):
    """Reject anything but an https URL on github.com (under this repo) or
    objects.githubusercontent.com. GitHub's API response is otherwise handed
    straight to the browser as a link href, which would make a malformed or
    tampered response an open redirect."""
    if not url:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.hostname == "objects.githubusercontent.com":
        return True
    if parsed.hostname == "github.com":
        return parsed.path.startswith("/%s/" % _UPDATE_CHECK_REPO)
    return False

def _platform_asset_suffixes(platform_name=None, machine=None):
    """The tails of the release assets built for this computer, or () when that cannot be said.

    The release workflow names its archives LumaSuite-<tag>-windows-x86_64.zip,
    LumaSuite-<tag>-macos-x86_64.zip, -macos-arm64.zip and LumaSuite-<tag>-linux-x86_64.tar.gz: the
    operating system and the architecture, both stated. A bare executable named for Windows x86-64
    (-windows-x86_64.exe, how a release candidate is handed out) is the same artifact. A computer that
    matches none of them is not offered a download at all: the wrong build is worse than the releases page.
    """
    platform_name = platform_name or sys.platform
    machine = (machine or platform.machine() or "").lower()
    x86 = machine in ("x86_64", "amd64")
    if platform_name.startswith("win"):
        return ("-windows-x86_64.zip", "-windows-x86_64.exe") if x86 else ()
    if platform_name == "darwin":
        if machine in ("arm64", "aarch64"):
            return ("-macos-arm64.zip",)
        return ("-macos-x86_64.zip",) if x86 else ()
    if platform_name.startswith("linux"):
        return ("-linux-x86_64.tar.gz",) if x86 else ()
    return ()

# A production release is exactly v<major>.<minor>.<patch>. v1.1.0-rc4, V1.1.0, v1.1.0\n and
# v1.1 are not release tags; \Z, never $, so a trailing newline does not qualify.
_PRODUCTION_TAG_RE = re.compile(r"v\d+\.\d+\.\d+\Z")

def _is_production_release(payload):
    """True only for a published, non-prerelease release whose tag is exactly v<M>.<m>.<p>.

    The update check reads GitHub's "latest" release, which is whatever the owner last published as
    non-draft and non-prerelease. That is a channel, not an identity, so the tag is checked as well:
    a candidate that was published by mistake is reported as unable to check, never offered."""
    if not isinstance(payload, dict):
        return False
    if payload.get("draft") or payload.get("prerelease"):
        return False
    return bool(_PRODUCTION_TAG_RE.match(str(payload.get("tag_name") or "")))

def _pick_platform_asset(assets, platform_name=None, machine=None, tag=None):
    """The download built for this computer. With `tag` given (the update check always gives it), the
    asset name must be exactly LumaSuite-<tag><platform suffix>, so a candidate build's
    executable, which carries extra words in its name, can never match a production release."""
    suffixes = _platform_asset_suffixes(platform_name, machine)
    if not suffixes:
        return None
    for suffix in suffixes:           # in order of preference: the archive before a bare executable
        for asset in assets:
            name = str((asset or {}).get("name") or "").lower()
            if tag is not None:
                if name != ("lumasuite-%s%s" % (tag, suffix)).lower():
                    continue
            elif not name.startswith("lumasuite-"):
                continue
            if name.endswith(suffix) and asset.get("browser_download_url"):
                return asset["browser_download_url"]
    return None

def _run_update_check():
    installed_tuple = _parse_version_tuple(__version__)
    try:
        resp = requests.get(
            "https://api.github.com/repos/%s/releases/latest" % _UPDATE_CHECK_REPO,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "LumaSuite/%s" % __version__},
            timeout=_UPDATE_CHECK_TIMEOUT_SEC,
        )
    except Exception as exc:
        return {"state": "unable_to_check", "detail": type(exc).__name__}

    if resp.status_code != 200:
        # A 404 (no releases yet) is not an application error and must not
        # be shown as "current" -- neither answer is provable here.
        return {"state": "unable_to_check", "detail": "http_%s" % resp.status_code}

    try:
        payload = resp.json()
    except Exception:
        return {"state": "unable_to_check", "detail": "malformed_response"}

    if not _is_production_release(payload):
        # Not a production release (draft, prerelease or a tag that is not exactly vX.Y.Z). Neither
        # "current" nor "update available" is provable from it.
        return {"state": "unable_to_check", "detail": "not_a_production_release",
                "latest_version": str((payload or {}).get("tag_name") or "") if isinstance(payload, dict) else ""}

    latest_tag = payload.get("tag_name") or payload.get("name") or ""
    latest_tuple = _parse_version_tuple(latest_tag)
    cmp_result = _compare_versions(latest_tuple, installed_tuple)
    if cmp_result is None:
        return {"state": "unable_to_check", "detail": "unparseable_version", "latest_version": latest_tag}

    asset_url = _pick_platform_asset(payload.get("assets") or [], tag=latest_tag)

    html_url = payload.get("html_url")
    if not _is_official_release_url(html_url):
        html_url = None
    if not _is_official_release_url(asset_url):
        asset_url = None

    state = "update_available" if cmp_result > 0 else "current"
    return {
        "state": state,
        "latest_version": latest_tag.lstrip("vV") or latest_tag,
        "installed_version": __version__,
        "html_url": html_url,
        "asset_url": asset_url,
    }

