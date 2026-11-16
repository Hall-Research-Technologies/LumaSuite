# LumaSuite v1.1.0 Release Notes
LumaSuite v1.1.0 adds optional Remote Access with accounts, so people can route sources to displays from a phone or another computer without being given the full LumaSuite interface. It also adds Display Groups, a Matrix for Users, saved Video Walls as User destinations, verified routing, an optional fixed local port, a rebuilt Device Manager table and third-party licence notices inside the application.

Nothing changes until an administrator turns Remote Access on. Existing devices, routes, video walls and settings are untouched.

## What's new

### Remote Access and accounts (off until you turn it on)

- Administrators sign in from another device and get the normal LumaSuite pages. **Users** get a routing page limited to the sources, displays, display groups and saved video walls they were assigned. Using LumaSuite on its own computer is unchanged whether Remote Access is on or off.
- Administrator and User accounts, throttled sign-in, server-side sessions, and optional **Direct Access** links for wall panels and lectern tablets (never for an administrator; treat the link like a password).
- Permissions are held by hardware (MAC) address, so a changed network address keeps its assignments. Friendly names are kept by LumaSuite and never written to a device.
- A **Remote Access** entry in the main navigation (while it is on) leads to Accounts, Device names, Display groups and Activity. The Activity log is in plain language and is kept for 30 days and 4 MB by default (configurable: 1-365 days, 1-64 MB). Passwords, tokens and links are never recorded.

### HTTP, HTTPS and certificates

- Remote Access starts on **HTTP**, labelled not encrypted, because a certificate is valid only for the address it was made for and a DHCP address can change. You confirm HTTP explicitly each time. **HTTPS** is one choice away and encrypts everything including passwords. Nothing downgrades an HTTPS setup by itself.
- Choose **Create one for this network** (LumaSuite makes a certificate), or **Use my own files**. A generated certificate is issued to this computer's Windows name by Hall Research for LumaSuite, covers the machine name and the chosen adapter's address, and is valid for three calendar years.
- Settings says whether the certificate is **active** (LumaSuite made it), **custom certificate active** (yours) or **certificate required** (missing, unreadable, expired, or made for another network). "Active" never means a browser trusts it: a certificate LumaSuite makes is self-signed, so the first visit shows a browser warning (compare the fingerprint shown in Settings). A certificate is never replaced automatically; **Regenerate certificate** is explicit, asks first, and shows the old and new fingerprints. A certificate you supplied is never replaced.

### Routing

- **A route is reported successful only after the display confirms it.**
- **Standard routing** (the User page): choose a source and any number of displays, display groups and saved video walls, then press Route once, or drag a source onto one destination. Displays are routed together in a small parallel batch, each confirmed on its own; an offline display is never written to, a failure does not stop the others, and nothing already routed is undone. The result says how many were routed ("3 of 4 displays routed").
- **Audio follows video** (on by default, per page visit): turn it off to change only the picture.
- **User Matrix**: sources across the top, destinations (saved video walls, display groups, displays) down the side, in Audio + Video, Video or Audio modes. Which interface each User gets is chosen per account.
- Administrators' **Matrix Routing** offers the same three modes, verified from the display and put back if the display does not hold the change.
- Previews: a Preview switch (remembered per device); pictures load only for visible cards, three at a time, refreshed every five seconds; Matrix previews appear when the pointer rests on a routing dot.

### Display Groups and saved Video Walls

- A **Display Group** is a named set of displays that all receive the same source (not a video wall). A group only changes its own members: before a restricted route writes to a display, LumaSuite confirms by hardware address that the unit at the recorded address is the member; a display that answers as a different unit, or will not say, is left alone and the result says why.
- A **saved Video Wall** can be given to a User as one destination, with the same identity check on every member.
- The User page remembers ticked targets, so choosing group B after routing group A routes both together; the Route button says how many groups and displays that is. Use **Clear selection** to start over.

### Video Wall and Production Studio

- Per-decoder **Video Wall Mode** (Enabled / Disabled / Unknown) read from the decoder and changed with read-back. **Apply to Entire Wall** verifies every member and reports complete, partial, failed or empty; one failing member no longer stops the others.
- Routing in Video Wall is immediate: select a source, then click a display, zone, row, column or the entire wall.
- **Production Studio logo** position, height and transparency now take effect on the unit as soon as you change them; a refusal is shown.

### Device Manager

- The table now matches OmniSuite's Device Info: frozen checkbox/IP/Hostname columns, a header that stays in view, a scroll bar above and below, numeric sorting of IP addresses and versions, and keyboard-operable headings.
- **Link speed** (green at exactly 1 Gbps, red otherwise) and the **connected switch and port** (hover or focus the MAC address).
- **Scan other networks**: the adapter's network plus any routed networks you list, remembered between runs, with every range size-checked before scanning.
- **Firmware status** (Up to Date / Available / Unknown with the reason) per model, checked shortly after start and every seven days. It only reports; nothing is downloaded or installed.
- A CS31 switcher is now read by the matrix refresh, so it no longer shows Offline while it is answering.

