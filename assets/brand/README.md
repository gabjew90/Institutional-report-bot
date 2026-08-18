# Brand assets

## omnibeta-logo.png (REQUIRED for the daily calendar graphic)

Drop the Omnibeta logo here as `omnibeta-logo.png`.

Preferred: the MARK ALONE (the lotus/compass mandala) on a transparent
background, square, at least 512x512. The calendar renderer composites it
into the sheet header and draws the OMNIBETA wordmark itself as text, so a
version that already includes the wordmark will double it up.

Acceptable fallback: the full square lockup (mark + wordmark on the green
ground). The renderer will letterbox it and skip drawing its own wordmark.

SVG is welcome as `omnibeta-logo.svg` alongside the PNG — it is not used
today (the renderer is Pillow-based and rasterizes) but keeping the vector
here means a future move to SVG rendering costs nothing.

If neither file is present the renderer logs a warning and ships the sheet
with the wordmark only. The graphic never fails to post over a missing
logo.
