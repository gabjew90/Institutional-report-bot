"""Generate a local HTML dashboard showing the latest archived pulse.

Single static HTML file you open in a browser. No server, no install
beyond what scripts/pulse_pdf.py already pulled in (the `markdown` package).

By default, fetches the most recent archived pulse from `pulse-output/archive/`
on the `pulse-data` branch via the GitHub API (with the GH_TOKEN env var if set,
unauthenticated otherwise), renders it to a styled HTML file, and opens it in
the system default browser.

Usage:
    python scripts/pulse_dashboard.py
        # fetch latest from GitHub, write to pulse-dashboard.html, open browser

    python scripts/pulse_dashboard.py --from-file path/to/pulse.md
        # render a local markdown file (skips the GitHub fetch)

    python scripts/pulse_dashboard.py --out my-dashboard.html
        # write to a custom path

    python scripts/pulse_dashboard.py --no-open
        # don't auto-open the browser
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
import webbrowser
from datetime import datetime
from pathlib import Path

import markdown


REPO = "gabjew90/Institutional-report-bot"
BRANCH = "pulse-data"
ARCHIVE_DIR = "pulse-output/archive"


# === Web-optimized CSS ===
# Same visual identity as the PDF (section colors, masthead, cashtag pill)
# but tuned for screen reading: responsive max-width, larger body type, no
# @page rules, a sticky section nav on the right at >=900px viewports.
CSS = """
:root {
  --color-recap:    #c9a227;
  --color-insights: #2956a3;
  --color-watch:    #d97706;
  --color-signal:   #138d75;
  --color-changed:  #1abc9c;
  --color-board:    #8e44ad;
  --color-text:     #1a1a1a;
  --color-muted:    #666;
  --color-soft:     #f7f5f0;
  --color-card:     #ffffff;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--color-soft);
  color: var(--color-text);
  font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
  font-size: 16px;
  line-height: 1.55;
}

.page {
  max-width: 820px;
  margin: 0 auto;
  padding: 36px 48px 80px;
  background: var(--color-card);
  min-height: 100vh;
  box-shadow: 0 0 24px rgba(0,0,0,0.04);
}

@media (max-width: 720px) {
  .page { padding: 24px 18px 60px; }
  html, body { font-size: 15px; }
}

/* === MASTHEAD === */
.masthead {
  border-bottom: 3px solid var(--color-text);
  padding-bottom: 14px;
  margin-bottom: 24px;
}
.masthead .brand {
  font-size: 11px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--color-muted);
  margin-bottom: 6px;
}
.masthead h1 {
  font-size: 32px;
  line-height: 1.1;
  margin: 0 0 6px 0;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.masthead .dateline {
  font-size: 14px;
  color: var(--color-muted);
}
.masthead .meta {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-muted);
}

/* === SECTION BANNERS === */
h2 {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin: 28px 0 12px 0;
  padding: 10px 14px;
  border-radius: 4px;
  color: #ffffff;
}
h2.recap    { background: var(--color-recap); }
h2.insights { background: var(--color-insights); }
h2.watch    { background: var(--color-watch); }
h2.changed  { background: var(--color-changed); }
h2.signal   { background: var(--color-signal); }
h2.board    { background: var(--color-board); }

/* === THEME HEADERS === */
h3 {
  font-size: 18px;
  font-weight: 700;
  margin: 22px 0 6px 0;
  color: var(--color-text);
}
.insights-body h3 {
  border-left: 4px solid var(--color-insights);
  padding-left: 10px;
  margin-left: -4px;
}
.watch-body h3 {
  border-left: 4px solid var(--color-watch);
  padding-left: 10px;
  margin-left: -4px;
}

/* === BODY === */
p { margin: 10px 0; }
em {
  color: var(--color-insights);
  font-style: italic;
  font-weight: 500;
}
strong { color: var(--color-text); font-weight: 700; }
ul, ol { margin: 8px 0; padding-left: 26px; }
li { margin: 5px 0; }

/* === CASHTAGS === */
.cashtag {
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 14px;
  background: #f1efeb;
  padding: 1px 6px;
  border-radius: 3px;
  color: #1a3a6b;
  font-weight: 600;
  white-space: nowrap;
}