### Local application port

- Automatic by default (starts on 8090, takes the next free port, shows the address). In **Settings > Local Application Port** you can use a fixed port; **Save and Restart** really restarts LumaSuite. A busy fixed port is never silently replaced: the launcher offers Retry, Start on Alternate Port (this session only), Change Fixed Port and Exit.

### Backup and restore, Clear Units

- One passphrase-encrypted backup file, made and restored on the LumaSuite computer only. A restore shows what the file contains and what it will remove before changing anything, is authoritative for what the backup manages, signs out anyone whose account changed, and puts everything back if it cannot finish (and says so if it cannot).
- **Clear Units** and **Remove Selected** keep units that Users, Display Groups or saved video walls depend on while Remote Access is on, and tell you what is keeping each one. With Remote Access off they behave as before.

### Reliability and security

- Every network route is checked on the server; device passwords never leave the LumaSuite computer; request bodies that are not valid requests are refused with a plain message instead of being treated as instructions; the network listener answers only to its own names and limits request size; state files are written atomically and a damaged file is set aside rather than stopping LumaSuite.
- A scan that identifies a unit by its own hardware address no longer leaves a reachable unit marked offline.
- Updated components: Flask 3.1.3, Werkzeug 3.1.9, requests 2.34.2, cryptography 50.0.2 (48.0.1 on Intel Macs) and Pillow 13.0.0.

## Upgrading

Nothing changes until an administrator turns Remote Access on. LumaSuite's own listener now binds only to the local computer by default; set `LUMA_HOST` if you relied on the previous behaviour. Runtime data lives in the application data folder, never beside the executable.

## Supported platforms

| Platform | Download | Status |
|---|---|---|
| Windows x86-64 | `LumaSuite-v1.1.0-windows-x86_64.zip` (`LumaSuite.exe` with `LICENSE`, `THIRD_PARTY_NOTICES.md`, `REBUILD.md`, a `licenses` folder with the LGPL and GPL texts, and the pystray 0.19.5 source in `third_party`) | Tested on Windows 11. Physical LumaStream equipment was used to test this release's routing, Video Wall and Production Studio features in earlier builds of the release; the final build was tested with simulated devices. |
| macOS Intel | `LumaSuite-v1.1.0-macos-x86_64.zip` | Built by the release workflow. Not run on a Mac by Hall Research. |
| macOS Apple Silicon | `LumaSuite-v1.1.0-macos-arm64.zip` | As above. |
| Linux x86-64 | `LumaSuite-v1.1.0-linux-x86_64.tar.gz` | Built on Ubuntu only; needs a desktop session. Not used on a desktop by Hall Research. |

The macOS builds are one architecture each. Windows on Arm, 32-bit systems and Linux on Arm are not provided. The minimum macOS and Linux versions have not been established. None of the downloads is code signed, so Windows SmartScreen and macOS Gatekeeper ask you to confirm the first time.

## What LumaSuite contacts on the Internet

LumaSuite makes two read-only lookups and neither has an off switch: it asks Hall Research's public product pages which firmware is published (shortly after start and every seven days; six hours after a failed attempt), and it asks GitHub for the newest LumaSuite release when you open Settings or press Check Now. Neither installs anything, and no device names, passwords or settings are sent. Both servers can see this computer's public address, as with any web request.

## Known limitations

- The Remote Access block in Settings looks up network adapters and can take a few seconds to appear.
- There is no single-instance lock: a second copy starts on the next free port and shares the data folder. Run only one.
- Memory rises slowly under sustained use (about 2 MB an hour in a six-hour test); close and reopen a LumaSuite that has run for days.
- HTTP Remote Access is not encrypted; use it only on a network you trust.
- A remote administrator can use the device-changing pages on devices LumaSuite already knows; Users never can.
- A User's verified route to a saved Video Wall updates the wall's saved source to match what the displays now show.
- Whether a display's audio follows its video after a route is the display's own setting; LumaSuite reports it.
- Preview pictures from the encoder have been checked against test devices; they have not been verified on physical LumaStream encoders in a browser.
- LumaSuite is source-available under the Hall Research Technologies Source-Available Software License (free to use, including in business; selling it and distributing modified versions are not permitted). The licence, the third-party notices and licence texts, the pystray source and rebuild instructions are in the download (Settings > About > License).

## To validate on hardware

- Preview pictures from the encoder have been checked against test devices and are not yet verified on physical LumaStream encoders in a browser.
- Whether each display's audio follows its video after a route depends on that display's own setting; LumaSuite reports it rather than changing it.
- The macOS and Linux packages have not been run on those operating systems by Hall Research.

## Build and automation

The release workflow validates Python and JavaScript, including the security suites, before building packages for Windows x64, Linux x64, macOS Intel and macOS Apple Silicon. A tag is accepted only if it is exactly `v` plus the application version and sits on the release branch; the workflow then creates a draft release with a `SHA256SUMS.txt`, and a person publishes it after inspecting it. A manual run or a push to `main` builds without publishing.
