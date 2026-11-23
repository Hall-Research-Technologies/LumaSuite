#!/usr/bin/env python3
"""Release safety guard. Fails closed: any doubt is a non-zero exit and nothing is published.

Subcommands
  identity   Decide what a workflow run is, before anything is built.
  inspect    Inspect the packaged release assets before a draft release is created.
  checksums  Write SHA256SUMS.txt from the final bytes in a directory.
  stage      Lay out a package: the application plus LICENSE, notices, REBUILD.md, licenses/, third_party/.

Untrusted text (a tag, a dispatched ref) arrives through the environment, never through a shell
command line. The tag must be exactly v<major>.<minor>.<patch> (\\Z, never $), must equal "v" plus the
application version in lumaserver.py, and must point at a commit reachable from origin/main.
A branch push or a manual dispatch is a diagnostic build: it can never publish.
"""
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import zipfile

TAG_RE = re.compile(r"v\d+\.\d+\.\d+\Z")
BAD_WORDS = re.compile(r"(rc\d*|weekend|manual|final-rc|test|dev|draft|beta|alpha|snapshot)", re.I)
PRODUCT = "LumaSuite"
EXPECTED_REPOSITORY = "Hall-Research-Technologies/LumaSuite"
# file names that never belong in a release package
FORBIDDEN_PARTS = ("luma_users", "users.json", "__pycache__", ".git/")


class GuardError(Exception):
    pass


# What travels beside the application in every release package (Windows x86-64, macOS Apple Silicon). The licence, the
# third-party notices, the rebuild steps and the unmodified pystray source must accompany the executable.
PYSTRAY_DIR = "third_party/pystray-0.19.5"
BESIDE_FILES = (
    "LICENSE", "THIRD_PARTY_NOTICES.md", "REBUILD.md",
    "licenses/MANIFEST.json", "licenses/pystray/COPYING", "licenses/pystray/COPYING.LGPL",
    PYSTRAY_DIR + "/PROVENANCE.md",
    PYSTRAY_DIR + "/pystray-0.19.5-github-source.tar.gz",
    PYSTRAY_DIR + "/pystray-0.19.5-py2.py3-none-any.whl",
    PYSTRAY_DIR + "/source/COPYING", PYSTRAY_DIR + "/source/COPYING.LGPL",
)
BESIDE_TREES = ("licenses", "third_party")
# The shipped pystray files are pinned to the published upstream bytes.
PINNED_SHA256 = {
    PYSTRAY_DIR + "/pystray-0.19.5-github-source.tar.gz": "2ab6bd15c14b400c9e8271a82c9de8adc0f1a0cacfc14080bdd4fb42bb88ec92",
    PYSTRAY_DIR + "/pystray-0.19.5-py2.py3-none-any.whl": "a0c2229d02cf87207297c22d86ffc57c86c227517b038c0d3c59df79295ac617",
}
SECTION_11A = "11A. COMPONENTS LICENSED UNDER THE GNU LESSER GENERAL PUBLIC LICENSE"
WINDOWS_TOP = {"LumaSuite.exe", "LICENSE", "THIRD_PARTY_NOTICES.md", "REBUILD.md", "licenses", "third_party"}


def stage(platform_name, dist_dir, out_dir, root="."):
    """Lay out one release package: the application files plus everything that must travel beside them."""
    import shutil
    if os.path.exists(out_dir):
        raise GuardError("staging folder already exists: %s" % out_dir)
    os.makedirs(out_dir)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "REBUILD.md"):
        shutil.copy2(os.path.join(root, name), os.path.join(out_dir, name))
    for tree in BESIDE_TREES:
        shutil.copytree(os.path.join(root, tree), os.path.join(out_dir, tree))
    if platform_name == "windows":
        shutil.copy2(os.path.join(dist_dir, "LumaSuite.exe"), os.path.join(out_dir, "LumaSuite.exe"))
    elif platform_name == "macos":
        shutil.copytree(os.path.join(dist_dir, "LumaSuite.app"), os.path.join(out_dir, "LumaSuite.app"), symlinks=True)
    else:
        raise GuardError("unknown platform %r" % platform_name)
    return sorted(os.listdir(out_dir))


