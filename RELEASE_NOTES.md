# LumaSuite v1.0.6 Release Notes

## Highlights

LumaSuite v1.0.6 tightens live Device Manager behavior and cache accuracy. The dashboard clock now runs locally from one stable source device, polling rebases that clock without jumping between units, and NTP cache updates correctly clear stale server names when a device reports an intentionally blank server.

## What's New

- Device Date/Time now ticks locally in the browser and rebases from the selected device, or from one sticky fallback device when no row is selected.
- Video Wall rotation can be applied to assigned displays and checked during refresh/reporting.
- Matrix, Producer, Video Wall, and Device Manager refresh paths defer table or panel rerenders while the user is actively editing controls.
- Matrix and Producer device lists now reflect the shared backend cache without hiding devices solely because another page marked them offline.

## Fixes

- Prevented the dashboard clock from jumping forward and backward by locking it to a single source IP unless selection changes.
- Removed an extra `SystemCurrentTime.Get` call from `/api/ntp_profile`.
- Allowed blank `ntp_server` and `ntpserver` scan values to overwrite stale cached values.
- Ignored local `*.code-workspace` IDE files so they are not accidentally included in release commits.

## Build And Automation

The release workflow validates Python and JavaScript before building artifacts for:

- Windows x64
- Linux x64
- macOS Intel
- macOS Apple Silicon

Local release validation was run before tagging this release.
