# pystray 0.19.5 - corresponding source and provenance

LumaSuite uses **pystray 0.19.5** (system-tray icon library) unmodified. pystray is licensed under the GNU Lesser
General Public License version 3 (GNU GPL version 3 text in `source/COPYING`, LGPL version 3 text in
`source/COPYING.LGPL`). Copyright (C) Moses Palmer. Upstream: https://github.com/moses-palmer/pystray and
https://pypi.org/project/pystray/0.19.5/. Nothing in this folder has been modified.

| File | What it is | SHA-256 |
|---|---|---|
| `pystray-0.19.5-github-source.tar.gz` | Upstream source archive of tag `v0.19.5` (downloaded from https://github.com/moses-palmer/pystray/archive/refs/tags/v0.19.5.tar.gz, 48,142 bytes; the file name here adds `-github-source`) | `2ab6bd15c14b400c9e8271a82c9de8adc0f1a0cacfc14080bdd4fb42bb88ec92` |
| `pystray-0.19.5-py2.py3-none-any.whl` | The PyPI wheel that the build installs (49,068 bytes). PyPI lists no source distribution for 0.19.5. | `a0c2229d02cf87207297c22d86ffc57c86c227517b038c0d3c59df79295ac617` (identical to the digest PyPI publishes: `a0c2229d02cf87207297c22d86ffc57c86c227517b038c0d3c59df79295ac617`) |
| `source/` | The archive above, extracted without changes (top-level folder removed). Use it to modify and rebuild. | file-by-file equal to the archive (checked by `tests/test_licence_package.py`) |

## What was checked

- All 13 `pystray/*.py` modules in the wheel are byte-identical to `lib/pystray/*.py` in the source archive (13 of 13).
- The pystray installed in the LumaSuite build environment matches the wheel's `RECORD` (13 of 13 file hashes).
- Pure Python: LumaSuite's executable contains pystray as compiled modules in its archive; there is no native pystray
  binary and nothing to replace in place. To use a different pystray, rebuild LumaSuite: see `REBUILD.md`.
