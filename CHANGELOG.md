# Changelog

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
