# Building LumaSuite for Windows and macOS

This guide explains how to build the standalone LumaSuite application and how the release workflow
does it. Two targets are distributed: Windows x86-64 and macOS Apple Silicon (arm64). Each is built on its own
operating system; PyInstaller does not cross-compile.

## Requirements

- Python 3.12 (the version the lock was resolved on and CI uses). The Windows release build uses a
  python-build-standalone CPython 3.12.16, because it carries OpenSSL 3.5.x.
- `pip install -r requirements-build-lock.txt` (every package, direct and transitive, an exact pin).
  `requirements-build.txt` lists only the direct pins and is what you edit; the lock is regenerated from
  a fresh environment (see the header of the lock file).
- Node 24 only if you want to run the JavaScript suites (`python run_tests.py` runs everything).

## What the build packages

`build_executable.py` calls PyInstaller with `app_launcher.py` as the entry point and adds:

- `ui/` (minus `*.zip`, `logos`, `imagestream`, `localdata`), which includes the User Guide's screenshots in
  `ui/guide/` (the README's screenshots in `docs/screenshots/` are published documentation and are not packaged),
- `hallway.png` and `atlona.png`,
- `licenses/` and `THIRD_PARTY_NOTICES.md` (the third-party licence texts and the notices that index them;
  served at Settings > About > License). The build stops if they are missing.

It never packages a firmware folder, certificates, keys or credentials: the firmware folder is whatever the
operator chooses in Settings, and runtime data lives in the application data folder at run time.
`LumaSuite.spec` in the repository is stale and not used by any build.

After changing `requirements-build-lock.txt`, regenerate the notices and commit the result:

```bash
python tools/collect_licenses.py          # rewrites licenses/ and THIRD_PARTY_NOTICES.md
python tools/collect_licenses.py --check  # fails if they no longer match the installed packages
```

Run it with the locked environment active. `tests/test_notices.py` fails if a locked package is neither listed
as shipped nor as a build tool, or if a text is missing.

## Windows

```bash
pip install -r requirements-build-lock.txt
python build_executable.py windows
```

Output: `dist/Windows/LumaSuite.exe`, a single windowed executable (`--onefile --windowed`) carrying Windows
version resources generated from the one authoritative version in `lumaserver.py`. Run it by double-click: a
launcher window opens, LumaSuite starts on `http://127.0.0.1:8090` (or the next free port, or the fixed port
chosen in Settings) and the default browser opens. *Open Browser* reopens it and *Exit* stops the server.

The executable is not Authenticode-signed: Windows SmartScreen shows "Windows protected your PC"; choose *More info*,
then *Run anyway*.

## macOS

```bash
pip install -r requirements-build-lock.txt
python build_executable.py macos
```

Output: `dist/macOS/LumaSuite.app` (`--onedir --windowed`, bundle identifier `com.lumasuite.app`). The build is
Apple Silicon (arm64) only and is not a universal binary: on an Intel Mac, or with `PYI_MACOS_ARCH` set to anything
but `arm64`, it refuses. The release builds it on `macos-15`, a native arm64 runner, and the workflow asserts the
runner, the interpreter, `lipo -archs` and the Mach-O header of every file in the bundle and in the zip.
The app carries an **ad hoc** signature: the build edits `Info.plist` to stamp the version, so it signs the bundle
again (`codesign --sign -`) and verifies it. It is not Developer ID signed and not notarised; Gatekeeper asks you
to confirm (right-click, Open). The minimum macOS version has not been established. The icon is `ui/favicon.icns`
(or `ui/LumaSuite.icns` if you add one).

No Intel Mac and no Linux package is built or offered.

## Command-line arguments

`--port=N` starts on port N for that run and is never saved; `--host=` is accepted for the server. The saved fixed
port is set in Settings, not on the command line.

```bash
LumaSuite.exe --port=8080
LumaSuite.app/Contents/MacOS/LumaSuite --port=8080
./LumaSuite --port=8080
```

A busy fixed port is never replaced silently: the launcher offers *Retry*, *Start on Alternate Port* (this session
only), *Change Fixed Port* and *Exit*.

## Verify the package (do not trust "build succeeded")

"PyInstaller completed successfully" proves nothing runs. Start the artifact and ask it:

```bash
python tests/smoke_packaged.py dist/Windows/LumaSuite.exe
```

It starts the executable against a throwaway data folder, waits for its server and checks the version, pages,
assets, the licence route and a bundled licence text, and the Remote Access defaults. It is run by hand and is not
part of CI, so a release must have it run on the artifact that is actually going to be published.

## GitHub Actions

`.github/workflows/build-cross-platform.yml`:

- **Triggers**: manual (`workflow_dispatch`), a push to `main`, and a tag matching `v*`.
- **Per platform** (Windows x86-64 on `windows-latest`, macOS Apple Silicon on `macos-15`; each is both the test runner and
  the distributed platform): assert the runner and interpreter architecture, checkout, Python 3.12, locked
  dependencies, `py_compile` of every server module, every Python suite, `node --check` and every Node suite, and
  only then the build. Any failing step stops that platform's build. The Windows build reads the packaged
  executable's table of contents and fails unless the licence, the notices and the pystray GPL and LGPL texts are in it.
  The packages are then inspected with `tools/inspect_release.py` and the packaged application is started and asked
  for its version, its pages and its licence texts (`tests/smoke_packaged.py`).
- **Artifacts**: `LumaSuite-Windows-x86_64` and `LumaSuite-macOS-arm64` (the built application) plus a `warn-*` log and a
  `release-*` packaged archive: `LumaSuite-<tag>-windows-x86_64.zip` and `LumaSuite-<tag>-macos-arm64.zip`. The update
  check chooses its download by these tails (`-windows-x86_64.zip`, `-macos-arm64.zip`), so renaming an archive means
  changing `_platform_asset_suffixes` in `lumaserver.py`; `tests/test_update_assets.py` and `tests/test_platform_matrix.py`
  tie the two together.
- **Publishing**: only a `v*` tag push publishes a GitHub Release, from the two `release-*` artifacts. A manual run or
  a push to `main` builds and uploads artifacts but publishes nothing. Tags are never created by the workflow, and a tag build stops at once unless the tag is `v` plus the version in `lumaserver.py` (the one authoritative version). The macOS build stamps that version into the bundle's `Info.plist`; the Windows build writes it into the executable's version resource.

For a manual build of a release, tag the commit (for example `v1.1.1`) only when the release is approved.