/* === FOOTER === */
.footer {
  margin-top: 36px;
  padding-top: 14px;
  border-top: 1px solid #e0ddd6;
  font-size: 12px;
  color: var(--color-muted);
  line-height: 1.5;
}
.footer .label {
  text-transform: uppercase;
  letter-spacing: 0.1em;
  font-weight: 600;
  color: #555;
}

/* === STICKY SECTION NAV (wide viewports only) === */
.section-nav {
  display: none;
}
@media (min-width: 1100px) {
  .section-nav {
    display: block;
    position: fixed;
    top: 36px;
    left: calc(50% + 440px);
    width: 180px;
    font-size: 12px;
  }
  .section-nav .label {
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--color-muted);
    margin-bottom: 8px;
    font-weight: 600;
  }
  .section-nav a {
    display: block;
    padding: 6px 0;
    color: var(--color-text);
    text-decoration: none;
    border-left: 3px solid transparent;
    padding-left: 10px;
    transition: border-color 0.15s, color 0.15s;
  }
  .section-nav a:hover {
    color: var(--color-insights);
    border-left-color: var(--color-insights);
  }
}

hr { border: 0; border-top: 1px dashed #ccc; margin: 18px 0; }
"""


# === Markdown -> HTML helpers ===
# Inlined here (not imported from elsewhere) because pulse_dashboard.py is
# now the sole consumer. Earlier these lived in scripts/pulse_pdf.py and
# were shared with a PDF renderer; the PDF path was retired in favor of
# litterbox-hosted HTML so pulse_pdf.py was deleted along with its sample
# artifact.

def _strip_frontmatter(md: str) -> tuple[dict, str]:
    """Strip YAML-ish frontmatter block. Return (meta, body).

    Tolerant of a leading UTF-8 BOM (PowerShell's `Out-File -Encoding utf8`
    adds one) — without this the `---` sniff fails and the frontmatter
    silently falls through as body content, leaving meta empty.
    """
    if md.startswith("﻿"):
        md = md[1:]
    if not md.startswith("---"):
        return {}, md
    end = md.find("\n---", 4)
    if end == -1:
        return {}, md
    block = md[3:end].strip()
    body = md[end + 4:].lstrip("\n")
    meta: dict = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
            meta[k] = int(v)
        else:
            meta[k] = v
    return meta, body


def _extract_title_and_body(md: str) -> tuple[str, str]:
    """Pull off a leading H1 if present. Returns (title, remaining_md)."""
    lines = md.lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        rest = "\n".join(lines[1:]).lstrip()
        return title, rest
    return "Daily Market Pulse", md


def _cashtag_to_span(text: str) -> str:
    """Wrap `$TICKER` cashtags in a styled span. Operates on rendered HTML."""
    return re.sub(
        r"(?<![A-Za-z0-9_>])(\$[A-Z]{1,6})\b",
        r'<span class="cashtag">\1</span>',
        text,
    )


def _wrap_sections(body_html: str) -> str:
    """Tag each H2 with a section class and wrap subsequent siblings in a
    matching div, so theme headers inside INSIGHTS/WATCH can pick up the
    colored left-border accent without selectors needing :has().
    """
    def _classify(m: re.Match) -> str:
        heading_text = re.sub(r"<.*?>", "", m.group(1)).lower()
        if "recap" in heading_text:
            cls = "recap"
        elif "changed" in heading_text:
            cls = "changed"      # WHAT CHANGED (format-overhaul Phase 1)
        elif "signal" in heading_text:
            cls = "signal"       # DESK SIGNAL BOARD (Phase 2) — MUST
            # precede the "board" check: "DESK SIGNAL BOARD" contains
            # "board" and would otherwise collide with TRADE BOARD.
        elif "board" in heading_text:
            cls = "board"        # TRADE BOARD (format-overhaul Phase 1)
        elif "main event" in heading_text:
            cls = "insights"     # THE MAIN EVENT (Phase 3) — render under
            # the stable .insights class so the dashboard repo needs no
            # change. "event" doesn't collide with any check above.
        elif "brief" in heading_text:
            cls = "insights briefs"  # BRIEFS (Phase 3) — styled as
            # insights, second class avoids the #pulse-insights id clash.
        elif "insights" in heading_text or "alpha" in heading_text:
            cls = "insights"
        elif "watch" in heading_text:
            cls = "watch"
        else:
            cls = ""
        return f'<h2 class="{cls}">{m.group(1)}</h2>'

    body_html = re.sub(r"<h2>(.*?)</h2>", _classify, body_html, flags=re.DOTALL)

    parts = re.split(r'(<h2 class="[^"]*">.*?</h2>)', body_html, flags=re.DOTALL)
    out: list[str] = []
    open_section: str | None = None
    for part in parts:
        # Capture the FIRST class token only — a heading may carry two
        # classes (e.g. "insights briefs"); the body div keys off the
        # first ("insights-body") so multi-class sections still wrap.
        m = re.match(r'<h2 class="(\w+)', part)
        if m:
            if open_section is not None:
                out.append("</div>")
            cls = m.group(1)
            out.append(part)
            open_section = cls
            out.append(f'<div class="{cls}-body">')
        else:
            out.append(part)
    if open_section is not None:
        out.append("</div>")
    return "".join(out)


# === Public renderers ===

def render_pulse_fragment(md: str) -> str:
    """Render the pulse as a headless HTML fragment for embedding into a host
    site. Output is just `<article class="pulse">...</article>` with semantic
    class hooks and id anchors; NO `<html>`, NO `<head>`, NO inline styles.
    All visual identity lives in the host site's CSS targeting the class
    structure below.

    Class structure (stable contract — the host site's CSS depends on it):
        .pulse                       # outer wrapper
        .pulse-masthead              # header block (brand + title + dateline)
          .pulse-brand               # small caps "Daily Market Pulse" label
          h1                         # pulse title (e.g. "Hot prices, Fed cornered")
          .pulse-dateline            # date + pdf count
        h2.recap         #pulse-recap
        .recap-body                  # children = <p>, <ul>, etc.
        h2.insights      #pulse-insights
        .insights-body
          h3                         # theme headers
          em                         # italicized punchlines
          ul / li                    # bullet rows
        h2.watch         #pulse-watch
        .watch-body
        .cashtag                     # `$TICKER` spans
    """
    meta, body_md = _strip_frontmatter(md)
    title, body_md = _extract_title_and_body(body_md)

    body_html = markdown.markdown(
        body_md,
        extensions=["extra", "sane_lists", "smarty"],
    )
    body_html = _cashtag_to_span(body_html)
    body_html = _wrap_sections(body_html)

    # Add id anchors to section h2s so the host site can deep-link.
    body_html = body_html.replace(
        '<h2 class="recap">', '<h2 class="recap" id="pulse-recap">'
    )
    body_html = body_html.replace(
        '<h2 class="insights">', '<h2 class="insights" id="pulse-insights">'
    )
    body_html = body_html.replace(
        '<h2 class="watch">', '<h2 class="watch" id="pulse-watch">'
    )
    body_html = body_html.replace(
        '<h2 class="changed">', '<h2 class="changed" id="pulse-changed">'
    )
    body_html = body_html.replace(
        '<h2 class="board">', '<h2 class="board" id="pulse-board">'
    )
    body_html = body_html.replace(
        '<h2 class="signal">', '<h2 class="signal" id="pulse-signal">'
    )

    pdf_count = meta.get("pdf_count")
    date_iso = (meta.get("dumped_at_utc") or "").rstrip("Z")[:10]
    dateline_parts: list[str] = []
    if date_iso:
        try:
            dateline_parts.append(
                datetime.fromisoformat(date_iso).strftime("%A, %B %d, %Y")
            )
        except ValueError:
            dateline_parts.append(date_iso)
    if pdf_count:
        dateline_parts.append(f"{pdf_count} research analyses")
    dateline = " &middot; ".join(dateline_parts)

    return (
        '<article class="pulse">\n'
        '  <header class="pulse-masthead">\n'
        '    <p class="pulse-brand">Daily Market Pulse</p>\n'
        f'    <h1>{title}</h1>\n'
        f'    <p class="pulse-dateline">{dateline}</p>\n'
        '  </header>\n'
        f'  {body_html}\n'
        '</article>\n'
    )


def extract_pulse_metadata(
    md: str,
    ts: str,
    source_filename: str | None = None,
    repo: str = "gabjew90/Institutional-report-bot",
    branch: str = "pulse-data",
) -> dict:
    """Extract metadata for the JSON sidecar that ships alongside the
    fragment. Host sites use this to render their own masthead / byline /
    archive list outside the fragment, so they're not coupled to the
    fragment's internal layout.
    """
    meta, body_md = _strip_frontmatter(md)
    title, body_md = _extract_title_and_body(body_md)

    # Pull INSIGHTS theme headers (### lines inside ## 2. INSIGHTS section).
    themes: list[str] = []
    in_insights = False
    for line in body_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_insights = "INSIGHTS" in stripped.upper() or "ALPHA" in stripped.upper()
            continue
        if in_insights and stripped.startswith("### "):
            themes.append(stripped[4:].strip())

    fname = source_filename or f"{ts}.md"
    # target_channels is set in the frontmatter ONLY for test fires (the
    # routine appends it when TARGET_CHANNELS env / file is non-empty).
    # Production scheduled fires have no target_channels line. The web
    # publisher uses this to filter test pulses out of the public archive.
    target_channels = str(meta.get("target_channels") or "").strip()
    return {
        "ts": ts,
        "title": title,
        "date_utc": (meta.get("dumped_at_utc") or "").rstrip("Z")[:10],
        "pdf_count": meta.get("pdf_count", 0),
        "input_tokens": meta.get("input_tokens", 0),
        "output_tokens": meta.get("output_tokens", 0),
        "themes": themes,
        "source_filename": fname,
        "archive_url": f"https://raw.githubusercontent.com/{repo}/{branch}/pulse-output/archive/{fname}",
        "fragment_url": f"https://raw.githubusercontent.com/{repo}/{branch}/pulse-output/web/latest-fragment.html",
        "target_channels": target_channels,
    }


def _gh_get(path: str, token: str | None) -> dict | list:
    """Call the GitHub Contents API. Returns parsed JSON."""
    url = f"https://api.github.com/repos/{REPO}/contents/{path}?ref={BRANCH}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _gh_raw(path: str, token: str | None) -> str:
    """Fetch a file's raw text via the Contents API (auth-friendly even
    when the repo is private — raw.githubusercontent.com requires the
    repo to be public OR you to use authenticated access via the API).
    """
    import base64
    meta = _gh_get(path, token)
    if not isinstance(meta, dict):
        raise RuntimeError(f"{path}: unexpected response shape")
    content = meta.get("content", "")
    return base64.b64decode(content).decode("utf-8")


def fetch_latest_pulse_markdown(token: str | None) -> tuple[str, str]:
    """Return (filename, markdown_content) for the latest archived pulse."""
    items = _gh_get(ARCHIVE_DIR, token)
    if not isinstance(items, list):
        raise RuntimeError(f"unexpected response listing {ARCHIVE_DIR}")
    md_files = [
        it for it in items
        if it.get("name", "").endswith(".md") and it.get("type") == "file"
    ]
    if not md_files:
        raise RuntimeError(f"no archived pulses found in {ARCHIVE_DIR}")
    # Filenames are timestamps so lexical sort = chronological. Take latest.
    latest = max(md_files, key=lambda it: it["name"])
    name = latest["name"]
    md = _gh_raw(f"{ARCHIVE_DIR}/{name}", token)
    return name, md


def _format_dateline(meta: dict, source_filename: str | None) -> str:
    """Build the "Friday, May 13, 2026 · pulse fired at 13:09 UTC" line."""
    dumped = (meta.get("dumped_at_utc") or "").rstrip("Z")[:19].replace("T", " ")
    today_label = datetime.now().strftime("%A, %B %d, %Y")
    if dumped:
        return f"{today_label} &middot; pulse synthesized {dumped} UTC"
    if source_filename:
        return f"{today_label} &middot; {source_filename}"
    return today_label


def render_dashboard_html(md: str, source_filename: str | None = None) -> str:
    meta, body_md = _strip_frontmatter(md)
    title, body_md = _extract_title_and_body(body_md)

    body_html = markdown.markdown(
        body_md,
        extensions=["extra", "sane_lists", "smarty"],
    )
    body_html = _cashtag_to_span(body_html)
    body_html = _wrap_sections(body_html)

    pdf_count = meta.get("pdf_count", "?")
    in_tok = meta.get("input_tokens", 0)
    out_tok = meta.get("output_tokens", 0)
    dateline = _format_dateline(meta, source_filename)
    rendered_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    footer = (
        '<div class="footer">'
        f'<span class="label">Coverage:</span> {pdf_count} research analyses'
        ' &middot; '
        f'<span class="label">Tokens:</span> {in_tok:,} in / {out_tok:,} out'
        ' &middot; '
        f'<span class="label">Dashboard rendered:</span> {rendered_at}'
        '<br/>'
        f'<span class="label">For:</span> self-directed US options &amp; crypto traders'
        ' &middot; Not investment advice. Internal note.'
        '</div>'
    )

    section_nav = (
        '<nav class="section-nav">'
        '<div class="label">Sections</div>'
        '<a href="#recap">Recap</a>'
        '<a href="#insights">Insights &amp; alpha</a>'
        '<a href="#watch">What to watch</a>'
        '</nav>'
    )

    # Inject id anchors on the h2s so the nav links work. We already wrapped
    # sections with class names via _wrap_sections; piggyback on the same
    # classes for the anchor ids.
    body_html = body_html.replace(
        '<h2 class="recap">', '<h2 class="recap" id="recap">'
    )
    body_html = body_html.replace(
        '<h2 class="insights">', '<h2 class="insights" id="insights">'
    )
    body_html = body_html.replace(
        '<h2 class="watch">', '<h2 class="watch" id="watch">'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} &middot; Daily Market Pulse</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
  <header class="masthead">
    <div class="brand">Daily Market Pulse</div>
    <h1>{title}</h1>
    <div class="dateline">{dateline}</div>
  </header>
  {body_html}
  {footer}
</div>
{section_nav}
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-file",
        type=Path,
        help="Render a local markdown file instead of fetching the latest from GitHub.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("pulse-dashboard.html"),
        help="Output HTML path (default: pulse-dashboard.html in cwd).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Skip opening the rendered file in the default browser.",
    )
    args = parser.parse_args()

    if args.from_file:
        if not args.from_file.exists():
            print(f"input not found: {args.from_file}", file=sys.stderr)
            return 1
        md = args.from_file.read_text(encoding="utf-8-sig")
        source_name = args.from_file.name
        print(f"rendering {args.from_file}")
    else:
        token = (os.environ.get("GH_TOKEN") or "").strip() or None
        auth_label = "authenticated (GH_TOKEN set)" if token else "unauthenticated"
        print(f"fetching latest archived pulse from {REPO}@{BRANCH} ({auth_label})...")
        try:
            source_name, md = fetch_latest_pulse_markdown(token)
        except urllib.error.HTTPError as e:
            print(f"GitHub API error: HTTP {e.code} {e.reason}", file=sys.stderr)
            if e.code in (401, 403, 404) and not token:
                print(
                    "hint: set GH_TOKEN env var to a fine-grained PAT with "
                    "Contents:read on this repo, especially if the repo is private",
                    file=sys.stderr,
                )
            return 2
        print(f"got {source_name} ({len(md):,} chars)")

    html = render_dashboard_html(md, source_filename=source_name)
    args.out.write_text(html, encoding="utf-8")
    size_kb = args.out.stat().st_size // 1024
    print(f"wrote {args.out} ({size_kb} KB)")

    if not args.no_open:
        # webbrowser.open expects a URL or absolute file path; the file:// URI
        # works cross-platform.
        url = args.out.resolve().as_uri()
        webbrowser.open(url)
        print(f"opened {url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
