# LumaSuite v1.0.8 Release Notes

## Highlights

LumaSuite v1.0.8 standardizes runtime JSON storage across platforms so scans, cached units, Video Wall layouts, producer settings, and state files live in the user's application data folder instead of beside the executable.

## What's New

- Runtime files now resolve to `%LOCALAPPDATA%\LumaSuite` on Windows, `~/Library/Application Support/LumaSuite` on macOS, and `~/.lumasuite` on Linux.
- `LUMA_DATA_DIR` can override the runtime data folder for custom deployments.
- The launcher now sets the shared data directory before importing the server, keeping packaged launches and direct server launches aligned.

## Fixes

- Existing legacy JSON files are migrated into the new user-data folder on startup.
- Existing user-data files are not overwritten by older legacy files.
- If a newer legacy file is migrated over an existing user-data file, the previous user-data file is preserved with a timestamped backup.

## Build And Automation

The release workflow validates Python and JavaScript before building artifacts for:

- Windows x64
- Linux x64
- macOS Intel
- macOS Apple Silicon

Local release validation was run before tagging this release.
