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
to read inline.

SCRUB-DISPATCH CONTRACT
-----------------------
Exit code is the structural signal for whether SCRUB is needed:
    0 → markdown is clean; routine should SKIP SCRUB dispatch
    1 → input file missing / config error
    2 → invalid CLI args
    3 → hard issues found; routine should DISPATCH SCRUB
    4 → only soft issues (jargon-bare, etc.); SCRUB dispatch is OPTIONAL

The same signal is also embedded in two sidecar artifacts so the
routine can consume it without parsing exit codes:

    <output_json>             — the issue list (unchanged shape)
    <output_json>.decision     — JSON: {"scrub_recommended": bool,
                                        "hard_issue_count": int,
                                        "soft_issue_count": int,
                                        "reason": str}

The 'hard' vs 'soft' classification is centralized in HARD_ISSUE_KINDS
below. The previous QC review (2026-05-29) called out that SCRUB was
dispatching on 0-lint-issue input and producing cosmetic regressions
("25-40 basis points" → "25-40 hundredths of a percent"). The decision
file makes the gate explicit at the linter boundary so the Opus routine
doesn't have to second-guess.
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
    from ai_analysis.voice_rules import compose_lint_patterns, compose_jargon_lint_patterns
except ImportError:
    # Fallback: linter run outside the repo context (e.g. routine fetched
    # both files into /tmp). Add cwd's parent dirs to sys.path and retry.
    here = Path(__file__).resolve()
    sys.path.insert(0, str(here.parent.parent))  # repo root
    from ai_analysis.voice_rules import compose_lint_patterns, compose_jargon_lint_patterns


# Hard-issue kinds REQUIRE SCRUB. Soft-issue kinds are advisory: jargon
# the model should have translated, structural-coverage warnings the
# routine may surface but doesn't need to send through Gemini to fix.
# Kinds emitted by lint_markdown that aren't in either set default to
# 'hard' for safety (SCRUB will look at it).
SOFT_ISSUE_KINDS = {
    "jargon-bare",
    "top-3-theme-missing",
    "discovered-theme-missing",
    # Slot-overlap is structural — SCRUB can't fix it (SCRUB rewrites
    # sentences, doesn't partition stats across slots). Surface as a
    # soft warning so QC and human review can act; the next iteration
    # should belong in DRAFT_USER's coverage rules. See 2026-06-04 QC
    # which flagged INSIGHTS #2 and #3 both citing the $60B levered
    # ETF stat + the 9-up-days streak.
    "slot-stat-overlap",
    # Same rationale — two slots closing on the same trade lean is a
    # DRAFT-time composition problem (2026-06-09: slots #1 and #5 both
    # closed Long $MU). The CROSS-SLOT INSTRUMENT OVERLAP block in
    # theme_coverage is the write-time prevention; this is the catch.
    "slot-lean-overlap",
}


# Regex set for extracting "stat tokens" from an INSIGHTS slot.
# Each pattern captures a distinctive number-bearing claim. Stats
# appearing in 2+ adjacent INSIGHTS slots get flagged by
# _check_slot_stat_overlap as load-bearing-citation reuse.
_STAT_TOKEN_PATTERNS = [
    # Dollar amounts with magnitude suffix: $60B, $1.5T, $700B, $35B
    r"\$\d+(?:[,.]\d+)?\s*[BMT]\b",
    # Percentages with leading numeric: 67%, 4.4%, 60% YoY
    r"\b\d+(?:\.\d+)?%(?:\s+YoY)?",
    # Basis points: 25bp, 50 basis points, 25bps
    r"\b\d+\s*(?:bps?|basis\s+points?)\b",
    # Streak / consecutive stats: 9 consecutive up days, 10 up weeks,
    # nine straight up days, nine consecutive down weeks. The middle
    # block can be a single direction-or-adjective ("up", "consecutive")
    # OR an adjective + direction ("consecutive up", "straight down").
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s+(?:consecutive|straight|up|down)"
    r"(?:\s+(?:up|down))?"
    r"\s+(?:days?|weeks?|months?)\b",
    # Ratio / multiplier shape: 24x by 2030, 2x leveraged
    r"\b\d+x(?:\s+by\s+\d{4})?\b",
    # Named indices at specific levels: 'Bull and Bear Indicator at 8.5'
    # — match the index name + level for the cross-slot dedup signal
    r"\b(?:Bull\s+and\s+Bear\s+Indicator|VIX|Goldman\s+Risk\s+Appetite|"
    r"Goldman\s+Vol\s+Panic\s+Index|Hartnett['']s\s+(?:bull|bear)?\s*"
    r"indicator)(?:\s+at\s+\d+(?:\.\d+)?)?\b",
]


