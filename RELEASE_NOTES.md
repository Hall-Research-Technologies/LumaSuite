# LumaSuite v1.1.1 Release Notes

LumaSuite v1.1.1 is the first published release of the 1.1 series. It adds optional **Remote Access** with accounts, so people can route sources to displays from a phone or another computer without being given the full LumaSuite interface, together with Display Groups, verified routing, awareness of units on more than one network switch, and a set of responsiveness and reliability improvements. (Version 1.1.0 was an internal candidate and was never published; 1.1.1 includes everything in it.)

Nothing changes until an administrator turns Remote Access on. Existing devices, routes, video walls and settings are untouched, and using LumaSuite on its own computer works the same whether Remote Access is on or off.

The User Guide (Settings > User Guide) describes every feature below with screenshots.

## Highlights

- Remote Access with Administrator and User accounts, Direct Access links, HTTP or HTTPS, and a certificate workflow that never changes anything behind your back.
- Routing you can trust: a route is reported done only after the display confirms it, several routes can run at once, and Unroute and Clear selection are on the routing page.
- Display Groups: send one source to a named set of displays together.
- Device Manager tells you when units are plugged into more than one network switch.
- Clear Units and the Device Manager stay responsive with large unit lists.
- Windows (x86-64) and macOS (Apple Silicon) are the two supported platforms.

## Remote Access

- **Off until you turn it on.** Administrators open the normal LumaSuite pages from another device. Users get a simple routing page limited to the sources, displays, display groups and saved video walls they were given.
- **Accounts and permissions.** Permissions are held by each unit's hardware (MAC) address, so a unit that gets a new network address keeps its assignments. Each User is given Standard routing (cards and drag and drop), Matrix routing, or both. Friendly names are kept by LumaSuite and never written to a unit.
- **HTTP or HTTPS.** Remote Access starts on HTTP, clearly labelled as not encrypted, because a certificate is valid only for the address it was made for and a DHCP address can change. HTTPS is one choice away. Plain HTTP always needs an explicit confirmation, and there is no automatic fallback from HTTPS to HTTP.
- **Direct Access links** for wall panels and lectern tablets: a link that opens one User's routing page without a password. Treat the link like a password. There is one live link per account, regenerating it stops the old one at once, administrators cannot have one, and a link opens only the routing page.
- **Navigation follows the setting.** The Remote Access item appears when Remote Access is turned on and disappears when it is turned off, without refreshing the page.
- **Per-unit credentials.** The credential that works for a unit is remembered for that unit and used for everything LumaSuite does to it, until you change it or the unit refuses it. A site where some units use the primary password and others the fallback password works without changing any unit. Passwords are never shown.
- **Activity log** in plain language, kept 30 days and 4 MB by default (configurable). Passwords, tokens and links are never recorded.

## Certificate workflow

- Choosing HTTPS with no usable certificate asks what you want: **Create Certificate**, **Change to HTTP** or **Cancel**. Nothing is created or downgraded silently.
- A certificate LumaSuite creates names this computer and the chosen network's address and is valid for three years. Settings shows the address it is for, the days left and its fingerprint.
- **A certificate for another address is refused, not replaced.** If the chosen network's address no longer matches the certificate, LumaSuite will not start HTTPS with it and offers to regenerate it for this network or to use HTTP. It never issues a new certificate by itself, because that would change the fingerprint you checked. A certificate you supplied is never replaced.
- The first visit to a certificate LumaSuite created shows a browser warning, because the certificate is self-signed. Compare the fingerprint in the browser with the one in Settings before you continue.
- A fixed local application port is available in Settings, so the address on the LumaSuite computer stays the same between starts. A busy fixed port is never silently replaced.

## Routing improvements

- **A route is reported successful only after the display confirms it.** Matrix Routing offers Audio + Video, Video and Audio, and puts a display back as it was if it does not hold the change.
- **The page does not freeze while a route runs.** Routes to several displays or Display Groups run independently, and each shows pending, verified or failed. Displays that are not members of a chosen Display Group are never touched.
- **Standard routing:** choose a source and any number of displays, groups and saved walls, then press Route once, or drag a source onto one destination. The result says how many were routed, for example "3 of 4 displays routed".
- **Matrix routing for Users:** sources across the top, walls, groups and displays down the side.
- **Audio follows video** is on by default: a route moves the picture and the sound together. Turn it off to change the picture only.
- **Clear selection** forgets what you chose and changes no unit. **Unroute** needs no source: it sets the chosen displays, or every display of a chosen group, to no stream, and reads each one back. A video switcher is refused, and so is a video wall.

