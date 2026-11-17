# Changelog

## v1.1.1 - 2026-11-17

Corrects defects found in hardware acceptance of the unpublished v1.1.0 candidate. Remote Access, Display Groups,
video walls and settings are as described under v1.1.0 below; nothing changes until an administrator turns Remote Access on.

### Fixed

- Remote Access: the Remote Access navigation item follows the setting without a page refresh. Turning HTTPS on with no usable certificate now offers Create Certificate, Change to HTTP or Cancel instead of starting broken HTTPS.
- Each unit keeps the credential that last authenticated it and uses it for discovery, sign-in, upload, proxy, routing, Preview, walls and Production Studio. A stale credential is replaced when the unit refuses it.
- Routing: a route no longer locks the whole page. Several routes can be in flight; each display shows pending, verified or failed. Displays and Display Groups can be chosen together, and a display that is not a member of a chosen group is never written.
- Routing page: Clear selection forgets the chosen source, displays and groups and sends nothing to a device.
- Routing page: Unroute clears what the chosen displays or Display Groups are showing, with no source needed.
- Security test coverage: all 565 security mutation checks now run and are caught.

## v1.1.0 - 2026-11-11

Adds optional Remote Access with accounts, Display Groups, verified routing, a rebuilt Device Manager table,
a fixed local port on request, and a third-party notices bundle. Nothing changes until an administrator
turns Remote Access on; existing devices, routes, video walls and settings are untouched.

### Licence

- LumaSuite now ships with its licence (Hall Research Technologies Source-Available Software License, version 1.0, with section 11A for LGPL components). The release package contains `LICENSE`, `THIRD_PARTY_NOTICES.md`, the licence texts, the unmodified pystray 0.19.5 source and `REBUILD.md`, beside `LumaSuite.exe`.

### Remote Access and accounts

- Optional Remote Access, off by default. Administrators sign in from another device and get the normal
  LumaSuite pages; Users get a routing page limited to what they were assigned. Local use is unchanged.
- Administrator and User accounts with hashed passwords, throttled sign-in, server-side sessions, optional
  Direct Access links (never for an administrator) and a plain-language activity log kept 30 days and under
  4 MB by default (configurable: 1-365 days, 1-64 MB).
- Permissions by hardware (MAC) address over sources, displays, display groups and saved video walls; friendly
  names for sources and displays. A changed network address keeps its assignments.
- A Remote Access entry (administrators, while it is on) leads to Accounts, Device names, Display groups and
  Activity.
- HTTP or HTTPS. Settings starts on HTTP, labelled unencrypted, because a certificate generated for a DHCP
  address stops matching when the address changes; HTTPS is one choice away, plain HTTP needs an explicit
  confirmation, and there is no automatic fallback from HTTPS to HTTP.
- Certificates: the HTTPS choice describes the certificate that exists (certificate active, custom
  certificate active, certificate required), never "trusted". A generated certificate is issued to the
  Windows machine name by Hall Research for LumaSuite, names the machine and the selected adapter's address,
  and is valid for three calendar years. Existing certificates are never replaced automatically; Regenerate
  certificate is explicit, asks first and reports the old and new fingerprints. A supplied certificate is
  never replaced.
- The application's own listener binds loopback by default (`LUMA_HOST` overrides). The network listener
  limits request size, answers only to its own names, and sends browser security headers. Every route is
  classified and an unclassified route is denied. Device passwords are never returned to a remote caller.
- State files are written atomically and a malformed file is set aside.

### Routing

- A route is only reported successful once the display confirms it.
- Standard Routing (the User page): choose a source and any number of displays, display groups and saved
  video walls, then press Route once, or drag a source onto one destination. Displays are routed in a small
  parallel batch, each locked, routed and read back on its own. Offline displays are never written, one
  failure does not stop the others, nothing is rolled back, and the result reports "N of M displays routed".
- Audio follows video: a switch beside Preview, on by default; off changes the picture only.
- User Matrix: sources against destinations (saved video walls, display groups, displays) in Audio + Video,
  Video or Audio. Which interface a User gets is chosen per account.
- Matrix Routing for administrators offers Audio + Video, Video and Audio, verified from the display and put
  back if the display does not hold the change.
- Previews: a Preview switch (remembered per device); only visible cards are fetched, three at a time,
  refreshed every five seconds; Matrix previews appear when the pointer rests on a routing dot.

### Display Groups and saved Video Walls

- Display Groups: a named set of displays that all receive the same source (not a video wall). A group only
  changes its own members: before a restricted route writes to a display, LumaSuite confirms by hardware
  address that the unit there is the member; a different unit, or one that will not say, is not written to.
- A scan never gives one unit's identity to another, and a unit a scan has identified by its own hardware
  address is no longer refused as offline until the next refresh.
- Saved Video Walls can be assigned to a User as one destination, with the same identity confirmation.
- The User page remembers ticked targets, so choosing group B after routing group A routes both together;
  the Route button says how many groups and displays that is.

### Video Wall

- Video Wall Mode per decoder (Enabled / Disabled / Unknown) read from the decoder and changed with read-back.
- Apply to Entire Wall verifies every member; the result is complete, partial, failed or empty and names each
  failure. One failing member no longer stops the others.
- Routing in Video Wall is immediate: select a source, then click a display, zone, row, column or the entire
  wall.

### Device Manager

- Matches OmniSuite's Device Info: frozen checkbox/IP/Hostname columns, a header that stays in view, a scroll
  bar above and below, numeric sorting, keyboard-operable headings, Blink, device counts.
- Link speed (green at 1 Gbps, red otherwise) and the connected switch and port (hover or focus the MAC).
- Routed scanning: the adapter's network and any routed networks you list are scanned together, remembered, and
  size-checked before scanning (at most a /20 per range, 8,192 addresses and 32 ranges per scan).
