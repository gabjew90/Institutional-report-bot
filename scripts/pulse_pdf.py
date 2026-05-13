"""Render a pulse markdown to a styled PDF.

Produces a PDF that reads like a sell-side research note for WhatsApp
sharing. Each of the three sections gets a color band matching the Discord
embed scheme: RECAP gold, INSIGHTS & ALPHA blue, WHAT TO WATCH orange.

Usage:
    python scripts/pulse_pdf.py <input_md> <output_pdf>

Renderer chain: markdown → HTML (with inline CSS) → Edge headless print.
Edge is available on every Windows install (no extra deps) and is what
the Railway worker would use via Playwright/Chromium in production. The
HTML is also self-contained, so the worker can switch to wkhtmltopdf,
pdfkit, or chromium-headless without changing the styling.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import markdown


CSS = """
@page {
  size: Letter;
  margin: 0.55in 0.6in 0.7in 0.6in;
  @bottom-right {
    content: "Page " counter(page) " of " counter(pages);
    font: 9pt 'Segoe UI', system-ui, sans-serif;
    color: #888;
  }
  @bottom-left {
    content: "Daily Market Pulse";
    font: 9pt 'Segoe UI', system-ui, sans-serif;
    color: #888;
  }
}

* { box-sizing: border-box; }

body {
  font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
  font-size: 10.5pt;
  line-height: 1.45;
  color: #1a1a1a;
  margin: 0;
}

/* === MASTHEAD === */
.masthead {
  border-bottom: 3px solid #1a1a1a;
  padding-bottom: 10pt;
  margin-bottom: 16pt;
}
.masthead .brand {
  font-size: 9pt;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: #666;
  margin-bottom: 4pt;
}
.masthead h1 {
  font-size: 22pt;
  line-height: 1.1;
  margin: 0 0 4pt 0;
  font-weight: 700;
}
.masthead .dateline {
  font-size: 10pt;
  color: #555;
}