## Display Groups

A Display Group is a named set of displays that all receive the same source (it is not a video wall). A group changes only its own members: before a restricted route writes to a display, LumaSuite confirms by hardware address that the unit at the recorded address is the member, and a display that answers as a different unit is left alone with the reason reported. The routing page remembers what you ticked, so choosing a second group after routing the first routes both; the Route button says how many.

## Video Wall improvements

- Video Wall Mode (Enabled, Disabled or Unknown) is read from each decoder and changed with a read-back.
- **Apply to Entire Wall** verifies every display and reports complete, partial, failed or empty, naming any display that did not keep the setting. One failing display no longer stops the others.
- Routing to a wall is immediate: choose a source, then click a display, zone, row, column or the entire wall.
- A saved wall can be given to a User as one destination.
- The Video Wall status badge no longer holds up the page when a decoder does not answer.

## Production Studio improvements

Logo position, height and transparency take effect on the unit as soon as you change them. A value the unit refuses, or accepts but does not keep, is shown, and out-of-range values are corrected before they are sent.

## Preview improvements

- **Device Manager:** each unit's thumbnail switch is a checkbox in the Preview column; hover the word Preview to see the picture, click it to open a window that keeps refreshing.
- **Routing page:** a Preview switch (remembered per device) shows each source and display card's picture. Only visible cards are fetched, three at a time, and refreshed every five seconds. Pictures are fetched by LumaSuite, so a User's device never contacts a unit.
- **Matrix:** a source's picture appears while the pointer rests on a routing dot.

## Multiple-switch awareness

When the units you scan are plugged into more than one network switch, the Device Manager says so. The switch is identified by the chassis ID each unit reports, so two networks on one switch are still one switch. The switch most units use is the primary switch; a unit on any other switch is marked **Different switch** under its MAC address, and the notice **Multiple network switches detected** gives the counts and suggests checking the bandwidth between the switches if USB or AV-over-IP problems occur.

This is information, not a fault: routing, discovery and every unit are unchanged, nothing extra is requested from any unit, and a unit that reports no switch is never counted as being on another one. Dismiss the notice with the cross; it stays dismissed until you clear the unit list with Clear Units. A unit cabled through another unit is followed to its switch.

## Device Manager and Clear Units responsiveness

- **Clear Units is local and immediate.** It no longer waits for units that do not answer, however many there are. Work already under way for the cleared units is stopped or discarded, and a page that was open before the clear cannot bring a cleared unit back.
- The Device Manager's background reads (streaming status, reachability checks, the Poll Units refresh) run one round at a time, with limits, and units that do not answer are left alone for a growing interval. Actions you take are never queued behind them.
- **More than 32 units:** the background detail refresh now works for long lists. It reads in batches of at most 32 instead of failing.

## Large unit lists

LumaSuite is qualified for inventories of up to about 250 units. Inventories of 500 and 1,000 units were also tried with simulated units, mostly silent: they work, but refresh rounds are long and the Matrix Routing page opens slowly (a few seconds at 1,000 units). Clear Units finished in under a second at every size tried. See "How many units" in the User Guide.

## Polling and logging

- Background polling is bounded and single-flight: a slow unit never lets a new round stack on the one still running.
- The log is written at INFO level to a rotating file in the application data folder (at most 8 MB), much quieter than before for a site with many silent units (about fifteen times less log in a test with 200 units that did not answer). Set the environment variable `LUMA_LOG_LEVEL=DEBUG` for the full detail.

## Settings and interface consistency

- One Settings dialog on every page, arranged the same way as in OmniSuite: device sign-in, firmware, Production Studio folders, appearance, local application port, Remote Access, backup and restore, application information.
- Appearance offers light and dark themes, a Classic or Compact layout, colour presets and a light background colour.
- Settings no longer says an HTTPS certificate is missing when Remote Access uses HTTP by choice.
- Firmware availability (Up to Date, Available or Unknown, with the reason) is shown per model and per unit. It is checked shortly after start and every seven days, only reports, and never installs anything. With no Internet connection the last answer is kept.

## Backup and restore

