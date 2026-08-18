# LumaSuite v1.0.5 Release Notes

## Highlights

LumaSuite v1.0.5 adds the Video Wall workflow and tightens release readiness for field use. Installers can save wall layouts, export the full wall configuration JSON for a job site, import it later, and restore wall behavior after units are cleared or rescanned.

## What's New

- Video Wall page with saved wall layouts, decoder assignment, display source control, display/bezel settings, refresh-from-devices, and source sync.
- Video wall export downloads the entire wall config file. Import only accepts LumaSuite video wall config JSON and replaces the existing saved wall configurations after confirmation.
- Device Manager polling now remembers whether Poll Units was on or off when returning to the page.
- Configurator popups now show the app version and contain the Dark Mode control.
- Manual was rewritten as a how-to guide and linked from the configurator popup.
- Encoder/source display format was changed to use IP and stream name where applicable.

## Fixes

- Source sync skips decoders that are already subscribed to the selected source to avoid unnecessary video freezes.
- Video wall state is stored in `video_walls.json` with a guarded file kind so unrelated JSON imports are rejected.
- Adapter discovery includes additional OS-friendly paths for packaged builds.
- Production Studio gear icon was removed from the visible UI.
- Producer logo positioning and ticker behavior received focused fixes and test coverage.

## Build And Automation

The release workflow validates Python and JavaScript before building artifacts for:

- Windows x64
- Linux x64
- macOS Intel
- macOS Apple Silicon

The Windows executable was built locally at `dist/Windows/LumaSuite.exe`.

