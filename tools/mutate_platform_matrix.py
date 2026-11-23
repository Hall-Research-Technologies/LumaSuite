#!/usr/bin/env python3
"""Break each platform-matrix, updater and release-identity rule on purpose; the focused suites must fail.

    python tools/mutate_platform_matrix.py <repository root> [--list]

Run it in a THROWAWAY worktree (`git worktree add --detach <dir> <commit>`): it rewrites production files in
place and restores them in a `finally` block. Each mutation edits one file, runs the suites that guard it, and
requires a failure. Anchors are translated to the file's own line endings, and an anchor that cannot be found
exactly once is an ERROR, never a pass. The whole output of every run is kept in the harness's own output so a
survivor can be investigated, not guessed at. Run it with the pinned build/test interpreter.
"""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else Path(".")
CR, NL, B = chr(13), chr(10), chr(92)

PY = ("test_platform_matrix", "test_updater_proof", "test_update_assets", "test_update_production_identity",
      "test_inspect_release", "test_release_guard", "test_dependency_hygiene", "test_release_consistency",
      "test_licence_package", "test_notices")
WF = ".github/workflows/build-cross-platform.yml"
LS = "lumaserver.py"
RG = "tools/release_guard.py"
IR = "tools/inspect_release.py"
BE = "build_executable.py"
VM = "tools/verify_macho_arch.py"

W = "          - os: windows-latest" + NL + "            platform: Windows" + NL + '            mac_arch: ""' + NL
MAC_ROW = "          - os: macos-15" + NL + "            platform: macOS" + NL + "            mac_arch: arm64" + NL