def app_version(root="."):
    with open(os.path.join(root, "lumaserver.py"), encoding="utf-8") as handle:
        match = re.search(r'^__version__ = "([^"]+)"', handle.read(), re.M)
    if not match:
        raise GuardError("lumaserver.py declares no __version__")
    return match.group(1)


# SUPPORTED DISTRIBUTED PLATFORMS: Windows x86-64 and macOS Apple Silicon (arm64), and only those. No
# macOS Intel, no Linux, no universal binary. This is the one place the distributed set is stated for
# the release gate; the updater, the inspector, the workflow and the documentation are pinned to it by
# tests/test_platform_matrix.py.
DISTRIBUTED_TAILS = ("windows-x86_64.zip", "macos-arm64.zip")


def expected_assets(tag):
    return sorted("%s-%s-%s" % (PRODUCT, tag, tail) for tail in DISTRIBUTED_TAILS)


def decide_identity(event, ref_type, ref_name, repository, version, on_main):
    """Return a dict. Raises GuardError when a tag run is not this product's release."""
    if repository != EXPECTED_REPOSITORY:
        raise GuardError("repository %r is not %r" % (repository, EXPECTED_REPOSITORY))
    if ref_type != "tag":
        # a branch push or a dispatch: diagnostic only
        return {"publishable": False, "tag": "", "reason": "diagnostic build (%s %s) cannot publish" % (event, ref_type)}
    if not TAG_RE.match(ref_name or ""):
        raise GuardError("tag %r is not exactly v<major>.<minor>.<patch>" % (ref_name,))
    if ref_name != "v" + version:
        raise GuardError("tag %r does not match the application version %r" % (ref_name, version))
    if BAD_WORDS.search(version):
        raise GuardError("version %r carries a candidate/test word" % (version,))
    if not on_main:
        raise GuardError("tag %r is not on the release branch (origin/main)" % (ref_name,))
    return {"publishable": True, "tag": ref_name, "reason": "release identity verified"}


def _commit_on_main():
    r = subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], capture_output=True)
    return r.returncode == 0


def cmd_identity():
    env = os.environ
    try:
        out = decide_identity(env.get("GITHUB_EVENT_NAME", ""), env.get("GITHUB_REF_TYPE", ""),
                              env.get("GITHUB_REF_NAME", ""), env.get("GITHUB_REPOSITORY", ""),
                              app_version(), _commit_on_main() if env.get("GITHUB_REF_TYPE") == "tag" else False)
    except GuardError as exc:
        print("IDENTITY REFUSED: %s" % exc)
        return 1
    print("identity: %s" % out["reason"])
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write("publishable=%s\ntag=%s\n" % ("true" if out["publishable"] else "false", out["tag"]))
    return 0


def _members(path):
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            return [(i.filename, i.file_size) for i in z.infolist()]
    if path.endswith(".tar.gz"):
        with tarfile.open(path) as t:
            return [(m.name, m.size) for m in t.getmembers()]
    raise GuardError("unknown archive type: %s" % path)


def _norm(name):
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name


def _read_member(path, wanted):
    """Bytes of one member (normalised name), or None."""
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if _norm(info.filename) == wanted:
                    return z.read(info)
    else:
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if m.isfile() and _norm(m.name) == wanted:
                    return t.extractfile(m).read()
    return None


def check_beside(path):
    """The licence, notices, rebuild steps and pinned pystray source must be in the package."""
    names = {_norm(n) for n, _ in _members(path)}
    prefix = ""
    if not all(f in names for f in BESIDE_FILES):
        missing = [f for f in BESIDE_FILES if f not in names]
        raise GuardError("%s lacks %s" % (os.path.basename(path), missing))
    for member, digest in PINNED_SHA256.items():
        data = _read_member(path, member)
        if data is None or hashlib.sha256(data).hexdigest() != digest:
            raise GuardError("%s: %s is not the pinned upstream file" % (os.path.basename(path), member))
    licence = _read_member(path, "LICENSE") or b""
    if SECTION_11A.encode("utf-8") not in licence:
        raise GuardError("%s: LICENSE does not carry section 11A" % os.path.basename(path))
    return prefix


