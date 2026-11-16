# Building LumaSuite for Windows, macOS and Linux

This guide explains how to build the standalone LumaSuite application and how the release workflow
does it. Each target is built on its own operating system; PyInstaller does not cross-compile.

## Requirements

- Python 3.12 (the version the lock was resolved on and CI uses). The Windows release build uses a
  python-build-standalone CPython 3.12.16, because it carries OpenSSL 3.5.x.
- `pip install -r requirements-build-lock.txt` (every package, direct and transitive, an exact pin).
  `requirements-build.txt` lists only the direct pins and is what you edit; the lock is regenerated from
  a fresh environment (see the header of the lock file).
- Linux also needs `python3-tk libx11-dev libxkbcommon-x11-0` and a graphical session to run.
- Node 24 only if you want to run the JavaScript suites (`python run_tests.py` runs everything).

## What the build packages

`build_executable.py` calls PyInstaller with `app_launcher.py` as the entry point and adds:

- `ui/` (minus `*.zip`, `logos`, `imagestream`, `localdata`),
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

The executable is not code-signed: Windows SmartScreen shows "Windows protected your PC"; choose *More info*,
then *Run anyway*.

## macOS

```bash
pip install -r requirements-build-lock.txt
python build_executable.py macos
```

Output: `dist/macOS/LumaSuite.app` (`--onedir --windowed`, bundle identifier `com.lumasuite.app`). The build is
for **one architecture**: that of the machine, or the value of `PYI_MACOS_ARCH` (`x86_64` or `arm64`). It is not a
universal binary; the release builds an Intel app on `macos-26-intel` and an Apple Silicon app on `macos-26`.
The app is not signed or notarised; Gatekeeper asks you to confirm (right-click, Open). The minimum macOS version
has not been established. The icon is `ui/favicon.icns` (or `ui/LumaSuite.icns` if you add one).

"Bad CPU type in executable" means the wrong architecture was downloaded.

## Linux

```bash
pip install -r requirements-build-lock.txt
python build_executable.py linux
```

Output in `dist/Linux/`: `LumaSuite` (`--onefile --windowed`), `LumaSuite.desktop`, `LumaSuite.png`,
`run-lumasuite.sh` and `install-lumasuite-desktop.sh` (installs a launcher and icon for the current user). It is
built on the Ubuntu runner only and needs X11 or Wayland; other distributions are untested. If no window appears
over SSH, use `DISPLAY=:0 ./LumaSuite`.

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
- **Per platform** (Windows, macOS Intel, macOS Apple Silicon, Linux): checkout, Python 3.12, locked
  dependencies, `py_compile` of every server module, every Python suite, `node --check` and every Node suite, and
  only then the build. Any failing step stops that platform's build.
- **Artifacts**: `LumaSuite-Windows`, `LumaSuite-macOS-intel`, `LumaSuite-macOS-arm64`, `LumaSuite-Linux`
  (the built application) plus a `warn-*` log and a `release-*` packaged archive:
  `LumaSuite-<tag>-windows-x86_64.zip`, `LumaSuite-<tag>-macos-x86_64.zip`, `LumaSuite-<tag>-macos-arm64.zip` and
  `LumaSuite-<tag>-linux-x86_64.tar.gz`. The update check chooses its download by these tails
  (`-windows-x86_64.zip`, `-macos-x86_64.zip`, `-macos-arm64.zip`, `-linux-x86_64.tar.gz`), so renaming an archive means changing
  `_platform_asset_suffix` in `lumaserver.py`; `tests/test_update_assets.py` ties the two together.
- **Publishing**: only a `v*` tag push publishes a GitHub Release, from all four `release-*` artifacts. A manual run or
  a push to `main` builds and uploads artifacts but publishes nothing. Tags are never created by the workflow, and a tag build stops at once unless the tag is `v` plus the version in `lumaserver.py` (the one authoritative version). The macOS build stamps that version into the bundle's `Info.plist`; the Windows build writes it into the executable's version resource.

For a manual build of a release, tag the commit (for example `v1.1.0`) only when the release is approved.