One passphrase-encrypted backup file, made and restored on the LumaSuite computer only. A restore shows what the file holds, and what it will remove, before changing anything; it is applied as one step and every file is put back if any part fails. People whose account changed are signed out.

## Reliability and security

- Every network route is checked on the server, device passwords never leave the LumaSuite computer, and requests that are not valid are refused with a plain message.
- The network listener answers only to its own names and limits the size of a request; state files are written atomically, and a damaged file is set aside rather than stopping LumaSuite.
- A scan that identifies a unit by its own hardware address no longer leaves a reachable unit marked offline.
- Updated components: Flask 3.1.3, Werkzeug 3.1.9, requests 2.34.2, cryptography 50.0.2 and Pillow 13.0.0.

## Supported platforms

| Platform | Download | Status |
|---|---|---|
| Windows x86-64 | `LumaSuite-v1.1.1-windows-x86_64.zip` (`LumaSuite.exe` with `LICENSE`, `THIRD_PARTY_NOTICES.md`, `REBUILD.md`, a `licenses` folder and the pystray 0.19.5 source in `third_party`) | Tested on Windows 11. Physical LumaStream equipment was used to test routing, Video Wall and Production Studio features in earlier builds of the release; the final build was tested with simulated devices. |
| macOS Apple Silicon (arm64) | `LumaSuite-v1.1.1-macos-arm64.zip` | Built on a native arm64 runner; every file in the app is checked to be arm64. Not run on a Mac by Hall Research. |

Only these two platforms are distributed. There is no macOS Intel download, no universal app and no Linux download. Windows on Arm and 32-bit systems are not provided. The minimum macOS version has not been established. The Windows executable is not Authenticode-signed, so Windows SmartScreen asks you to confirm the first time. The macOS app carries an ad hoc signature only: it is not Developer ID signed and not notarised, so macOS Gatekeeper asks you to confirm the first time (right-click the app, then Open). A computer of another kind that checks for updates is told there is no supported update package for it.

## Upgrading

Nothing changes until an administrator turns Remote Access on. LumaSuite's own listener now binds only to the local computer by default; set `LUMA_HOST` if you relied on the previous behaviour. Runtime data lives in the application data folder, never beside the executable.

## What LumaSuite contacts on the Internet

LumaSuite makes two read-only lookups and neither has an off switch: it asks Hall Research's public product pages which firmware is published (shortly after start and every seven days; six hours after a failed attempt), and it asks GitHub for the newest LumaSuite release when you open Settings or press Check Now. Neither installs anything, and no device names, passwords or settings are sent. Both servers can see this computer's public address, as with any web request.

## Known limitations

- The Remote Access block in Settings looks up network adapters and can take a few seconds to appear.
- There is no single-instance lock: a second copy starts on the next free port and shares the data folder. Run only one.
- Memory rises slowly under sustained use (about 2 MB an hour in a six-hour test); close and reopen a LumaSuite that has run for days.
- HTTP Remote Access is not encrypted; use it only on a network you trust.
- A remote administrator can use the device-changing pages on units LumaSuite already knows; Users never can.
- A User's verified route to a saved Video Wall updates the wall's saved source to match what the displays now show.
- Whether a display's audio follows its video after a route is the display's own setting; LumaSuite reports it.
- LumaSuite is source-available under the Hall Research Technologies Source-Available Software License (free to use, including in business; selling it and distributing modified versions are not permitted). The licence, the third-party notices and licence texts, the pystray source and rebuild instructions are in the download (Settings > About > License).

## To validate on hardware

- Unroute was observed on one decoder with audio held on its own (audio follows video off): video and then audio set to none, read back, and restored. Unroute with audio following video is covered by simulated-device tests only.
- Preview pictures from the encoder have been checked against test devices and are not yet verified on physical LumaStream encoders in a browser.
- Whether each display's audio follows its video after a route depends on that display's own setting; LumaSuite reports it rather than changing it.
- The macOS (Apple Silicon) package has not been run on a Mac by Hall Research.
- Inventories larger than a normal installation were measured with simulated units only.

## Build and automation

The release workflow validates Python and JavaScript, including the security suites, before building packages for Windows x86-64 and macOS Apple Silicon (the only two distributed platforms). A tag is accepted only if it is exactly `v` plus the application version and sits on the release branch; the workflow then creates a draft release with a `SHA256SUMS.txt`, and a person publishes it after inspecting it. A manual run or a push to `main` builds without publishing.
