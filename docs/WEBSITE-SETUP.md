# Web embed — publishing pipeline + drop-in snippet

> **The production daily-pulse page is NOT in this repo.** It lives at <https://gabjew90.github.io/Stock-market-dashboard/pulse/>, published from [gabjew90/Stock-market-dashboard](https://github.com/gabjew90/Stock-market-dashboard). If you want to change how the page LOOKS (layout, # pulses per page, pagination, colors, fonts, nav), work in that repo. The page-styling files that used to live under `docs/` in this repo have been removed — they were a starter/demo that's no longer needed now that the production page exists elsewhere.
>
> This doc describes one thing only: **the publishing pipeline in this repo that produces the fragments + JSON the production page (and any other host site) consumes.**

## What the publishing pipeline produces

After each pulse archives, the bridge worker (Railway service, see
[`publish_web_fragment_job`](../github_bridge/jobs.py)) writes to the
`pulse-data` branch:

```
pulse-output/web/
├── fragments/<ts>.html       # per-pulse headless HTML — only the newest pulse gets one
├── latest-fragment.html      # copy of the newest fragment (convenience pointer)
├── latest.json               # metadata of the newest pulse (title, date, pdf_count, themes, URLs)
└── archive.json              # index of the last 60 pulses (full metadata for the newest + any cached, stubs for the rest)
```

Everything is served via `raw.githubusercontent.com`, which sets
`Access-Control-Allow-Origin: *` (CORS-friendly for `fetch()`) and has
~5-minute Fastly caching. No CDN proxy in the middle.

The fragments are `<article class="pulse">...</article>` markup with semantic
class hooks (`.pulse h2.recap`, `.pulse h2.insights`, `.pulse h2.watch`,
`.pulse-masthead`, `.pulse em` for punchlines, `.pulse .cashtag` for $TICKER
pills, etc.). No inline styles — host sites provide ALL the styling via CSS
targeting those classes.

Class structure is a **stable contract**. See
[scripts/pulse_dashboard.py :: render_pulse_fragment()](../scripts/pulse_dashboard.py)
for the canonical definition. Don't rename `.pulse`, `.pulse h2.recap`,
`.pulse-masthead`, `.cashtag`, `.recap-body`, `.insights-body`, `.watch-body`,
etc. without coordinating with the Stock-market-dashboard repo (their CSS
targets these exact selectors).

## Backfill policy

`publish_web_fragment_job` only renders the HTML fragment for the **single most
recent** archived pulse. Older entries in `archive.json` are minimal stubs
(`{ts, filename, archive_url}` only, no `fragment_url`). Host sites filter
stubs out, so historical pulses don't appear in the embed view until a
fragment exists for them.

To populate older pulses, a one-shot backfill script is needed (none exists
yet — ask if you want one).

## Embedding the pulse on any other site

Drop this on any HTML page (Substack custom-HTML block, Ghost code injection,
WordPress widget, static site, etc.):

```html
<!-- 1. Container for the rendered pulse -->
<div id="pulse-pane"><p>Loading&hellip;</p></div>

<!-- 2. Your CSS that styles the .pulse classes — see the production page
        at gabjew90/Stock-market-dashboard/web/pulse.html for a reference
        stylesheet you can copy as a starting point. -->

<!-- 3. Fetch + inject the latest fragment -->
<script>
  fetch('https://raw.githubusercontent.com/gabjew90/Institutional-report-bot/pulse-data/pulse-output/web/latest-fragment.html')
    .then(r => r.text())
    .then(html => document.getElementById('pulse-pane').innerHTML = html);
</script>
```

For a **weekly view** that shows all of this week's pulses (newest on top,
prev/next-week pagination), see how the production page does it:
[`web/pulse.html` in Stock-market-dashboard](https://github.com/gabjew90/Stock-market-dashboard/blob/main/web/pulse.html).
That file is the reference implementation. Fetch `archive.json`, filter by
ISO week, and inject each entry's fragment.

The fragment carries no inline styles — your host site's CSS controls every
visual decision.