/* === SECTION BANNERS === */
h2 {
  font-size: 13pt;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  margin: 18pt 0 8pt 0;
  padding: 7pt 11pt;
  border-radius: 3px;
  color: #ffffff;
  page-break-after: avoid;
}
/* Match Discord embed colors */
h2.recap     { background: #c9a227; }
h2.insights  { background: #2956a3; }
h2.watch     { background: #d97706; }

/* === THEMES === */
h3 {
  font-size: 12pt;
  font-weight: 700;
  margin: 14pt 0 4pt 0;
  color: #111;
  page-break-after: avoid;
}
/* Theme header inside INSIGHTS gets a left-border accent */
.insights-body h3 {
  border-left: 3px solid #2956a3;
  padding-left: 8pt;
  margin-left: -4pt;
}
.watch-body h3 {
  border-left: 3px solid #d97706;
  padding-left: 8pt;
  margin-left: -4pt;
}

/* === BODY === */
p {
  margin: 6pt 0;
  orphans: 2;
  widows: 2;
}
em {
  color: #2956a3;
  font-style: italic;
  font-weight: 500;
}
strong {
  color: #111;
  font-weight: 700;
}
ul, ol {
  margin: 6pt 0;
  padding-left: 22pt;
}
li {
  margin: 3pt 0;
}

/* === CASHTAGS === */
.cashtag {
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 9.8pt;
  background: #f4f4f4;
  padding: 0 4pt;
  border-radius: 2px;
  color: #1a3a6b;
  font-weight: 600;
}

/* === FOOTER === */
.footer {
  margin-top: 22pt;
  padding-top: 8pt;
  border-top: 1px solid #ddd;
  font-size: 8.5pt;
  color: #777;
  line-height: 1.4;
}
.footer .label {
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 600;
  color: #555;
}
hr {
  border: 0;
  border-top: 1px dashed #ccc;
  margin: 14pt 0;
}
"""


def _strip_frontmatter(md: str) -> tuple[dict, str]:
    """Strip YAML-ish frontmatter block. Return (meta, body).

    Tolerant of a leading UTF-8 BOM (PowerShell `Out-File -Encoding utf8`
    adds one) — without this the `---` sniff fails and the frontmatter
    silently falls through as body content, leaving meta empty and the
    footer rendering "? research analyses / 0 tokens / blank timestamp".
    """
    if md.startswith("﻿"):
        md = md[1:]
    if not md.startswith("---"):
        return {}, md
    end = md.find("\n---", 4)
    if end == -1:
        return {}, md
    block = md[3:end].strip()
    body = md[end + 4 :].lstrip("\n")
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
    """Add CSS classes to each H2 section + wrap section bodies in divs so
    theme headers inside INSIGHTS/WATCH get the colored left-border accent.

    The markdown produces flat HTML — h2 then siblings until the next h2.
    We post-process the string to inject class names and wrap each h2's
    siblings into a section div.
    """

    # First add `class="..."` to the H2 elements based on content.
    def _classify(m: re.Match) -> str:
        heading_text = re.sub(r"<.*?>", "", m.group(1)).lower()
        if "recap" in heading_text:
            cls = "recap"
        elif "insights" in heading_text or "alpha" in heading_text:
            cls = "insights"
        elif "watch" in heading_text:
            cls = "watch"
        else:
            cls = ""
        return f'<h2 class="{cls}">{m.group(1)}</h2>'

    body_html = re.sub(r"<h2>(.*?)</h2>", _classify, body_html, flags=re.DOTALL)

    # Wrap content between h2s in a div whose class lets us style the children.
    parts = re.split(r'(<h2 class="[^"]*">.*?</h2>)', body_html, flags=re.DOTALL)
    out: list[str] = []
    open_section: str | None = None
    for part in parts:
        m = re.match(r'<h2 class="(\w*)">', part)
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


def render_markdown_to_html(md: str, meta: dict) -> str:
    title, body_md = _extract_title_and_body(md)
    body_html = markdown.markdown(
        body_md,
        extensions=["extra", "sane_lists", "smarty"],
    )
    body_html = _cashtag_to_span(body_html)
    body_html = _wrap_sections(body_html)

    today = datetime.now().strftime("%A, %B %d, %Y")
    dumped = meta.get("dumped_at_utc", "")[:19].replace("T", " ")
    pdf_count = meta.get("pdf_count", "?")
    in_tok = meta.get("input_tokens", 0)
    out_tok = meta.get("output_tokens", 0)

    footer = f"""
<div class="footer">
  <span class="label">Coverage:</span> {pdf_count} research analyses
  &nbsp;·&nbsp;
  <span class="label">Tokens:</span> {in_tok:,} in / {out_tok:,} out
  &nbsp;·&nbsp;
  <span class="label">Synthesized:</span> {dumped} UTC
  <br/>
  <span class="label">For:</span> self-directed US options &amp; crypto traders
  &nbsp;·&nbsp;
  Not investment advice. Internal note.
</div>
"""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
  <div class="masthead">
    <div class="brand">Daily Market Pulse</div>
    <h1>{title}</h1>
    <div class="dateline">{today}</div>
  </div>
  {body_html}
  {footer}
</body>
</html>
"""


def _find_edge() -> str:
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(p).exists():
            return p
    raise FileNotFoundError(
        "msedge.exe not found — install Edge or swap renderer to chromium-headless"
    )


def html_to_pdf(html: str, output_pdf: Path) -> None:
    edge = _find_edge()
    with tempfile.NamedTemporaryFile(
        suffix=".html", delete=False, encoding="utf-8", mode="w"
    ) as tmp:
        tmp.write(html)
        tmp_path = Path(tmp.name)
    try:
        out_abs = output_pdf.resolve()
        in_url = "file:///" + str(tmp_path.resolve()).replace("\\", "/")
        cmd = [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out_abs}",
            in_url,
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def render_pulse_pdf(md_path: Path, pdf_path: Path) -> None:
    # utf-8-sig transparently strips a BOM if present (Windows tools love
    # to add one); _strip_frontmatter is also BOM-tolerant as belt+braces.
    md = md_path.read_text(encoding="utf-8-sig")
    meta, body = _strip_frontmatter(md)
    html = render_markdown_to_html(body, meta)
    html_to_pdf(html, pdf_path)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: pulse_pdf.py <input_md> <output_pdf>", file=sys.stderr)
        return 2
    md_path = Path(sys.argv[1])
    pdf_path = Path(sys.argv[2])
    if not md_path.exists():
        print(f"input not found: {md_path}", file=sys.stderr)
        return 1
    render_pulse_pdf(md_path, pdf_path)
    print(f"wrote {pdf_path} ({pdf_path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