- Firmware status (Up to Date / Available / Unknown) per model, checked shortly after start and every seven
  days (six hours after a failed attempt). It only reports.
- A CS31 switcher is read by the matrix refresh and no longer shows Offline while it answers.

### Local application port

- Automatic by default (starts on 8090, takes the next free port). Settings > Local Application Port adds a
  fixed port; Save and Restart restarts LumaSuite. A busy fixed port is never silently replaced; the launcher
  offers Retry, Start on Alternate Port (this session only), Change Fixed Port and Exit. A damaged setting
  never stops LumaSuite starting; it is not part of a backup.

### Production Studio

- Logo position, height and transparency take effect on the unit as soon as they are changed; a refusal or a
  value the unit did not keep is shown. Limits are those the unit accepts: transparency 1-255, height 1-100,
  horizontal 0-100, vertical never less than the height.

### Backup and restore, Clear Units

- Encrypted backup and restore (local only) with a pre-restore snapshot and rollback. A restore is
  authoritative for what the backup managed, signs out anyone whose account changed, and says plainly when a
  rollback is incomplete.
- Restore no longer overwrites a concurrent scan or poll, and a failed inventory save during a restore is
  reported, not hidden.
- Clear Units and Remove Selected keep units that Users, groups or saved walls depend on while Remote Access
  is on, and show what is keeping each one.

### Request handling

- A request that is not valid is refused with a plain message on every route that takes one, and never acts as
  an instruction. The Remote Access port must be 1024-65535. A network address is never read as a hardware
  address. Logs record field names, never request bodies.
- The update check offers only the download that matches this computer's operating system and architecture.

### Dependencies and notices

- Flask 3.1.3, Werkzeug 3.1.9, requests 2.34.2, cryptography 50.0.2 (48.0.1 on Intel macOS), Pillow 13.0.0;
  the build lock pins the whole dependency graph. Windows builds use CPython 3.12.16 with OpenSSL 3.5.9.
- THIRD_PARTY_NOTICES.md and every component's licence text are packaged and served at Settings > About >
  License.

### Known behaviours (documented, not changed)

- The generated private key's file permissions are the data folder's (owner-only where the system supports
  it); it is never returned, logged or packaged. Keep the folder private.
- A remote administrator can use the device-changing pages on known devices. Users cannot.
- A restricted User's verified route to a saved Video Wall updates the saved wall's source.

## v1.0.8 - 2026-08-27

- Moved LumaSuite runtime JSON storage to the user's application data directory on Windows, macOS, and Linux instead of writing beside the executable or into the source checkout.
- Added `LUMA_DATA_DIR` override support so deployments can explicitly choose a runtime data location.
- Updated the launcher to pass the shared LumaSuite data directory to the server before startup.
- Added startup migration for existing `units_cache.json`, `video_walls.json`, `producer_config.json`, `producer_state.json`, and `producer2_state.json` files from legacy app/source locations.
- Preserved existing user-data files during migration unless a legacy file is newer, in which case the existing user-data file is backed up before the newer file is copied forward.

## v1.0.7 - 2026-08-24

- Added Video Wall layout preview overlays using decoder thumbnails, with a Preview toggle and transparency slider in the Wall Layout header.
- Refreshed enabled Video Wall previews with a buffered batch loader so existing images stay visible until the next set is ready.
- Added a persisted Video Wall preview refresh slider from 2 to 10 seconds, defaulting to 10 seconds on first run.
- Kept preview images fully visible and proportional, including corrected preview rotation handling for rotated displays.
- Defaulted first-run Video Wall preview settings to full image strength with no dimming veil.
- Fixed Video Wall full-layout apply so missing or stale wall source selections do not produce display-error toasts when the wall matrix, bezel, and rotation apply successfully.
- Improved Video Wall apply error reporting to show the first display-level failure instead of a generic `Request failed: 200` message.
- Standardized Video Wall monitor tiles to a fixed 16:9 size across 1x1 through larger layouts, centering smaller walls and letting larger walls expand outward with horizontal scrolling.
- Moved the selected Video Wall monitor highlight inside the tile so the top row selection border is not clipped.

## v1.0.6 - 2026-08-24

- Added a running Device Manager dashboard clock that ticks locally and rebases from one stable source device instead of bouncing between devices during polling.
- Reduced NTP polling load by reusing refreshed scan data for dashboard time updates and removing a duplicate device clock read from the NTP profile endpoint.
- Fixed NTP cache accuracy so intentionally blank NTP server values clear stale cached server names.
- Added Video Wall rotation apply support and live rotation reporting for assigned displays.
- Improved Device Manager, Matrix, Producer, and Video Wall refresh behavior so background polling avoids interrupting active controls.
- Kept local IDE workspace files out of git by ignoring `*.code-workspace`.

## v1.0.5 - 2026-08-18

- Added Video Wall management with saved wall layouts, decoder assignment, source sync, display/bezel settings, import/export, and protected video-wall-only JSON import.
- Added a task-oriented LumaSuite manual linked from the configurator popup.
- Added shared confirmation dialogs so import/export and destructive prompts use the LumaSuite UI instead of browser debug popups.
- Moved Dark Mode into the configurator popup and added the app version to the popup.
- Preserved the Device Manager polling state across page navigation and refreshes.
- Improved adapter discovery for packaged and cross-platform builds by including psutil and fallback adapter detection paths.
- Updated cross-platform GitHub Actions automation for Windows, Linux, macOS Intel, and macOS Apple Silicon builds.
- Added validation steps for Python, JavaScript, and focused Video Wall/Producer behavior tests before release builds.
- Removed orphaned UI files and duplicate build workflow.
