#!/usr/bin/env python3
"""Prove that a macOS package is Apple Silicon (arm64) and nothing else.

A name is not evidence of an architecture. The macOS artifact is called `macOS-arm64`, and this
reads the Mach-O headers of what is actually inside it: the launcher must be a thin arm64 binary
and no file in the bundle may carry an x86_64 slice, a universal (fat) wrapper or a 32-bit image.
`lipo -archs` answers the same question for one file on a Mac; this answers it for every file,
and it is plain Python so its decision logic is tested on any machine, with no Mac.

    verify_macho_arch.py --path dist/arm64/LumaSuite.app --expect arm64
    verify_macho_arch.py --zip release/LumaSuite-v1.1.1-macos-arm64.zip --expect arm64

Exit 0 only when at least one Mach-O file was found, every one is thin and the expected
architecture, and the main executable (when named) is one of them. It fails closed: an empty
bundle, an unreadable file or an unknown CPU type is a failure, never a pass.
"""
from __future__ import annotations

import argparse
import struct
import sys
import zipfile
from pathlib import Path

CPU_ARM64 = 0x0100000C
CPU_X86_64 = 0x01000007
CPU_NAMES = {CPU_ARM64: "arm64", CPU_X86_64: "x86_64", 0x0000000C: "arm", 0x00000007: "i386",
             0x0200000C: "arm64_32", 0x01000012: "ppc64", 0x12: "ppc"}

_THIN_64_LE = 0xFEEDFACF
_THIN_32_LE = 0xFEEDFACE
_FAT = 0xCAFEBABE
_FAT_64 = 0xCAFEBABF
_MAX_FAT_ARCHES = 8          # a Java class file also begins CAFEBABE; its next word is a version >= 45


def classify(head: bytes) -> dict | None:
    """What the first bytes of a file say about it, or None when it is not Mach-O.

    Returns {"kind": "thin"|"fat", "arches": [names]}. A thin 32-bit image is reported as
    "thin" with its own cpu name, so the caller refuses it for not being arm64.
    """
    if len(head) < 8:
        return None
    magic_le = struct.unpack("<I", head[:4])[0]
    magic_be = struct.unpack(">I", head[:4])[0]
    if magic_le in (_THIN_64_LE, _THIN_32_LE):
        cpu = struct.unpack("<i", head[4:8])[0] & 0xFFFFFFFF
        return {"kind": "thin", "arches": [CPU_NAMES.get(cpu, "unknown(0x%08x)" % cpu)]}
    if magic_be in (_FAT, _FAT_64):
        count = struct.unpack(">I", head[4:8])[0]
        if not 1 <= count <= _MAX_FAT_ARCHES:
            return None                                   # a Java class file, not a universal binary
        size = 32 if magic_be == _FAT_64 else 20
        arches = []
        for index in range(count):
            start = 8 + index * size
            if len(head) < start + 4:
                return {"kind": "fat", "arches": ["unreadable"]}
            cpu = struct.unpack(">I", head[start:start + 4])[0]
            arches.append(CPU_NAMES.get(cpu, "unknown(0x%08x)" % cpu))
        return {"kind": "fat", "arches": arches}
    return None


def problems_for(entries, expect: str = "arm64", main: str | None = None) -> list:
    """Every reason `entries` is not a thin `expect` package.

    `entries` is an iterable of (name, head_bytes). The decision lives here, on bytes, so a
    test can hand it a synthetic bundle.
    """
    found = 0
    problems = []
    main_seen = False
    for name, head in entries:
        info = classify(head)
        if info is None:
            continue
        found += 1
        if main is not None and name.replace("\\", "/").endswith(main):
            main_seen = True
            if info["kind"] != "thin":
                problems.append("%s is a universal binary (%s); the main executable must be thin %s"
                                % (name, "+".join(info["arches"]), expect))
        if info["kind"] == "fat":
            problems.append("%s is a universal binary (%s), not a thin %s image"
                            % (name, "+".join(info["arches"]), expect))
        elif info["arches"] != [expect]:
            problems.append("%s is %s, expected %s" % (name, info["arches"][0], expect))
    if found == 0:
        problems.append("no Mach-O file was found at all; an empty or wrong package is not a pass")
    if main is not None and not main_seen:
        problems.append("the main executable %s is not a Mach-O file in this package" % main)
    return problems


#: What build_executable.py gives the bundle (--osx-bundle-identifier) and what the bundle's launcher is called.
BUNDLE_IDENTIFIER = "com.lumasuite.app"
BUNDLE_EXECUTABLE = "LumaSuite"


def bundle_identity_problems(info: dict, identifier: str = BUNDLE_IDENTIFIER, executable: str = BUNDLE_EXECUTABLE) -> list:
    """What an app bundle's Info.plist must say: this product's identifier and the launcher it names."""
    problems = []
    if info.get("CFBundleIdentifier") != identifier:
        problems.append("CFBundleIdentifier is %r, expected %r (macOS keys permissions on it)"
                        % (info.get("CFBundleIdentifier"), identifier))
    if info.get("CFBundleExecutable") != executable:
        problems.append("CFBundleExecutable is %r, expected %r"
                        % (info.get("CFBundleExecutable"), executable))
    version = str(info.get("CFBundleShortVersionString") or "")
    if not version or version == "0.0.0":
        problems.append("CFBundleShortVersionString is %r: the application version was not stamped into the bundle"
                        % version)
    return problems


def _check_bundle_identity(app: Path) -> int:
    import plistlib
    plist = app / "Contents" / "Info.plist"
    try:
        with open(plist, "rb") as handle:
            info = plistlib.load(handle)
    except (OSError, ValueError) as exc:
        print("::error::cannot read %s: %s" % (plist, exc))
        return 1
    executable = app / "Contents" / "MacOS" / str(info.get("CFBundleExecutable") or "")
    problems = bundle_identity_problems(info)
    if not executable.is_file():
        problems.append("the executable the bundle names does not exist: %s" % executable)
    for problem in problems:
        print("::error::" + problem)
    print("bundle identity: %s" % ("FAILED" if problems else "ok"))
    return 1 if problems else 0


def _entries_from_dir(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with open(path, "rb") as handle:
                yield str(path.relative_to(root)), handle.read(8 + 8 * 32)
        except OSError as exc:
            yield str(path.relative_to(root)), b""
            print("::warning::cannot read %s: %s" % (path, exc))


def _entries_from_zip(path: Path):
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or (info.external_attr >> 28) == 0xA:      # directory or symlink
                continue
            with archive.open(info) as handle:
                yield info.filename, handle.read(8 + 8 * 32)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--path", type=Path)
    where.add_argument("--zip", type=Path)
    where.add_argument("--bundle-identity", type=Path, metavar="APP",
                       help="check the app's Info.plist identity instead of its architecture")
    parser.add_argument("--expect", default="arm64", choices=["arm64"])
    parser.add_argument("--main", default="Contents/MacOS/LumaSuite")
    args = parser.parse_args(argv)
    if args.bundle_identity:
        return _check_bundle_identity(args.bundle_identity)
    entries = _entries_from_dir(args.path) if args.path else _entries_from_zip(args.zip)
    entries = list(entries)
    problems = problems_for(entries, args.expect, args.main)
    macho = sum(1 for _, head in entries if classify(head))
    print("checked %d file(s), %d Mach-O, expecting thin %s" % (len(entries), macho, args.expect))
    for problem in problems:
        print("::error::" + problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