# (name, file, anchor, replacement, runner) where runner is "py" or "js:<file>"
MUTATIONS = [
    ("the vendor Mach-O exemption covers every file under Frameworks", IR,
     'data[:4] in _MACHO_MAGIC', 'True', "py"),
    ("the vendor Mach-O exemption covers the launcher too", IR,
     'member.startswith("LumaSuite.app/Contents/Frameworks/")', 'member.startswith("LumaSuite.app/Contents/")', "py"),
    ("the vendor Mach-O exemption is removed", IR,
     'if _BUILD_PATH.search(data) and not _is_vendor_macho(member, data):\n                    found.append("%s: %s contains', 'if _BUILD_PATH.search(data):\n                    found.append("%s: %s contains', "py"),
    ("the private-name pattern loses its word boundary", IR,
     r'|LumaSuite[\s_-]*(Plus|Pro|Enterprise|Premium)\b"', r'|LumaSuite[\s_-]*(Plus|Pro|Enterprise|Premium)"', "py"),
    ("any password value is exempt, not only the factory default", IR,
     "(?!password[" + chr(92) + "\"'])", "(?!pass)", "py"),
    ("an Intel macOS build row returns", WF, MAC_ROW,
     "          - os: macos-26-intel" + NL + "            platform: macOS" + NL + "            mac_arch: x86_64" + NL
     + "            artifact_name: LumaSuite-macOS-intel" + NL + MAC_ROW, "py"),
    ("a Linux build row returns", WF, MAC_ROW,
     "          - os: ubuntu-latest" + NL + "            platform: Linux" + NL + '            mac_arch: ""' + NL
     + "            artifact_name: LumaSuite-Linux" + NL + MAC_ROW, "py"),
    ("the macOS runner label floats", WF, "          - os: macos-15" + NL, "          - os: macos-latest" + NL, "py"),
    ("the runner architecture case admits Linux", WF, "Windows-X64|macOS-ARM64) ;;", "Windows-X64|macOS-ARM64|Linux-X64) ;;", "py"),
    ("an x86_64-only interpreter is accepted", WF, "'x86_64' in p and 'universal2' not in p", "False", "py"),
    ("lipo's answer is not required to be exactly arm64", WF, 'test "$archs" = "arm64" ||', "true ||", "py"),
    ("the Mach-O walk over the bundle is dropped", WF, "python tools/verify_macho_arch.py --path", "echo skipped --path", "py"),
    ("the Mach-O walk over the distributed zip is dropped", WF, "python tools/verify_macho_arch.py --zip", "echo skipped --zip", "py"),
    ("the bundle identity check is dropped", WF, "python tools/verify_macho_arch.py --bundle-identity", "echo skipped --bundle-identity", "py"),
    ("the ad hoc signature is no longer asserted", WF, 'grep -q "Signature=adhoc"', 'grep -q "Signature="', "py"),
    ("the Windows PE machine type is no longer required", WF, "sys.exit(0 if machine == 0x8664 else", "sys.exit(0 if True else", "py"),
    ("the packaged application is no longer started", WF, 'python tests/smoke_packaged.py "$exe"', "echo skipped", "py"),
    ("a tagged package is no longer inspected", WF,
     'python tools/inspect_release.py "$zip" --version "$RUN_REF_NAME"', "echo skipped", "py"),
    ("the Windows executable table of contents is no longer checked in CI", WF,
     "python tools/release_guard.py inspect-exe", "echo skipped inspect-exe", "py"),
    ("Linux packaging returns", WF, "      - name: Upload packaged release asset" + NL,
     "      - name: Package Linux release asset" + NL + "        if: runner.os == 'Linux'" + NL
     + '        run: tar -C stage -czf "release/LumaSuite-${{ github.ref_name }}-linux-x86_64.tar.gz" .' + NL + NL
     + "      - name: Upload packaged release asset" + NL, "py"),
    ("the release gate admits the Intel archive", RG, 'DISTRIBUTED_TAILS = ("windows-x86_64.zip", "macos-arm64.zip")',
     'DISTRIBUTED_TAILS = ("windows-x86_64.zip", "macos-x86_64.zip", "macos-arm64.zip")', "py"),
    ("the stage function can lay out Linux again", RG,
     '    else:' + NL + '        raise GuardError("unknown platform %r" % platform_name)',
     '    elif platform_name == "linux":' + NL + '        pass' + NL + '    else:' + NL
     + '        raise GuardError("unknown platform %r" % platform_name)', "py"),
    ("the executable check no longer needs the GPL text", RG,
     '"licenses/pystray/copying",' + NL + '            "licenses/pystray/copying.lgpl"]', '"licenses/pystray/copying.lgpl"]', "py"),
    ("an empty executable table of contents passes", RG, NL + "    if not names:", NL + "    if False:", "py"),
    ("the inspector accepts a Linux archive name", IR, "PLATFORM_TAILS = release_guard.DISTRIBUTED_TAILS",
     'PLATFORM_TAILS = release_guard.DISTRIBUTED_TAILS + ("linux-x86_64.tar.gz",)', "py"),
    ("the updater offers an Intel Mac the Intel archive", LS,
     '        return ("-macos-arm64.zip",) if machine in ("arm64", "aarch64") else ()',
     '        return ("-macos-arm64.zip",) if machine in ("arm64", "aarch64") else ("-macos-x86_64.zip",)', "py"),
    ("the updater offers Linux an archive", LS,
     '        return ("-macos-arm64.zip",) if machine in ("arm64", "aarch64") else ()' + NL + "    return ()",
     '        return ("-macos-arm64.zip",) if machine in ("arm64", "aarch64") else ()' + NL
     + '    if platform_name.startswith("linux"):' + NL + '        return ("-linux-x86_64.tar.gz",)' + NL + "    return ()", "py"),
    ("the updater offers Windows on Arm the x86-64 archive", LS,
     '        return ("-windows-x86_64.zip", "-windows-x86_64.exe") if x86 else ()',
     '        return ("-windows-x86_64.zip", "-windows-x86_64.exe")', "py"),
    ("an unsupported computer is told nothing", LS,
     "else UPDATE_NO_PACKAGE_UNSUPPORTED)", "else None)", "py"),
    ("a missing archive in the release is not explained", LS,
     'if state == "update_available" and not asset_url:', "if False:", "py"),
    ("a malformed asset list crashes the check", LS,
     "    if not isinstance(assets, (list, tuple)):", "    if False:", "py"),
    ("a non-object asset crashes the check", LS,
     "            if not isinstance(asset, dict):", "            if False:", "py"),
    ("an upper-case V installed version is unreadable", LS, 'r"^[vV]?(', 'r"^v?(', "py"),
    ("a draft or prerelease is offered", LS, 'if payload.get("draft") or payload.get("prerelease"):', "if False:", "py"),
    ("a tag that is not exactly v1.2.3 is accepted", LS, '_PRODUCTION_TAG_RE = re.compile(r"v\\d+\\.\\d+\\.\\d+\\Z")',
     '_PRODUCTION_TAG_RE = re.compile(r"v\\d+\\.\\d+\\.\\d+")', "py"),
    ("an asset for another tag is picked", LS, 'if name != ("lumasuite-%s%s" % (tag, suffix)).lower():', "if False:", "py"),
    ("the update check compares versions as text", LS,
     "    return (a > b) - (a < b)", "    return (str(a) > str(b)) - (str(a) < str(b))", "py"),
    ("a failed check is reported as current", LS,
     '        return {"state": "unable_to_check", "detail": type(exc).__name__}',
     '        return {"state": "current", "detail": type(exc).__name__}', "py"),
    ("an off-site download address is handed to the browser", LS,
     "    if not _is_official_release_url(asset_url):", "    if False:", "py"),
    ("an Intel Mac may build as arm64", BE, "    if machine not in ('arm64', 'aarch64'):", "    if False:", "py"),
    ("a non-arm64 architecture request is accepted", BE, "    if requested and requested.lower() != 'arm64':", "    if False:", "py"),
    ("the macOS bundle is not signed again after stamping", BE,
     "    run_command(['codesign', '--force', '--deep', '--sign', '-', app_bundle])", "    pass", "py"),
    ("the Windows build no longer checks its table of contents", BE,
     "    assert_windows_package_contents(dist_target / 'LumaSuite.exe')" + NL + NL + "    # Clean up temp folder",
     "    pass" + NL + NL + "    # Clean up temp folder", "py"),
    ("a failed contents check no longer fails the build", BE,
     '        print(f"Error: the packaged executable failed its contents check: {exc}")' + NL + "        sys.exit(1)",
     '        print(f"Error: the packaged executable failed its contents check: {exc}")', "py"),
    ("a universal binary is accepted", VM,
     '        if info["kind"] == "fat":' + NL + '            problems.append("%s is a universal binary (%s), not a thin %s image"',
     '        if False:' + NL + '            problems.append("%s is a universal binary (%s), not a thin %s image"', "py"),
    ("an empty bundle is accepted", VM, "    if found == 0:", "    if False:", "py"),
    ("a missing main executable is accepted", VM, "    if main is not None and not main_seen:", "    if False:", "py"),
    ("a wrong CPU type is accepted", VM, '        elif info["arches"] != [expect]:', "        elif False:", "py"),
    ("a fat header is not recognised", VM, "    if magic_be in (_FAT, _FAT_64):", "    if False:", "py"),
    ("the wrong bundle identifier is accepted", VM, '    if info.get("CFBundleIdentifier") != identifier:', "    if False:", "py"),
    ("a user-profile path is no longer detected", IR, "rb" + chr(34) + "(?i:[A-Za-z]:" + B + B + "+Users",
     "rb" + chr(34) + "(?i:[A-Za-z]:" + B + B + "+NoSuchFolder", "py"),
    ("a macOS or Linux home path is no longer detected", IR, "(?<![A-Za-z0-9_.-])/(?:Users|home)/", "(?<![A-Za-z0-9_.-])/(?:NoSuchRoot)/", "py"),
    ("a drive-rooted development path is no longer detected", IR, "(?:Windows|Program Files|ProgramData|Users|Temp|tmp|a)",
     "(?:[A-Za-z0-9_-]+|Program Files)", "py"),
    ("a folder name may be any byte again", IR, '_SEG = rb"[A-Za-z0-9_.()-]{2,64}"',
     '_SEG = rb"[^' + B + B + '/' + B + 's' + B + '"' + "'" + '<>|*?:%$]{1,64}"', "py"),
    ("a placeholder exemption swallows a real name", IR, "someone|example)(?![A-Za-z0-9_-])", "someone|example|Alice)(?![A-Za-z0-9_-])", "py"),
    ("a placeholder prefix exempts a longer real name", IR, "(?![A-Za-z0-9_-])|[<%$~{])", "|[<%$~{])", "py"),
    ("the detector names an author folder again", IR, "someone|example)(?![A-Za-z0-9_-])", "someone|example|" + "soft" + "wareDEV" + ")(?![A-Za-z0-9_-])", "py"),
    ("the Intel cryptography pin returns", "requirements-build.txt", "cryptography==50.0.2",
     'cryptography==50.0.2 ; sys_platform != "darwin" or platform_machine != "x86_64"' + NL
     + 'cryptography==48.0.1 ; sys_platform == "darwin" and platform_machine == "x86_64"', "py"),
    ("the settings page drops the no-package sentence", "ui/settings.js",
     "if (!data.asset_url && data.package_message) {", "if (false) {", "js:settings_parity_test.js"),
]


