#!/usr/bin/env python3
"""Inspect a built LumaSuite release archive before it can be offered to anyone.

    inspect_release.py ARCHIVE [ARCHIVE ...] --version v1.1.1 [--diagnostic]

It fails closed: any problem exits non-zero with a sentence per problem. It reads archives only;
it never extracts, imports or runs anything in them. It checks what is in the package, not what
the build intended to put there:

  * the archive name is this product's, for exactly this version
    (LumaSuite-v<version>-<platform>), with no pre-release or candidate label;
  * what must travel beside the application is present: the executable, LICENSE (carrying
    section 11A), THIRD_PARTY_NOTICES.md, REBUILD.md, licenses/ and the unmodified pystray
    source, wheel and tarball (required LGPL material, byte-identical to the pinned upstream
    files -- not leakage);
  * nothing that must never ship is present: tests, tools, the application's own source, firmware
    images, key or certificate material, configuration, user, cache and state files, backups,
    logs, version-control data, temporary files, diagnostics and reports, a second archive;
  * text members carry no credential, private-key block, lab-range address, private product name
    or build-machine path, and the executable carries no build-machine path.

--diagnostic is for a workflow run that can never publish. It accepts ONE additional kind of
name, LumaSuite-v<release>-diag<N>-<platform> or LumaSuite-ci-<label>-<platform>, so a rehearsal
can run the same content checks as a release. It relaxes the name and nothing else; a release
tag, an rc label or a stray suffix is judged exactly as before whether or not the flag is given.

Limit, stated plainly: the Windows executable is a compressed PyInstaller archive, so the
build-machine-path scan of its bytes sees the bootloader and any uncompressed data, not the
compressed modules. `release_guard.py inspect-exe` reads the table of contents for those.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_guard                                   # noqa: E402

PRODUCT = release_guard.PRODUCT
# \Z, never $: a tag with a trailing newline is not a release tag, and nothing here repairs one.
_TAG = re.compile(r"v\d+\.\d+\.\d+\Z")
_DIAG_VERSION = re.compile(r"v\d+\.\d+\.\d+-diag[0-9]{1,3}\Z")
_CI_LABEL = re.compile(r"ci-[a-z0-9][a-z0-9-]{0,40}\Z")
PLATFORM_TAILS = ("windows-x86_64.zip", "macos-x86_64.zip", "macos-arm64.zip", "linux-x86_64.tar.gz")

# The corresponding source of the LGPL component ships unmodified, so its own tests/ and tools/
# folders are part of it and are not a leak. Its bytes are pinned elsewhere; secrets are still scanned.
_UPSTREAM = re.compile(r"^(.*/)?third_party/pystray-0\.19\.5/")
_UPSTREAM_ARCHIVES = {release_guard.PYSTRAY_DIR + "/pystray-0.19.5-github-source.tar.gz",
                      release_guard.PYSTRAY_DIR + "/pystray-0.19.5-py2.py3-none-any.whl"}
# Two exact allowances produced by the build tool itself rather than chosen by us: PyInstaller's
# frozen standard library and certifi's public CA bundle (a certificate list, not key material).
_BUILD_TOOL_OK = re.compile(r"^(.*/)?base_library\.zip$|^(.*/)?certifi/cacert\.pem$")
_LICENCE_TREE = re.compile(r"^licenses/")

_APP_SOURCE = re.compile(r"(^|/)(lumaserver|luma_[a-z0-9_]+|producer2_routes|app_launcher|server|run_tests|"
                         r"build_executable)\.py$")
_STATE_NAMES = ("device_aliases", "display_groups", "firmware_status", "producer2_state", "producer_config",
                "producer_state", "remote_access", "security_audit", "units_cache", "users", "video_walls",
                "luma_users", "scan_results")
_FORBIDDEN = [
    (r"(^|/)tests?/", "a test folder"),
    (r"(^|/)tools/", "a tools folder"),
    (r"(^|/)\.git(/|$|ignore$|attributes$|modules$)", "version-control data"),
    (r"(^|/)(\.github|\.vscode|\.idea)/", "development-environment data"),
    (r"(^|/)__pycache__/|\.py[co]$", "compiled or cached Python"),
    (r"(^|/)firmware/", "a firmware folder"),
    (r"\.(bin|img|fw|hex|rom|ota|bix)$", "a firmware or binary image"),
    (r"\.(key|pfx|p12|crt|cer|csr|jks|keystore)$|(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$|(^|/)\.env$",
     "key, certificate or environment material"),
    (r"\.pem$", "certificate or key material"),
    (r"(^|/)(" + "|".join(_STATE_NAMES) + r")(\.[A-Za-z0-9_-]+)?\.(json|jsonl|db|sqlite|dat)$",
     "a configuration, user, cache or state file"),
    (r"(^|/)ui/(localdata|archive)/", "runtime data or superseded pages"),
    (r"\.(lumabak|bak|orig|rej|old|tmp|temp|swp|swo|dmp)$|~$|(^|/)\.DS_Store$|(^|/)Thumbs\.db$|(^|/)desktop\.ini$",
     "a backup or temporary file"),
    (r"\.(log|jsonl|etl|trace|har)$", "a log or trace"),
    (r"(^|/)(AGENTS|PROJECT|DEVELOPMENT|CODE_REVIEW_FIXES|BUILD_STANDALONE|CHANGELOG)\.md$|CHECKLIST",
     "a development document"),
    (r"(soak|mutation|qualification|diagnostic|diagnostics|bench|evidence|overnight|spotcheck|report)",
     "diagnostic or qualification material"),
    (r"\.(zip|tar|gz|tgz|7z|rar|xz|bz2|whl|dmg|pkg|msi|exe)$", "a nested archive or a second executable"),
]
_FORBIDDEN = [(re.compile(p, re.IGNORECASE), why) for p, why in _FORBIDDEN]

_SECRET = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
                     rb"|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}")
# A credential assigned to a named field. Text members only: an executable's embedded manifest
# carries publicKeyToken="..." and that is a public identifier, not a secret.
_SECRET_ASSIGNED = re.compile(rb"(?<![A-Za-z])(?i:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
                              rb"[\"']?\s*[:=]\s*[\"'][^\"'\s<>{}$%]{8,}[\"']")
_BENCH = re.compile(rb"\b192\.168\.(100|150|200|250)\.\d{1,3}\b")
_PRIVATE_NAME = re.compile(rb"OmniSuite[\s_-]*(Plus|Pro|Enterprise|Premium)|LumaSuite[\s_-]*(Plus|Pro|Enterprise|Premium)"
                           rb"|(private|internal)[\s_-]+(branch|repository|repo|product)|omnisuite-unified"
                           rb"|(?i:edition[\s_-]*(tier|gate))", re.IGNORECASE)
_BUILD_PATH = re.compile(rb"[A-Za-z]:\\+(softwareDEV|Users\\+Wylie)|overnight_rc|work24|/Users/[A-Za-z]+/softwareDEV"
                         rb"|\\AppData\\Local\\Temp", re.IGNORECASE)
_TEXT_SUFFIX = {".md", ".txt", ".html", ".css", ".js", ".json", ".rst", ".cfg", ".yml", ".yaml", ".toml", ".terms",
                ".py", ".lgpl", ""}
_TEXT_NAMES = {"LICENSE", "COPYING", "NOTICE", "VERSION", "MANIFEST.json"}


def _norm(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name


def members(path: Path):
    """(name, bytes) for every file member, read in memory; nothing is extracted."""
    lower = path.name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield _norm(info.filename), archive.read(info)
    elif lower.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            for info in archive:
                if info.isfile():
                    yield _norm(info.name), archive.extractfile(info).read()
    else:
        raise ValueError("%s is not a release archive" % path.name)


def _platform_of(name: str):
    for tail in PLATFORM_TAILS:
        if name.endswith("-" + tail):
            return tail
    return None


def name_problems(name: str, version: str, diagnostic: bool) -> list:
    """The archive's own name must say it is this product's release for exactly this version."""
    version = version if isinstance(version, str) else ""
    tail = _platform_of(name)
    if not _TAG.fullmatch(version) and not (diagnostic and _DIAG_VERSION.fullmatch(version)):
        return ["%s: %r is not a release version (v<major>.<minor>.<patch>) and cannot be inspected "
                "against" % (name, version)]
    if tail is None:
        return ["%s is not named for a platform build (%s)" % (name, ", ".join(PLATFORM_TAILS))]
    if name == "%s-%s-%s" % (PRODUCT, version, tail):
        return []
    if diagnostic:
        head = name[len(PRODUCT) + 1:-len(tail) - 1] if name.startswith(PRODUCT + "-") else ""
        if _CI_LABEL.fullmatch(head) or _DIAG_VERSION.fullmatch(head):
            return []
    return ["%s is not this product's release archive for exactly %s (a pre-release or build label "
            "is not an identity)" % (name, version)]


def problems_for(path: Path, version: str, diagnostic: bool = False) -> list:
    name = path.name
    found = name_problems(name, version, diagnostic)
    try:
        listing = list(members(path))
    except Exception as error:                         # a corrupt archive is a finding, not a crash
        return found + ["%s cannot be read: %s" % (name, error)]
    if not listing:
        return found + ["%s is empty" % name]
    data_of = dict(listing)
    names = list(data_of)
    tail = _platform_of(name) or ""

    # ---- required members
    for required in release_guard.BESIDE_FILES:
        if required not in data_of:
            found.append("%s has no %s, which must travel with the application (licence, notices, "
                         "rebuild steps or the LGPL component)" % (name, required))
    for member, digest in release_guard.PINNED_SHA256.items():
        if member in data_of and hashlib.sha256(data_of[member]).hexdigest() != digest:
            found.append("%s: %s is not the pinned upstream file" % (name, member))
    licence = data_of.get("LICENSE", b"").replace(b"\r\n", b"\n")
    if "LICENSE" in data_of and release_guard.SECTION_11A.encode() not in licence:
        found.append("%s: LICENSE does not carry section 11A" % name)
    if tail.startswith("windows") and "LumaSuite.exe" not in data_of:
        found.append("%s has no LumaSuite.exe at its top level" % name)
    if tail.startswith("macos") and not any(n.startswith("LumaSuite.app/Contents/MacOS/") for n in names):
        found.append("%s has no LumaSuite.app application bundle" % name)
    if tail.startswith("linux") and not any(n == "LumaSuite" or n.endswith("/LumaSuite") for n in names):
        found.append("%s has no LumaSuite executable" % name)
    if tail.startswith("windows"):
        tops = {n.split("/")[0] for n in names}
        if tops != release_guard.WINDOWS_TOP:
            found.append("%s: the top level must be exactly %s, found %s"
                         % (name, sorted(release_guard.WINDOWS_TOP), sorted(tops)))

    # ---- forbidden members and content
    for member, data in listing:
        upstream = bool(_UPSTREAM.match(member))
        if not upstream and not _BUILD_TOOL_OK.match(member) and member not in _UPSTREAM_ARCHIVES:
            if _APP_SOURCE.search(member):
                found.append("%s: %s is the application's own source and must not ship" % (name, member))
            elif tail.startswith("windows") and member.lower().endswith(".py"):
                found.append("%s: %s is Python source and must not ship" % (name, member))
            licence_path = bool(_LICENCE_TREE.match(member))
            # A licence text is named "LICENSE" or "COPYING"; its folder name is a library's, not ours.
            checked = member if not licence_path else ""
            for pattern, why in _FORBIDDEN:
                if checked and pattern.search(checked):
                    if why == "a nested archive or a second executable" and member == "LumaSuite.exe":
                        continue
                    if member.startswith("LumaSuite.app/") and pattern.pattern.endswith("(zip|tar|gz|tgz|7z|rar|xz|bz2|whl|dmg|pkg|msi|exe)$"):
                        continue
                    found.append("%s: %s must not ship (%s)" % (name, member, why))
                    break
            if licence_path and re.search(r"\.(pem|key|pfx|p12|crt|cer|zip|tar|gz|exe|bin|py|pyc|log|bak)$", member,
                                          re.IGNORECASE):
                found.append("%s: %s must not ship (only licence texts belong in licenses/)" % (name, member))
        suffix = Path(member).suffix.lower()
        is_text = suffix in _TEXT_SUFFIX or Path(member).name in _TEXT_NAMES
        if member in _UPSTREAM_ARCHIVES or _BUILD_TOOL_OK.match(member) or not is_text:
            if member == "LumaSuite.exe" or (len(data) > 1_000_000 and not (
                    member in _UPSTREAM_ARCHIVES or _BUILD_TOOL_OK.match(member))):
                if _BUILD_PATH.search(data):
                    found.append("%s: %s contains a build-machine path" % (name, member))
                if _SECRET.search(data):
                    found.append("%s: %s contains a credential or private-key block" % (name, member))
            continue
        if _SECRET.search(data) or _SECRET_ASSIGNED.search(data):
            found.append("%s: %s contains a credential or private-key block" % (name, member))
        if upstream:
            continue
        if _PRIVATE_NAME.search(data):
            found.append("%s: %s names a private product, branch or edition" % (name, member))
        if _BENCH.search(data):
            found.append("%s: %s names a lab-range address" % (name, member))
        if _BUILD_PATH.search(data):
            found.append("%s: %s names a build-machine path" % (name, member))
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--version", required=True, help="the release tag, for example v1.1.1")
    parser.add_argument("--diagnostic", action="store_true",
                        help="accept the rehearsal names LumaSuite-v<release>-diag<N>-<platform> and "
                             "LumaSuite-ci-<label>-<platform>; nothing else is relaxed")
    args = parser.parse_args(argv)
    bad = []
    for path in args.archives:
        found = problems_for(path, args.version, diagnostic=args.diagnostic)
        print("%s: %s" % (path.name, "OK" if not found else "%d problem(s)" % len(found)))
        bad += found
    for line in bad:
        print("::error::" + line if "GITHUB_ACTIONS" in os.environ else "  " + line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
