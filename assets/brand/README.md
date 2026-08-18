# Brand assets

## omnibeta-logo.png — source lockup (owner-supplied, 950x960)

The full square lockup: mark + wordmark on the radial green ground. Kept as
the source of truth. Not composited directly, because its ground is a
radial gradient and the calendar sheet's is flat — pasting the square
would show a visible disc.

## omnibeta-mark.png — the mark alone, transparent (derived, 275x287)

Extracted from the lockup: cropped to the mark's bounding box (measured at
350,233 - 597,492 in the source) and its ground keyed to alpha with a soft
falloff so anti-aliased stroke edges keep their gradient. Verified against
a magenta ground with no green halo. THIS is what the calendar renderer
composites into the header. If the source logo is ever replaced, re-derive
this file (the extraction is a ~20-line Pillow script; see the commit that
added it).

## ground.txt

The ground colour sampled directly beneath the mark in the source logo,
#273632. The calendar sheet uses this exact value so the mark sits on the
same green it was drawn on.

## Fallback behaviour

If omnibeta-mark.png is missing the renderer logs a warning once per boot
and ships the sheet with the wordmark only. A missing logo never blocks
the calendar from posting.
