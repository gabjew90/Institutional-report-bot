# Website setup (GitHub Pages, ~3 min)

Turn-key instructions to get the daily pulse rendered on a public URL,
auto-updated when each weekday's pulse fires.

## What you get

A single-page dashboard at `https://gabjew90.github.io/Institutional-report-bot/`
showing:
- **A live pane** with today's pulse (fetches the headless HTML fragment
  the bridge worker publishes after every successful pulse archive)
- **An archive list** of the last 14 pulses (linked to their raw markdown
  on the `pulse-data` branch)
- A footer with attribution

Everything is fetched client-side from the `pulse-data` branch via
[raw.githack.com](https://raw.githack.com/) — same CDN-proxy approach
as the existing dashboard snapshot. No server, no build step. Push a
new commit to this branch and the change is live on Pages within
~1 minute.

## How the auto-update works

After each pulse archives, the bridge worker (Railway service, see
[github_bridge/jobs.py](../github_bridge/jobs.py) `publish_web_fragment_job`)
writes three files to the `pulse-data` branch:

```
pulse-output/web/
├── latest-fragment.html   # <article class="pulse">...</article> (no inline styles)
├── latest.json            # metadata: title, date, pdf_count, themes, URLs
└── archive.json           # index of the last 30 pulses
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

The same `pulse.css` + fetch pattern works on any site. Drop this on
any page:

```html
<link rel="stylesheet" href="https://gabjew90.github.io/Institutional-report-bot/pulse.css">
<div id="pulse-pane"><p>Loading&hellip;</p></div>
<script>
  fetch('https://raw.githack.com/gabjew90/Institutional-report-bot/pulse-data/pulse-output/web/latest-fragment.html')
    .then(r => r.text())
    .then(html => document.getElementById('pulse-pane').innerHTML = html);
</script>
```

That works in Substack custom HTML blocks, Ghost code injection,
WordPress custom-HTML widgets, plain static sites, and any framework
that can fetch + inject. The fragment carries no inline styles — the
host site's `pulse.css` controls every visual decision.
