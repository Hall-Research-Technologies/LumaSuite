# Changelog

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
