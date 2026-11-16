# AGENTS.md — LumaSuite Development Instructions

Persistent instructions for anyone (human or AI) doing development work in this
repository. Read this file and [PROJECT.md](PROJECT.md) before starting any
substantial work. `PROJECT.md` is the technical/product reference; this file
is the rulebook for how to work here.

## 0. What LumaSuite is (one paragraph)

LumaSuite is a Hall Research Flask application that discovers, configures,
routes, and firmware-upgrades **LumaStream** HDMI-over-IP encoder/decoder
hardware (`HW-LUMA-E****` / `HW-LUMA-D****`) and **Atlona AT-OME-CS31** HDMI
matrix switchers over JSON-RPC-over-WebSocket and legacy HTTP, plus a
"Producer" signage/streaming module (slates, logo overlay, ticker, RTMP). See
PROJECT.md for full architecture.

## 1. Baseline working rules

- Read `AGENTS.md` and `PROJECT.md` before substantial work.
- Preserve existing LumaSuite functionality unless a task explicitly changes it.
- **LumaSuite hardware/protocol behavior is authoritative for LumaSuite.**
  Never substitute OmniStream (or any other Hall Research / Atlona product
  line's) assumptions for LumaStream behavior. LumaStream's JSON-RPC method
  names, params shapes, and quirks (e.g. CS31's top-level-params variant, the
  password-omission cases) were reverse-engineered from real devices — treat
  them as ground truth, not as a generic protocol to "clean up."
- Do not make speculative protocol changes. If a device call's shape looks
  wrong or redundant, assume it was shaped that way to work around a real
  firmware quirk until proven otherwise by code inspection or controlled
  hardware testing.
- Existing proven device communication (WebSocket JSON-RPC via `_ws_call*`,
  the CS31 raw-socket firmware upload fallback, the HTML-scraping
  `_discover_switcher_upload_forms` fallback, etc.) remains authoritative
  until disproven by code inspection or controlled hardware testing. These
  exist because simpler approaches didn't work against real firmware.
- UI/UX changes must preserve underlying actions unless explicitly authorized.
  Do not silently remove controls or workflows during UI modernization —
  every control inventoried in PROJECT.md must still exist (or be explicitly
  and knowingly retired) after a UI change.
- Hall Research applications should have a consistent family UI/UX while
  each product retains its correct product-specific behavior. Shared visual
  language is fine; shared assumptions about protocol/hardware behavior
  across product lines are not.

## 2. Discovery & device-communication discipline

- Discovery performance matters. Discovery (`/api/scan`) is an
  operator-triggered, IP-range-based parallel scan (no mDNS/SSDP) — see
  PROJECT.md §5. Any change here must keep scan latency and worker-pool
  behavior in mind; don't add serial/blocking calls to a path that scans up
  to thousands of hosts in parallel.
- Do not add unnecessary device queries, polling, retries, or authentication
  attempts. Every additional round-trip is multiplied across the device
  count during scan/refresh operations.
- Device-mutating operations should use appropriate verification/readback
  where supported by the actual LumaStream architecture (e.g. the existing
  "if needed" idempotent video-wall setters that only push a value when the
  device's reported value differs, and the CS31 upgrade monitor's
  post-reboot version confirmation). Follow that existing pattern for new
  mutating operations rather than firing-and-forgetting.
- Preserve existing rollback/error handling (e.g. Password Sync's
  full-success-only config update, the CS31 upgrade's multi-endpoint
  fallback chain).

## 3. Credentials & secrets

- Never expose passwords, tokens, credentials, or secrets in UI responses,
  logs, diagnostics, reports, screenshots, tests, URLs, or packaged content.
- The shared device admin password is stored and transmitted in plaintext
  (see PROJECT.md §"Authentication / Password Architecture"). `GET
  /api/config?include_password=1` returns it **only to the computer LumaSuite
  runs on** (v1.1.0): any network caller gets 403, and no remote response may
  carry a device password. Any further change to credential handling is itself
  a security-sensitive change and must be called out explicitly and confirmed
  with the user before being made.
- RTMP stream keys and per-device RTMP `authpassword` fields are likewise
  stored in plaintext in `producer_state.json` / `units_cache.json`. Treat
  these the same way as device passwords.
- Runtime configuration, credentials, logs, device caches, and
  developer-local paths must not be committed or packaged. This repository's
  `.gitignore` already excludes `producer_config.json`, `producer_state.json`,
  `producer2_state.json`, `units_cache.json`, `video_walls.json`,
  `ui/localdata/producer_cache.json`, and the entire `firmware/` directory —
  keep it that way, and extend it if new runtime-data files are introduced.
  Firmware images are local/operator content, not shipped with the packaged
  executable — do not add them to `datas`/`--add-data` in the build without
  an explicit, separate decision to do so.

## 4. Testing & hardware safety

- Automated tests must not unintentionally communicate with real hardware.
  **Since v1.1.0 the fence lives in `tests/_fence.py`** and is installed by
  `tests/__init__.py` and `tests/_support.py`, so it is present however a test
  is run; it also pre-seeds the scratch data folder, because an unseeded one
  has `lumaserver` migrate the developer's real runtime files into it. New
  tests import `_support` first. The older suites keep their own inline fence.
  There is still no injectable device-client abstraction: `lumaserver.py` calls `requests`, `socket`, and
  `websocket-client` directly inline inside route handlers; there is no
  injectable device-client abstraction. Any new automated Python test that
  exercises a route touching device I/O **must** mock/monkeypatch at the
  function level (`_ws_call`, `_ws_call_auth`, `_ws_call_switcher`,
  `_tcp_probe`, `requests.post`/`requests.get`, `socket.socket`/
  `socket.create_connection`, `websocket.create_connection`) or must not be
  automated at all until such a seam exists. Do not assume "it's Flask, the
  test client is safe" — the test client will happily trigger real outbound
  network calls to whatever IP a test passes in.
- Update tests when behavior intentionally changes.
- As of the 2026-09-19 Video Wall UX pass there are twelve automated
  test files, all wired into `.github/workflows/build-cross-platform.yml`:
  five Node files (`tests/producer_logo_position.test.js`,
  `tests/video_wall_model.test.js`, `tests/video_wall_preview_refresh.test.js`,
  `tests/no_native_dialogs.test.js`,
  `tests/shell_family_consistency.test.js` — run with
  `node tests/<file>.test.js`) and seven Python files
  (`tests/test_password_fallback.py`, `tests/test_credential_recovery.py`,
  `tests/test_video_wall_regions.py`, `tests/test_video_wall_detection.py`,
  `tests/test_video_wall_zones.py`, `tests/test_video_wall_candidates.py`,
  `tests/test_hall_firmware_status.py` — run with `python tests/<file>.py`).
  This list drifted out of sync with the actual CI workflow at least once
  before (a newly-added Python test file existed in the repo but was never
  added to the workflow's test step) — when adding a new automated test
  file, add it to the CI workflow step in the SAME change, not as a
  follow-up. `tests/test_password_fallback.py` establishes the
  hardware-fence pattern the rule above calls for: it
  arms spies on every real transport (`socket.socket`/
  `socket.create_connection`, `requests.post`/`.get`,
  `websocket.create_connection`) that raise if actually invoked, before
  importing `lumaserver`, then mocks `lumaserver._ws_call` per test.
  **Follow this same pattern for any new Python test that exercises a
  route touching device I/O** — copy the fence setup rather than
  re-deriving it, and mock `_ws_call`/`_ws_call_auth`, never anything
  closer to the real socket.
- **Before manually running the app locally for testing** (not the
  automated tests above — this is about interactive/manual verification),
  always point `LUMA_DATA_DIR` at a throwaway directory pre-seeded with
  an empty `units_cache.json` (`[]`) before first start. Starting with a
  *fresh but unseeded* `LUMA_DATA_DIR` still migrates whatever
  `units_cache.json`/`video_walls.json`/`producer_config.json` sit beside
  `lumaserver.py` in the repo root — i.e. real local network/device data
  from prior real use — into the new directory, and loading the Device
  Manager page against it then triggers real background verification
  calls to those real device IPs. This was hit repeatedly during manual
  testing in the family-alignment pass; see PROJECT.md §7 for the full
  mechanism.

## 5. UI/UX & documentation

- Do not begin a UI redesign, implement new product features, change device
  behavior, change firmware behavior, or change Password Sync as part of
  routine/governance work — those require an explicit, separate decision.
- As of the 2026-09-18 Hall Research application-family alignment pass,
  LumaSuite has a real shared design-token system
  (`ui/family.css`, `ui/theme-boot.js`, `ui/dialogs.js`,
  `ui/settings.js` — see PROJECT.md §5). Build new UI on these: use the
  semantic tokens (`--surface`, `--text`, `--action-primary`,
  `--state-*`, etc.) and the shared button/pill/notice/modal/toast/
  confirm classes rather than introducing new hardcoded colors or a new
  per-page confirmation dialog. Use `window.toast()`/`window.lumaConfirm()`
  for all new feedback and confirmations — never a native
  `alert()`/`confirm()`/`prompt()` (enforced by
  `tests/no_native_dialogs.test.js`).
- Review/update the User Guide (`ui/manual.html`) for every release/build
  intended for publication, so it continues to describe actual behavior.
- Documentation must describe actual LumaSuite behavior, not aspirational or
  copied-from-elsewhere behavior. Note: `README.md` currently has known
  drift from the real implementation (wrong default port, several
  documented API endpoints that don't exist under those names/paths — see
  PROJECT.md). Don't propagate those inaccuracies into new docs; fix drift
  you find, but as its own visible change, not folded silently into an
  unrelated commit.

## 6. Release checklist

Before publication, verify:
- Version is bumped and consistent across `README.md`, `CHANGELOG.md`,
  `RELEASE_NOTES.md`, `lumaserver.py` (`__version__`), and
  `app_launcher.py` (`__version__`).
- Documentation matches actual behavior.
- Licensing state is what's intended (currently **unset** — see PROJECT.md;
  do not add a license file speculatively, that's a business decision, not
  an engineering one).
- Packaging is correct (`build_executable.py` is the real build path;
  `LumaSuite.spec` is a stale, unused vestige — see PROJECT.md — don't edit
  it under the assumption it affects the shipped build).
- No credentials, logs, device caches, or developer-local paths got
  committed or packaged.
- Clean-start behavior works (fresh `LUMA_DATA_DIR`, no legacy JSON files to
  migrate).

## 7. Git & repo hygiene

- Avoid broad destructive Git operations such as `git checkout -- .` or
  `git reset --hard` when working changes exist.
- Preserve unrelated working-tree changes.
- Record newly **verified** hardware/protocol behavior and durable
  architectural decisions in `PROJECT.md`. Do not put temporary session
  notes, transient test counts, or one-off investigation results into
  `AGENTS.md` or `PROJECT.md` unless they represent durable project
  information.

## 8. Repository-specific gotchas discovered during this audit

- **`/api/producer2/state` is registered twice** with two different endpoint
  names (one pair of handlers reads/writes `producer_state.json`, the other
  reads/writes `producer2_state.json`); only the first-registered rule is
  reachable in practice, so the second handler and `producer2_state.json`
  are effectively dead. Don't build new functionality on top of the
  unreachable one; if this ever gets cleaned up, that's a deliberate fix to
  call out explicitly, not something to "helpfully" resolve as a drive-by.
- `flask-cors` was removed (it was never imported); cross-origin protection is
  a hand-rolled same-origin check (`enforce_request_security_guards`). Don't
  assume CORS headers are being set anywhere, and don't re-add the package.
- Authorization is a gate in front of every route (`luma_gate`, `luma_access`).
  **Every new route must be added to `luma_access.ROUTES`** or it is denied from
  the network, and `tests/test_access_fence.py` fails. The dev entrypoint and the
  launcher both bind loopback by default and must create their server through
  `luma_remote.serve_tagged`; a listener opened any other way would count every
  request as local.
- Firmware "is an update available" logic does not exist — the UI lists
  firmware files by filename/mtime only; version comparison happens only
  *after* upload, during CS31 reboot monitoring. Don't assume a
  pre-upload version-diff check exists anywhere.


## 9. Remote access, accounts and display groups (v1.1.0)

- Trust is the **listener that accepted the request**, never a header, `X-Forwarded-For` or the peer address.
- The server is the security boundary. Hiding a control is presentation; every route is classified, and a route that
  is not in `luma_access.ROUTES` is denied.
- Permissions, friendly names and group membership use a canonical **MAC**, never an address and never a name. A name
  is a label and decides nothing.
- A restricted response is built from **named fields** (`luma_projection`), tests assert the exact key sets, and no
  address, hostname, stream name or password may appear in one.
- A **display group** is not a video wall. It has no geometry, is never routed through `/api/video_wall/*`, and a
  group grant does not grant its members. Group routing is sequential, never parallel.
- A route is successful only after the display is **read back**; the cache follows the device. Audio and video are one
  operation: do not add a separate audio command without hardware evidence.
- JSON state files are written through `luma_store` (atomic, serialized, malformed files quarantined). Never
  truncate-in-place, and never hold two locks in opposite orders.
- Update `ui/manual.html` in the same change as any behaviour an operator can see, and say "User Guide reviewed -- no
  update required" when none is needed.
- After changing a security predicate, run `python tests/mutate_security.py` (preferably in a separate worktree). A
  survivor is investigated, not waved through. Validate on the pinned stack in `requirements-build-lock.txt`.
