"""Final-markdown linter for synthesized pulses.

Runs after AUDIT, before commit. Mechanical regex scan — single source
of truth for banned patterns is ai_analysis/voice_rules.py. If a pattern
slips through into the final markdown, lint reports it with line + kind
+ snippet so the routine can iterate on /tmp/final.md until clean.

Usage:
    python3 scripts/pulse_lint.py <input_md> <output_json> [<ctx_json>]

Example (from the routine):
    python3 scripts/pulse_lint.py /tmp/final.md /tmp/lint_report.json /tmp/ctx.json

Args:
    input_md:   path to the final pulse markdown to scan
    output_json: where to write the structured lint report
    ctx_json:   optional path to ctx.json — enables soft top-3 theme structural check

The script also prints a human-readable summary to stdout for the routine
to read inline. Exit code is 0 unless the input file is missing.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# voice_rules lives in ai_analysis/. The linter is invoked from the repo
# root (cwd in the routine sandbox after the repo is cloned), so direct
# package import works.
try:
    from ai_analysis.voice_rules import compose_lint_patterns
except ImportError:
    # Fallback: linter run outside the repo context (e.g. routine fetched
    # both files into /tmp). Add cwd's parent dirs to sys.path and retry.
    here = Path(__file__).resolve()
    sys.path.insert(0, str(here.parent.parent))  # repo root
    from ai_analysis.voice_rules import compose_lint_patterns


def lint_markdown(md_text: str, ctx: dict | None = None) -> list[dict]:
    """Scan final-pulse markdown for banned patterns. Returns list of
    issue dicts: {line, kind, snippet}.
    """
    # Strip fenced code blocks before scanning so URL/code semicolons
    # and dashes don't false-positive. INSIGHTS prose has no fenced blocks.
    scan_text = re.sub(r'```.*?```', '', md_text, flags=re.DOTALL)

    issues: list[dict] = []

    def add(line_no: int, kind: str, snippet: str) -> None:
        issues.append({'line': line_no, 'kind': kind, 'snippet': snippet[:80]})

    def line_of(match: re.Match, text: str) -> int:
        return text[:match.start()].count('\n') + 1

    # Banned-pattern scan from voice_rules
    for pattern, kind in compose_lint_patterns():
        for m in re.finditer(pattern, scan_text, re.IGNORECASE):
            add(line_of(m, scan_text), kind, m.group())

    # Soft structural check: top-3 themes by bank count must appear in INSIGHTS
    if ctx is not None:
        theme_map = ctx.get('theme_map') or {}
        if theme_map:
            ranked = sorted(
                theme_map.items(),
                key=lambda kv: -kv[1].get('banks', 0),
            )
            top3 = [t for t, _ in ranked[:3]]
            insight_headers = re.findall(r'^###\s+(.+)$', md_text, re.MULTILINE)
            header_text = ' '.join(insight_headers).lower()
            for theme_key in top3:
                sig_words = [w for w in theme_key.split() if len(w) > 3]
                if not sig_words:
                    continue
                hits = sum(1 for w in sig_words if w in header_text)
                if hits < min(2, len(sig_words)):
                    add(0, 'top-3-theme-missing',
                        f"top-3 theme '{theme_key}' not in INSIGHTS headers")

    return issues


def summarize(issues: list[dict]) -> None:
    """Print a human-readable summary of lint issues to stdout."""
    print(f"\nLint scan: {len(issues)} issue(s) found")
    if not issues:
        return
    by_kind: dict[str, int] = {}
    for i in issues:
        by_kind[i['kind']] = by_kind.get(i['kind'], 0) + 1
    print("By kind:")
    for k, c in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {c:>3}  {k}")
    print("\nFirst 20:")
    for i in issues[:20]:
        snippet = i['snippet']
        print(f"  L{i['line']:>4}  {i['kind']:<22}  {snippet!r}")


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: pulse_lint.py <input_md> <output_json> [<ctx_json>]",
              file=sys.stderr)
        return 2

    input_md = Path(sys.argv[1])
    output_json = Path(sys.argv[2])
    ctx_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None

    if not input_md.exists():
        print(f"input markdown not found: {input_md}", file=sys.stderr)
        return 1

    md = input_md.read_text(encoding='utf-8')
    ctx: dict | None = None
    if ctx_path is not None and ctx_path.exists():
        try:
            ctx = json.loads(ctx_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass

    issues = lint_markdown(md, ctx)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(issues, indent=1), encoding='utf-8')

    summarize(issues)
    return 0


if __name__ == '__main__':
    sys.exit(main())
