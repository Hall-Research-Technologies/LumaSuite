# Changelog

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