def _extract_stat_tokens(slot_text: str) -> set[str]:
    """Pull a normalized set of stat tokens from one INSIGHTS slot's
    body. Normalization: lowercase, whitespace collapsed. Same logical
    stat in two slots ('nine consecutive up days' / '9 consecutive up
    days' / 'nine straight up days') resolves to the same key after
    word-number coercion."""
    word_to_digit = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
        "eleven": "11", "twelve": "12",
    }
    tokens: set[str] = set()
    for pat in _STAT_TOKEN_PATTERNS:
        for m in re.finditer(pat, slot_text, re.IGNORECASE):
            t = m.group(0).lower()
            t = re.sub(r"\s+", " ", t).strip()
            # Word-to-digit normalization for streak-style stats so
            # "nine straight up days" matches "9 consecutive up days"
            for word, digit in word_to_digit.items():
                t = re.sub(rf"\b{word}\b", digit, t)
            # Collapse the streak-verb synonyms (straight/consecutive)
            # so the comparison sees them as one stat.
            t = re.sub(
                r"\b(?:consecutive|straight)\b", "consecutive", t
            )
            tokens.add(t)
    return tokens


def _check_slot_stat_overlap(md_text: str) -> list[dict]:
    """Flag stats that appear in 2+ INSIGHTS slots. 2026-06-04 QC
    review observed AI capex slot (#2) and Hartnett rotate-out slot
    (#3) both citing the $60B levered ETF stat AND the 9-consecutive-
    up-days streak — two themes sharing load-bearing evidence.
    Returns issue dicts (one per overlap stat) with kind=
    'slot-stat-overlap'."""
    insights_match = re.search(
        r"##\s+(?:\d+\.\s+)?INSIGHTS.*?(?=^##\s|\Z)",
        md_text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not insights_match:
        return []
    insights_text = insights_match.group(0)
    insights_offset = insights_match.start()

    # Split the INSIGHTS section on H3 ('### ') headings — each slot.
    slot_pattern = re.compile(r"^###\s+(.+?)$", re.MULTILINE)
    slot_starts = [(m.start(), m.group(1).strip())
                   for m in slot_pattern.finditer(insights_text)]
    if len(slot_starts) < 2:
        return []
    slot_starts.append((len(insights_text), "__END__"))

    # Build per-slot stat-token sets keyed by 0-based slot index.
    slot_tokens: list[tuple[str, set[str], int]] = []  # (title, tokens, line)
    for i in range(len(slot_starts) - 1):
        start_off, title = slot_starts[i]
        end_off = slot_starts[i + 1][0]
        slot_body = insights_text[start_off:end_off]
        tokens = _extract_stat_tokens(slot_body)
        # Compute the actual line number in the full pulse markdown.
        slot_line = md_text[:insights_offset + start_off].count("\n") + 1
        slot_tokens.append((title, tokens, slot_line))

    # Find stats appearing in 2+ slots.
    issues: list[dict] = []
    from collections import defaultdict
    stat_to_slots: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for title, tokens, line in slot_tokens:
        for t in tokens:
            stat_to_slots[t].append((title, line))
    for stat, occurrences in stat_to_slots.items():
        if len(occurrences) >= 2:
            slot_titles = [
                f"{title[:40]}" for title, _ in occurrences
            ]
            first_line = occurrences[0][1]
            issues.append({
                "line": first_line,
                "kind": "slot-stat-overlap",
                "snippet": (
                    f"stat {stat!r} cited in {len(occurrences)} slots: "
                    f"{slot_titles}"
                )[:200],
            })
    return issues


# Trade-lean verbs — the action words that open a closing trade
# expression. Paired with a cashtag, they identify a slot's close lean.
_LEAN_VERB_RE = re.compile(
    r"\b(?:long|short|buy|sell|own|add|fade|trim|exit|scale\s+(?:into|back)|"
    r"take\s+profits?\s+(?:in|on)|pair(?:ed)?\s+(?:with|against))\s+"
    r"(?:a\s+|the\s+|half-sized\s+|small\s+)?\$([A-Za-z]{1,5})\b",
    re.IGNORECASE,
)


def _extract_close_leans(slot_text: str) -> set[str]:
    """Pull the set of tickers in a slot's CLOSING trade lean.

    Only the LAST paragraph of the slot is scanned — that's where the
    trade lean lives by the pulse's structure. Scanning the whole body
    would false-positive on tickers discussed as evidence ('$NVDA's
    $124B purchase commitment') rather than as the lean.
    """
    paragraphs = [p.strip() for p in slot_text.split("\n\n") if p.strip()]
    if not paragraphs:
        return set()
    last_para = paragraphs[-1]
    return {m.group(1).upper() for m in _LEAN_VERB_RE.finditer(last_para)}


def _check_slot_lean_overlap(md_text: str) -> list[dict]:
    """Flag tickers appearing in 2+ INSIGHTS slots' CLOSING trade leans.
    2026-06-09 QC observed INSIGHTS #1 (AI capex supercycle) and #5
    (semis positioning regime) both closing on `Long $MU` — a five-
    theme pulse with three distinct trade expressions. The stat-overlap
    check catches shared evidence; this catches shared CONCLUSIONS,
    which is the redundancy a reader actually feels. Returns issue
    dicts with kind='slot-lean-overlap' (soft — advisory for SCRUB)."""
    insights_match = re.search(
        r"##\s+(?:\d+\.\s+)?INSIGHTS.*?(?=^##\s|\Z)",
        md_text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not insights_match:
        return []
    insights_text = insights_match.group(0)
    insights_offset = insights_match.start()

    slot_pattern = re.compile(r"^###\s+(.+?)$", re.MULTILINE)
    slot_starts = [(m.start(), m.group(1).strip())
                   for m in slot_pattern.finditer(insights_text)]
    if len(slot_starts) < 2:
        return []
    slot_starts.append((len(insights_text), "__END__"))

    slot_leans: list[tuple[str, set[str], int]] = []
    for i in range(len(slot_starts) - 1):
        start_off, title = slot_starts[i]
        end_off = slot_starts[i + 1][0]
        slot_body = insights_text[start_off:end_off]
        leans = _extract_close_leans(slot_body)
        slot_line = md_text[:insights_offset + start_off].count("\n") + 1
        slot_leans.append((title, leans, slot_line))

    issues: list[dict] = []
    from collections import defaultdict
    lean_to_slots: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for title, leans, line in slot_leans:
        for ticker in leans:
            lean_to_slots[ticker].append((title, line))
    for ticker, occurrences in lean_to_slots.items():
        if len(occurrences) >= 2:
            slot_titles = [f"{title[:40]}" for title, _ in occurrences]
            issues.append({
                "line": occurrences[0][1],
                "kind": "slot-lean-overlap",
                "snippet": (
                    f"close lean '${ticker}' used in {len(occurrences)} "
                    f"slots: {slot_titles} — same trade argued twice; "
                    f"merge the slots or find a distinct expression for one"
                )[:200],
            })
    return issues


def classify_issues(issues: list[dict]) -> tuple[int, int]:
    """Split lint issues into (hard_count, soft_count). Hard issues
    require SCRUB; soft issues are advisory. See module docstring."""
    hard = sum(1 for i in issues if i.get("kind") not in SOFT_ISSUE_KINDS)
    soft = sum(1 for i in issues if i.get("kind") in SOFT_ISSUE_KINDS)
    return hard, soft


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

    # Banned-pattern scan from voice_rules (high-confidence)
    for pattern, kind in compose_lint_patterns():
        for m in re.finditer(pattern, scan_text, re.IGNORECASE):
            add(line_of(m, scan_text), kind, m.group())

    # Jargon scan (soft — model is supposed to translate inline; linter
    # surfaces hits for review but doesn't block the commit). Each hit
    # means: the term is present; verify a plain-English translation is
    # in the same paragraph or rewrite to drop the term.
    for pattern, kind in compose_jargon_lint_patterns():
        for m in re.finditer(pattern, scan_text, re.IGNORECASE):
            add(line_of(m, scan_text), kind, m.group())

    # Slot-stat-overlap scan (soft — structural, SCRUB can't fix).
    # 2026-06-04 QC flagged adjacent INSIGHTS slots citing the same
    # load-bearing stats ($60B levered ETF AUM, 9-up-days streak).
    # Stats appearing in 2+ slots reduce the perceived theme breadth.
    issues.extend(_check_slot_stat_overlap(scan_text))
    issues.extend(_check_slot_lean_overlap(scan_text))

    # Coverage checks against the theme_map (passed in ctx).
    if ctx is not None:
        theme_map = ctx.get('theme_map') or {}
        if theme_map:
            # Two cohorts for coverage tests:
            #   primary: Phase A theme_stances; banks took a directional view.
            #     Must appear in INSIGHTS body when top-ranked.
            #   discovered: Phase B promoted; banks engaged contextually
            #     without a primary stance. Must appear SOMEWHERE in the
            #     pulse (INSIGHTS body OR WHAT TO WATCH) when top-ranked.
            # non_bank_only themes are excluded from BOTH cohorts' top-3
            # check — they're commentary publications (TME, Bloomberg,
            # Reuters) without underwritten analytical desks, so promoting
            # them to lead-insight slots is a category error. The synthesizer
            # already routes them to a separate "NON-BANK-ONLY" bucket in
            # the theme_coverage prompt block; this lint mirrors that.
            primary = {
                t: i for t, i in theme_map.items()
                if not i.get('discovered') and not i.get('non_bank_only')
            }
            discovered = {
                t: i for t, i in theme_map.items()
                if i.get('discovered') and not i.get('non_bank_only')
            }

            md_lower = md_text.lower()
            insights_match = re.search(
                r'##\s+(?:\d+\.\s+)?INSIGHTS.*?(?=^##\s|\Z)',
                md_text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
            )
            insights_text = (insights_match.group(0) if insights_match else md_text).lower()

            def _has_signal(theme_key: str, haystack: str) -> bool:
                sig_words = [w for w in theme_key.split() if len(w) > 3]
                if not sig_words:
                    return True  # too-generic key — don't flag
                return any(w in haystack for w in sig_words)

            # Combined top-3 ranking across primary + discovered, sorted by
            # bank count desc. A 12-bank discovered theme (Trump-Xi class)
            # outranks a 4-bank primary one; ranking by bank count alone
            # captures "what is most engaged with in this corpus" regardless
            # of whether banks staked a stance. Without this, the lint
            # silently missed the 2026-05-12 Trump-Xi miss: 12 banks of
            # contextual engagement, ranked nowhere in the top-3 primary
            # because primary-only ranking excluded discovery-promoted.
            combined = list(primary.items()) + list(discovered.items())
            combined_ranked = sorted(
                combined, key=lambda kv: -kv[1].get('banks', 0)
            )
            for theme_key, info in combined_ranked[:3]:
                if info.get('discovered'):
                    # Discovered themes belong in WHAT TO WATCH; accept
                    # appearance anywhere in the pulse.
                    if not _has_signal(theme_key, md_lower):
                        add(0, 'top-3-theme-missing',
                            f"top-3 theme '{theme_key}' ({info.get('banks', 0)} banks, discovered) "
                            f"appears nowhere in the pulse — add a WHAT TO WATCH bullet")
                else:
                    if not _has_signal(theme_key, insights_text):
                        add(0, 'top-3-theme-missing',
                            f"top-3 primary theme '{theme_key}' ({info.get('banks', 0)} banks) "
                            f"has no significant word in INSIGHTS section")

            # Heavily-discussed DISCOVERED topics (>=6 banks) must appear
            # SOMEWHERE in the pulse, even when outside the top-3. Distinct
            # `kind` so SCRUB knows the fix is to ADD a WHAT TO WATCH bullet
            # rather than to rewrite an existing INSIGHTS sentence.
            for theme_key, info in discovered.items():
                if info.get('banks', 0) >= 6 and not _has_signal(theme_key, md_lower):
                    add(0, 'discovered-theme-missing',
                        f"discovered topic '{theme_key}' ({info.get('banks', 0)} banks) "
                        f"appears nowhere in the pulse — add a WHAT TO WATCH bullet "
                        f"(no consensus stance — banks discussed without arguing direction)")

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

    # SCRUB-dispatch decision sidecar. Routine reads this single file to
    # decide whether to fire SCRUB; the issue list (output_json) carries
    # the per-issue detail SCRUB consumes when dispatched. Splitting the
    # decision from the issue list lets the decision contract evolve
    # without breaking SCRUB's input schema.
    hard, soft = classify_issues(issues)
    if hard > 0:
        scrub_recommended = True
        reason = f"{hard} hard lint issue(s) — SCRUB needed"
        exit_code = 3
    elif soft > 0:
        scrub_recommended = False
        reason = (
            f"{soft} soft issue(s) only (jargon/coverage warnings) — "
            f"routine may surface but SCRUB rewrite optional"
        )
        exit_code = 4
    else:
        scrub_recommended = False
        reason = "no lint issues — SCRUB skip (clean input)"
        exit_code = 0

    decision_path = output_json.with_suffix(output_json.suffix + ".decision")
    decision_path.write_text(
        json.dumps(
            {
                "scrub_recommended": scrub_recommended,
                "hard_issue_count": hard,
                "soft_issue_count": soft,
                "total_issue_count": len(issues),
                "reason": reason,
                "exit_code": exit_code,
            },
            indent=1,
        ),
        encoding='utf-8',
    )

    summarize(issues)
    print(
        f"\nSCRUB decision: "
        f"{'DISPATCH' if scrub_recommended else 'SKIP'}  "
        f"({reason})  ->  exit {exit_code}"
    )
    print(f"decision sidecar: {decision_path}")
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
