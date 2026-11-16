# Rebuilding LumaSuite with a different pystray

LumaSuite contains the LGPL-licensed library **pystray 0.19.5** (see `THIRD_PARTY_NOTICES.md` and
`third_party/pystray-0.19.5/PROVENANCE.md`). Section 11A of `LICENSE` lets you modify pystray and rebuild or
recombine LumaSuite with your modified, interface-compatible copy for your own permitted use. The LumaSuite
executable cannot be patched in place: pystray is compiled into it. Rebuild instead.

You need: Windows 10/11 x86-64 (the platform the release was built and tested on), Python 3.12 (the release uses
3.12.16) from python.org, internet access to install the pinned build dependencies, and the LumaSuite source for the
release (the public repository, https://github.com/Hall-Research-Technologies/LumaSuite, at the release tag).
The folder `third_party/pystray-0.19.5/` ships beside `LumaSuite.exe` in the release package and is also in the repository.

1. Copy `third_party/pystray-0.19.5/source/` somewhere writable and make your changes to the files under `lib/pystray/`.
   Keep the version `0.19.5` in `lib/pystray/_info.py` (the build installs dependencies with pinned versions and does not
   replace an already installed pystray of the same version).
2. In the LumaSuite source folder create an isolated environment and install the pinned build dependencies:

   ```
   py -3.12 -m venv rebuild-venv
   rebuild-venv\Scripts\python -m pip install --upgrade pip
   rebuild-venv\Scripts\python -m pip install -r requirements-build-lock.txt
   ```
3. Install your modified pystray over the pinned one:

   ```
   rebuild-venv\Scripts\python -m pip install --no-deps --force-reinstall <path to your modified source folder>
   ```
4. Build:

   ```
   rebuild-venv\Scripts\python build_executable.py windows
   ```
   The result is `dist\Windows\LumaSuite.exe`, a LumaSuite that runs against your pystray. It will not be byte-for-byte
   identical to the released executable (build timestamps differ).
5. Check that your copy is the one inside, for example by adding a recognisable string to your pystray and searching the
   built executable's archive for it.

This builds for your own use. Section 7 of `LICENSE` still governs distributing a Modified Version of LumaSuite; the
LGPL governs distributing pystray and your changes to it. macOS and Linux builds use `build_executable.py macos` or
`linux` with the same steps; those platforms have not been exercised by Hall Research.