def run_module(name):
    result = subprocess.run([sys.executable, "tests/%s.py" % name], cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def run_py():
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(run_module, PY))
    failing = [(name, out) for name, (code, out) in zip(PY, results) if code != 0]
    text = "".join("[%s]\n%s" % (n, o) for n, o in failing)
    return (1 if failing else 0), text


def run_js(name):
    result = subprocess.run(["node", "tests/" + name], cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def selected():
    """--only <text> (repeatable) keeps the mutations whose name contains the text, for re-checking survivors."""
    only = [sys.argv[i + 1] for i, arg in enumerate(sys.argv[:-1]) if arg == "--only"]
    return [m for m in MUTATIONS if not only or any(o in m[0] for o in only)]


def main():
    if "--list" in sys.argv:
        for entry in MUTATIONS:
            print(entry[0])
        return 0
    base_code, base_out = run_py()
    print("baseline (unmutated): exit %d" % base_code)
    if base_code != 0:
        print(base_out[-3000:])
        return 2
    caught, survivors, errors = 0, [], []
    chosen = selected()
    for name, relative, anchor, replacement, runner in chosen:
        path = root / relative
        original = path.read_bytes()
        text = original.decode("utf-8")
        crlf = CR + NL in text
        a = anchor.replace(NL, CR + NL) if crlf else anchor
        b = replacement.replace(NL, CR + NL) if crlf else replacement
        if text.count(a) != 1:
            errors.append("%s: anchor found %d times" % (name, text.count(a)))
            print("ERROR  %s (anchor found %d times)" % (name, text.count(a)))
            continue
        try:
            path.write_bytes(text.replace(a, b).encode("utf-8"))
            code, output = run_js(runner[3:]) if runner.startswith("js:") else run_py()
        finally:
            path.write_bytes(original)
        failing = [l for l in output.splitlines() if l.startswith(("FAIL:", "ERROR:", "FAILED", "AssertionError"))][:3]
        if code != 0:
            caught += 1
            print("caught %s -> %s" % (name, "; ".join(failing)[:170] or "exit %d" % code))
        else:
            survivors.append(name)
            print("SURVIVED %s" % name)
    print("\n%d mutation(s): %d caught, %d survived, %d error(s)" % (len(MUTATIONS), caught, len(survivors), len(errors)))
    for item in survivors + errors:
        print("  - " + item)
    return 1 if survivors or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
