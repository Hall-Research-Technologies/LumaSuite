# LumaSuite v1.0.7 Release Notes

## Highlights

LumaSuite v1.0.7 improves Video Wall setup by adding live decoder-thumbnail previews behind each monitor tile, stabilizing full-layout apply behavior, and making wall layouts easier to compare across sizes.

## What's New

- Wall Layout now has a Preview checkbox that places each decoder thumbnail behind the corresponding monitor controls.
- Preview transparency is adjustable from the Wall Layout header and persists locally.
- Enabled preview thumbnails refresh together every 7 seconds using one shared refresh tick.
- Monitor tiles now keep the same fixed 16:9 size across 1x1, 2x2, 5x5, and larger layouts, with smaller walls centered and larger walls expanding outward.

## Fixes

- Preview images remain fully visible and proportional inside monitor tiles.
- Rotated display previews are corrected so the design preview stays visually usable.
- Clicking or selecting a monitor no longer refreshes or changes the preview image.
- Full layout apply no longer reports display errors solely because the wall source is blank or stale when matrix, bezel, and rotation apply successfully.
- Video Wall apply failures now surface the first display-level error instead of a generic HTTP 200 failure message.

## Build And Automation

The release workflow validates Python and JavaScript before building artifacts for:

- Windows x64
- Linux x64
- macOS Intel
- macOS Apple Silicon

Local release validation was run before tagging this release.
