# Website setup

> **Important: the production daily-pulse page is NOT in this repo.** It lives at <https://gabjew90.github.io/Stock-market-dashboard/pulse/>, published from [gabjew90/Stock-market-dashboard](https://github.com/gabjew90/Stock-market-dashboard). If you want to change how the page LOOKS (layout, # pulses per page, pagination, colors, fonts, nav), work in that repo instead — not this one.
>
> **This doc describes two things, both in this repo:**
> 1. The publishing pipeline (`publish_web_fragment_job` in the bridge worker) that produces the fragments + JSON the production page consumes. **This is the only piece that matters for the live site.**
> 2. A small **starter dashboard** under [docs/](.) you can optionally enable as a GitHub Pages site at `gabjew90.github.io/Institutional-report-bot/` if you want a second standalone view, or as a reference implementation for embedding the pulse elsewhere. It's a sibling of the production page, not a replacement.

## What the publishing pipeline produces

After each pulse archives, the bridge worker (Railway service, see
[`publish_web_fragment_job`](../github_bridge/jobs.py)) writes to the
`pulse-data` branch:

- `pulse-output/web/fragments/<ts>.html` — per-pulse headless HTML (`<article class="pulse">...</article>`, no inline styles)
- `pulse-output/web/latest-fragment.html` — copy of the newest fragment (convenience pointer)
- `pulse-output/web/latest.json` — metadata of the newest pulse (title, date, pdf_count, theme list, URLs)
- `pulse-output/web/archive.json` — index of the last 60 pulses (full metadata for the newest one + any cached older ones; stubs `{ts, filename, archive_url}` for everything else)

Everything is served via `raw.githubusercontent.com`, which sets
`Access-Control-Allow-Origin: *` (CORS-friendly for `fetch()`) and has
~5-minute Fastly caching. No CDN proxy in the middle.

The published files are what the production dashboard at
`Stock-market-dashboard/pulse/` reads at runtime. Any host site that wants
to embed the pulse uses the same files via the same URLs.

## How the auto-update works

After each pulse archives, the bridge worker (Railway service, see
[github_bridge/jobs.py](../github_bridge/jobs.py) `publish_web_fragment_job`)
writes three files to the `pulse-data` branch:

```
pulse-output/web/
├── latest-fragment.html   # <article class="pulse">...</article> (no inline styles)
├── latest.json            # metadata: title, date, pdf_count, themes, URLs
└── archive.json           # index of the last 60 pulses (cap = WEB_ARCHIVE_LIMIT)
```

The site at `docs/index.html` fetches these three files on every page
load. Idempotent: the worker skips if `latest.json`'s timestamp already
matches the most recent archive, so it doesn't churn the branch.

## Enable Pages (one-time, 30 seconds)

1. Open repo settings: <https://github.com/gabjew90/Institutional-report-bot/settings/pages>
2. Under **Source**, pick **Deploy from a branch**.
3. Branch: pick whichever branch hosts your latest `docs/` directory
   (right now it's `claude/financial-pdf-discord-bot-mDpbk`; switch
   to `main` after a future merge).
4. Folder: **/docs**.
5. Click **Save**. First deploy takes ~1 minute; subsequent pushes
   redeploy within ~30 seconds.

The URL appears at the top of the Pages settings page once it's live.

## Files involved

| File | What |
|---|---|
| [docs/index.html](index.html) | The dashboard page. Fetch logic at the bottom. |
| [docs/style.css](style.css) | Site chrome — masthead, archive list, footer. |
| [docs/pulse.css](pulse.css) | Fragment styling — targets classes inside `<article class="pulse">`. Edit this to retheme the pulse without touching the site shell. |
| [docs/.nojekyll](.nojekyll) | Disables GitHub's Jekyll preprocessing. Otherwise it would try to parse markdown files in `/docs` as Jekyll posts. |
| [scripts/pulse_dashboard.py](../scripts/pulse_dashboard.py) | `render_pulse_fragment()` produces the headless HTML; `extract_pulse_metadata()` produces the JSON. |
| [github_bridge/jobs.py](../github_bridge/jobs.py) | `publish_web_fragment_job` — committed in the bridge worker, fires every ~60s, only does work when a new pulse has archived. |

## Customising

> These customizations apply to the **starter site** under `docs/` in this repo. They do NOT affect the production page at `Stock-market-dashboard/pulse/` — that page has its own HTML/CSS in the other repo and ignores everything here.

**Change site name + nav:** edit `<p class="site-brand">` and the
`<nav>` block at the top of [docs/index.html](index.html).

**Change site colors / typography:** edit [docs/style.css](style.css).
The `:root` variables at the top control most of the palette.

**Change pulse content styling** (section banner colors, cashtag pill,
typography of the pulse itself): edit [docs/pulse.css](pulse.css). All
selectors are scoped to `.pulse` so changes are isolated.

**Add panes alongside the pulse:** the site is intentionally a single
section right now. Add `<section>` blocks inside `<main>` for things
like "About," "Recent posts," etc. The pulse pane is just one card on
the page.

**Switch to a private repo:** GitHub Pages on the free tier requires
public repos. For a private repo, move the static site to
[Cloudflare Pages](https://pages.cloudflare.com/) or
[Netlify](https://www.netlify.com/), both of which support private
sources on their free tiers. The HTML/CSS files transfer verbatim;
only the deploy mechanism changes.

## Embedding the pulse elsewhere

This is the pattern the **production page in Stock-market-dashboard already uses** — it's a self-contained static HTML file that fetches the latest fragment at runtime. The same approach works on any site that lets you paste HTML+JS:

```html
<link rel="stylesheet" href="https://gabjew90.github.io/Institutional-report-bot/pulse.css">
<div id="pulse-pane"><p>Loading&hellip;</p></div>
<script>
  fetch('https://raw.githubusercontent.com/gabjew90/Institutional-report-bot/pulse-data/pulse-output/web/latest-fragment.html')
    .then(r => r.text())
    .then(html => document.getElementById('pulse-pane').innerHTML = html);
</script>
```

That works in Substack custom HTML blocks, Ghost code injection,
WordPress custom-HTML widgets, plain static sites, and any framework
that can fetch + inject. The fragment carries no inline styles — the
host site's `pulse.css` controls every visual decision.