def inspect_assets(directory, tag, require_notes_dir=None):
    """Raise GuardError unless the directory holds exactly this tag's two assets and nothing else."""
    if not TAG_RE.match(tag or ""):
        raise GuardError("tag %r is not a production tag" % (tag,))
    found = sorted(f for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f)))
    want = expected_assets(tag)
    if found != want:
        raise GuardError("assets differ. expected %s, found %s" % (want, found))
    for name in found:
        if BAD_WORDS.search(name[len(PRODUCT) + 1 + len(tag):]):
            raise GuardError("candidate/test word in asset name %r" % name)
        path = os.path.join(directory, name)
        if os.path.getsize(path) < 1024 * 1024:
            raise GuardError("%s is implausibly small" % name)
        names = _members(path)
        if not names:
            raise GuardError("%s is empty" % name)
        for member, _size in names:
            low = member.lower().replace("\\", "/")
            for bad in FORBIDDEN_PARTS:
                if bad in low:
                    raise GuardError("%s contains forbidden member %r" % (name, member))
            why = forbidden_executable_member(_norm(member))
            if why:
                raise GuardError("%s contains forbidden member %r (%s)" % (name, member, why))
    for name in found:
        check_beside(os.path.join(directory, name))
    with zipfile.ZipFile(os.path.join(directory, "%s-%s-windows-x86_64.zip" % (PRODUCT, tag))) as z:
        tops = {_norm(i.filename).split("/")[0] for i in z.infolist()}
        if tops != WINDOWS_TOP:
            raise GuardError("the Windows archive top level must be exactly %s, found %s" % (sorted(WINDOWS_TOP), sorted(tops)))
    return want


#: the one public CA bundle that legitimately ends in .pem (a certificate list, not key material)
ALLOWED_PEM = ("certifi/cacert.pem",)  # also inside a macOS bundle: matched by suffix


def forbidden_executable_member(name):
    """Why a member of the packaged executable must not be there, or "" if it may be."""
    n = name.replace("\\", "/").lower()
    if n.startswith("licenses/"):
        return ""
    if n.startswith("archive/") or "/archive/" in n:
        return "superseded archive content"
    if n.endswith(".pem") and not n.endswith(ALLOWED_PEM):
        return "certificate or key file"
    for ext in (".key", ".pfx", ".p12", ".env"):
        if n.endswith(ext):
            return "key or environment file"
    for bad in ("units_cache", "scan_results", "producer_config", "firmware/", "soak", "overnight"):
        if bad in n:
            return "runtime state, firmware or qualification material"
    return ""


def inspect_executable(exe_path, version):
    """Read the PyInstaller table of contents of the packaged exe (needs PyInstaller; build job only)."""
    from PyInstaller.archive.readers import CArchiveReader  # noqa: PLC0415
    names = list(CArchiveReader(exe_path).toc.keys())
    if not names:
        raise GuardError("the executable's table of contents read as empty; a check that parsed nothing proves nothing")
    low = [n.replace("\\", "/").lower() for n in names]
    need = ["license", "third_party_notices.md", "licenses/manifest.json", "licenses/pystray/copying",
            "licenses/pystray/copying.lgpl"]
    for item in need:
        if item not in low:
            raise GuardError("the executable does not carry %s" % item)
    for n in low:
        reason = forbidden_executable_member(n)
        if reason:
            raise GuardError("forbidden content in the executable: %s (%s)" % (n, reason))
    return len(names)


def write_checksums(directory, out_name="SHA256SUMS.txt"):
    lines = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if name == out_name or not os.path.isfile(path):
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        lines.append("%s *%s\n" % (digest.hexdigest(), name))
    with open(os.path.join(directory, out_name), "w", encoding="utf-8", newline="\n") as handle:
        handle.writelines(lines)
    return lines


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    try:
        if argv[1] == "identity":
            return cmd_identity()
        if argv[1] == "inspect":
            tag = os.environ.get("RELEASE_TAG", "")
            inspect_assets(argv[2], tag)
            print("package inspection passed for", tag)
            return 0
        if argv[1] == "stage":
            print("\n".join(stage(argv[2], argv[3], argv[4])))
            return 0
        if argv[1] == "inspect-exe":
            print("executable TOC entries:", inspect_executable(argv[2], app_version()))
            return 0
        if argv[1] == "checksums":
            for line in write_checksums(argv[2]):
                print(line, end="")
            return 0
    except GuardError as exc:
        print("REFUSED: %s" % exc)
        return 1
    print("unknown subcommand")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
