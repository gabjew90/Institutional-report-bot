"""Post-DRAFT structural validator for pulse INSIGHTS output.

WHY THIS EXISTS
================
The synthesizer side of this repo exposes structured data to DRAFT
via the theme_coverage block:

  - sibling_canonicals: theme pairs the cap blocked from merging,
    should fold into one INSIGHTS section
  - underweighted_candidate: 3+ bank multi-tier coverage outside the
    natural top-6, should at least appear as a WHAT TO WATCH bullet
  - contrarian_to_lead: multi-bank corpus voices contradicting the
    dominant theme, should NOT be buried in the lead's bear-case
    appendix
  - supportive/skeptical/neutral stance counts per theme; when split
    is >=2/>=2, DRAFT should name Bank-A-vs-Bank-B explicitly

DRAFT can read all of this and ignore it. The 2026-06-01 QC review
flagged the recurring failure mode:
  > "if next run fires the cap again on the same canonical AND ships
    a duplicate INSIGHTS section, the EDIT-layer fold-into-close
    instruction is unreliable and needs to move into DRAFT's per-
    theme slot guidance"

This validator runs AFTER DRAFT against (draft.md, ctx.json) and
reports structural violations. The orchestrating routine can choose
to:
  - re-roll DRAFT with the violations surfaced as fix-this feedback
  - lint-warn and ship anyway (current default — non-blocking)
  - hard-block on certain violation kinds (e.g. duplicate sibling
    sections)

Output is a structured JSON report + non-zero exit code on hard
violations. Same contract shape as scripts/pulse_lint.py:
  exit 0 = clean, no violations
  exit 3 = hard violations (duplicate sibling sections,
           lead contradicts contrarian signal that exists)
  exit 4 = soft violations (underweighted not surfaced, stance-split
           not named) — advisory

Usage:
    python3 scripts/pulse_draft_validate.py <draft_md> <ctx_json>
                                            <output_json>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


# Hard violations REQUIRE a re-roll. Soft violations are advisory —
# the routine can lint-warn and ship anyway.
HARD_VIOLATION_KINDS = {
    "duplicate-sibling-sections",
    "contrarian-buried-in-appendix",
    "main-event-lean-missing",
}

# A cashtag is `$` + a LETTER (so dollar amounts like $9.3B / $200B never
# match) + up to 6 more ticker chars.
_CASHTAG_RE = re.compile(r"\$[A-Za-z][A-Za-z0-9.]{0,6}")

# Direction/trade verbs that mark a sentence as proposing a position.
_DIRECTION_RE = re.compile(
    r"\b(long|short|buy|buying|sell|selling|own|owning|add|adding|"
    r"fade|fading|hedge|hedging|overweight|underweight|puts|calls)\b",
    re.IGNORECASE,
)


def _cashtags(text: str) -> set[str]:
    """Bare, upper-cased tickers (no `$`) found in `text`. A sentence-final
    period is stripped ($SPY. -> SPY) while an internal dot is kept
    (BRK.B -> BRK.B)."""
    return {
        m.group(0).upper().lstrip("$").rstrip(".")
        for m in _CASHTAG_RE.finditer(text)
    }


def _trade_tickers_in(text: str) -> set[str]:
    """Cashtags that sit in a trade-bearing sentence (one with a direction
    verb). These are the instruments a section actually proposes, as
    opposed to every ticker it mentions in passing."""
    tickers: set[str] = set()
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if _DIRECTION_RE.search(sent):
            tickers |= _cashtags(sent)
    return tickers


def _leans_instruments(md_text: str) -> set[str] | None:
    """Tickers in the `## _LEANS` block (format `- <dir> | <instr> | ...`).
    Returns None if there is no `## _LEANS` block at all (a different
    failure the routine surfaces elsewhere)."""
    m = re.search(
        r"^##\s+_LEANS\b.*?(?=^##\s|\Z)",
        md_text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    instruments: set[str] = set()
    for line in m.group(0).splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        parts = line.lstrip("- ").split("|")
        if len(parts) >= 2:           # the <instrument(s)> column
            instruments |= _cashtags(parts[1])
    return instruments


def _find_insights_sections(md_text: str) -> list[str]:
    """Return the INSIGHTS section bodies as plain strings.

    The INSIGHTS block runs from `## INSIGHTS` (or numbered variant)
    through the next `##` heading or end of document. Each `###`
    inside that block is one section; this returns each section's
    full text (including the H3 header).
    """
    insights_match = re.search(
        r'##\s+(?:\d+\.\s+)?INSIGHTS.*?(?=^##\s|\Z)',
        md_text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not insights_match:
        return []
    block = insights_match.group(0)
    # Split on H3 boundaries while keeping the headers
    parts = re.split(r'(?=^###\s)', block, flags=re.MULTILINE)
    # First part is the "## INSIGHTS" header itself; skip
    return [p.strip() for p in parts[1:] if p.strip()]


_HEURISTIC_STOPWORDS = {
    "and", "the", "for", "with", "from", "this", "that", "into",
    "over", "such", "what", "when", "where", "which", "their",
    "they", "them", "are", "was", "were", "been", "being",
    "about", "these", "those", "while", "after",
}


def _theme_distinctive_words(theme_key: str) -> list[str]:
    """Extract distinctive words from a theme key for matching. Prefer
    >=5 char content words; fall back to >=4 char if the theme key
    is too short to produce any (e.g. 'fed cuts')."""
    long_words = [
        w for w in re.findall(r"[a-zA-Z0-9]+", theme_key.lower())
        if len(w) >= 5 and w not in _HEURISTIC_STOPWORDS
    ]
    if long_words:
        return long_words
    return [
        w for w in re.findall(r"[a-zA-Z0-9]+", theme_key.lower())
        if len(w) >= 4 and w not in _HEURISTIC_STOPWORDS
    ]


def _theme_section_score(theme_key: str, section_text: str) -> int:
    """Header-weighted score: how strongly does this section 'belong
    to' this theme?

    Section headers carry the theme's noun phrase; body text mentions
    every other theme in passing. Scoring rule:
      - Each distinctive word from theme_key appearing in the H3
        HEADER counts 3 points
      - Each distinctive word appearing in the body counts 1 point
    Section with the highest score for a theme is the theme's
    primary section. Two themes mapping to DIFFERENT primary sections
    (when they're cap-blocked siblings supposed to fold) is the
    duplicate-sibling-sections violation.
    """
    words = _theme_distinctive_words(theme_key)
    if not words:
        return 0
    section_lower = section_text.lower()
    header_match = re.match(r"^###\s+([^\n]+)", section_text, re.MULTILINE)
    header_lower = (header_match.group(1) if header_match else "").lower()
    score = 0
    for w in words:
        if w in header_lower:
            score += 3
        elif w in section_lower:
            score += 1
    return score


def _theme_primary_section(
    theme_key: str, sections: list[str]
) -> int | None:
    """Return the index of the section where this theme has the
    highest score, or None if no section scored above 0. Ties broken
    by earlier index (top-of-pulse wins)."""
    scores = [
        (i, _theme_section_score(theme_key, s))
        for i, s in enumerate(sections)
    ]
    scored = [(i, score) for i, score in scores if score > 0]
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[0][0]


def _theme_in_section(theme_key: str, section_text: str) -> bool:
    """Returns True iff at least one distinctive word from the theme
    key appears anywhere in the section. Loose match — use for "is
    this theme referenced AT ALL in the pulse" checks. For "which
    section is this theme's PRIMARY section", use _theme_primary_section
    instead."""
    words = _theme_distinctive_words(theme_key)
    if not words:
        return False
    section_lower = section_text.lower()
    return any(w in section_lower for w in words)


def validate(md_text: str, ctx: dict) -> list[dict]:
    """Return a list of violation dicts. Each carries `kind`, `severity`
    (hard|soft), and a human-readable `message`. Empty list = clean."""
    violations: list[dict] = []

    theme_map = ctx.get("theme_map") or {}
    sections = _find_insights_sections(md_text)
    md_lower = md_text.lower()

    # =====================================================================
    # CHECK 1: sibling-pair didn't ship as separate sections
    # =====================================================================
    # If theme A has sibling_canonicals = [B] (and vice versa), B should
    # NOT have its own INSIGHTS section. Both should appear inside one.
    # ---------------------------------------------------------------------
    siblings_to_pair: dict[str, str] = {}  # theme -> any sibling
    for theme, info in theme_map.items():
        sibs = info.get("sibling_canonicals") or []
        if sibs:
            siblings_to_pair[theme] = sibs[0]
    # Track which sibling pairs we've already flagged so the same
    # pair (A,B) and (B,A) only emit one violation, not two.
    flagged_pairs: set[frozenset[str]] = set()
    for theme, sibling in siblings_to_pair.items():
        pair_key = frozenset((theme, sibling))
        if pair_key in flagged_pairs:
            continue
        theme_primary = _theme_primary_section(theme, sections)
        sibling_primary = _theme_primary_section(sibling, sections)
        # If both themes have a PRIMARY section AND they're different
        # sections, that's a duplicate: the synthesizer told DRAFT to
        # fold them via sibling_canonicals, DRAFT shipped them apart.
        # The primary-section check is header-weighted — a passing
        # body-mention doesn't count as "the section that's about this
        # theme", only being named in the H3 header does.
        if (theme_primary is not None
                and sibling_primary is not None
                and theme_primary != sibling_primary):
            violations.append({
                "kind": "duplicate-sibling-sections",
                "severity": "hard",
                "message": (
                    f"Theme '{theme}' (primary section #{theme_primary}) "
                    f"and its cap-blocked sibling '{sibling}' "
                    f"(primary section #{sibling_primary}) shipped as "
                    f"separate INSIGHTS sections. They should be folded "
                    f"into one section per the theme_coverage block's "
                    f"sub-bullet structure."
                ),
                "theme": theme,
                "sibling": sibling,
            })
            flagged_pairs.add(pair_key)

    # =====================================================================
    # CHECK 2: contrarian-divergence got promoted, not buried
    # =====================================================================
    # When theme_map carries a theme with contrarian_to_lead=True, the
    # DRAFT output must give that contrarian signal either its own
    # INSIGHTS section OR a WHAT TO WATCH bullet. Folding into the lead
    # theme's bear-case appendix is the failure mode the 2026-06-01 QC
    # flagged ("nobody wants NVDA / sell in May / IPO BOOM = market
    # top" all buried in the AI section's counter-case).
    # ---------------------------------------------------------------------
    for theme, info in theme_map.items():
        if not info.get("contrarian_to_lead"):
            continue
        labels = info.get("contrarian_signal_labels") or []
        # Has any section explicitly engaged with the contrarian frame?
        # Looking for the lexical fingerprint of the signal kinds.
        signal_keywords = {
            "froth/top": ["froth", "speculation", "bubble", "euphoria"],
            "calendar/rotation": ["rotate", "rotation", "sell in may", "go away"],
            "no-bid contrarian": ["nobody wants", "no-bid", "abandoned"],
            "rotate-out-of-lead": ["rotate out", "if not ai", "scarcity elsewhere", "what to buy"],
            "mixed-signals": ["hmm", "paradox", "do not add up"],
            "explicit-top-flag": ["market top", "topping", "rolling over", "peaked"],
            "lead-theme-bearish": ["ai bubble", "nvda topping", "ai cycle top", "saturation"],
        }
        keywords_for_this = []
        for lab in labels:
            keywords_for_this.extend(signal_keywords.get(lab, []))
        if not keywords_for_this:
            continue
        # Where does the contrarian content appear?
        in_dedicated_section = False
        for s in sections:
            sl = s.lower()
            kw_hits = sum(1 for kw in keywords_for_this if kw in sl)
            # 'Dedicated' = section header itself references the
            # contrarian frame (top, contrarian, rotation, speculation
            # in the H3 line) OR the section body has 2+ contrarian
            # keywords (meaning it's more than a 1-line appendix).
            header_line = s.split("\n", 1)[0].lower()
            if any(
                tag in header_line
                for tag in ("contrarian", "rotation", "rotate", "speculation",
                            "market top", "if not", "froth")
            ):
                in_dedicated_section = True
                break
            if kw_hits >= 2:
                in_dedicated_section = True
                break
        # Or in WHAT TO WATCH?
        watch_match = re.search(
            r'##\s+(?:\d+\.\s+)?WHAT\s+TO\s+WATCH.*?(?=^##\s|\Z)',
            md_text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        in_watch = False
        if watch_match:
            watch_lower = watch_match.group(0).lower()
            if any(kw in watch_lower for kw in keywords_for_this):
                in_watch = True
        if not in_dedicated_section and not in_watch:
            violations.append({
                "kind": "contrarian-buried-in-appendix",
                "severity": "hard",
                "message": (
                    f"Contrarian theme '{theme}' "
                    f"(signal kinds: {', '.join(labels)}) is not surfaced "
                    f"as a dedicated INSIGHTS section or WHAT TO WATCH "
                    f"bullet. It's likely folded into the lead theme's "
                    f"counter-case appendix. Promote it to its own slot "
                    f"with a named rotation instrument lean."
                ),
                "theme": theme,
                "signal_labels": labels,
            })

    # =====================================================================
    # CHECK 3: underweighted candidate got at least one mention
    # =====================================================================
    # When theme_map flags >=1 underweighted_candidate, at least one
    # of them should appear in the pulse (any section OR a WHAT TO
    # WATCH bullet). If ALL underweighted candidates were silently
    # dropped, soft-flag it.
    # ---------------------------------------------------------------------
    underweighted = {
        theme: info for theme, info in theme_map.items()
        if info.get("underweighted_candidate")
    }
    if underweighted:
        any_surfaced = False
        for theme in underweighted:
            if _theme_in_section(theme, md_text):
                any_surfaced = True
                break
        if not any_surfaced:
            violations.append({
                "kind": "underweighted-all-dropped",
                "severity": "soft",
                "message": (
                    f"{len(underweighted)} underweighted-candidate theme(s) "
                    f"surfaced in theme_coverage but none appear in the "
                    f"pulse. Examples: "
                    f"{', '.join(list(underweighted.keys())[:3])}. "
                    f"Surface at least one as a WHAT TO WATCH bullet."
                ),
                "themes": sorted(underweighted.keys()),
            })

    # =====================================================================
    # CHECK 4: stance-split themes named Bank-A vs Bank-B
    # =====================================================================
    # When a theme has >=2 supportive AND >=2 skeptical banks (the
    # ingredient for a real disagreement section), the section about
    # that theme should NAME at least one supporting bank AND one
    # skeptical bank by name. The 2026-06-01 QC's bank-vs-bank-
    # disagreement-regression flag.
    # ---------------------------------------------------------------------
    BANK_NAME_RE = re.compile(
        r"\b(?:Goldman|JPMorgan|JPM|Citi|BofA|Bank\s+of\s+America|UBS|"
        r"RBC|Barclays|Mizuho|ANZ|ING|Morgan\s+Stanley|MS|"
        r"Deutsche\s+Bank|Deutsche|Bernstein|TS\s+Lombard|SEB|"
        r"Wells\s+Fargo|Nomura|MUFG|Rabobank|Citadel|BlackRock|"
        r"Jefferies|Nat[iy]xis)\b",
        re.IGNORECASE,
    )
    for theme, info in theme_map.items():
        if info.get("discovered") or info.get("non_bank_only"):
            continue
        sup = info.get("supportive", 0)
        skp = info.get("skeptical", 0)
        if sup < 2 or skp < 2:
            continue
        # Find the section about this theme
        relevant_sections = [
            s for s in sections if _theme_in_section(theme, s)
        ]
        if not relevant_sections:
            continue  # not in pulse at all — different violation class
        # In any relevant section, do at least 2 distinct bank names
        # appear?
        for s in relevant_sections:
            names = set(m.group(0) for m in BANK_NAME_RE.finditer(s))
            if len(names) >= 2:
                break
        else:
            violations.append({
                "kind": "stance-split-no-named-debate",
                "severity": "soft",
                "message": (
                    f"Theme '{theme}' has stance split "
                    f"{sup} support / {skp} skeptical but the section "
                    f"about it does not name Bank-A-vs-Bank-B by name. "
                    f"The cross-bank-disagreement moat depends on "
                    f"explicit naming."
                ),
                "theme": theme,
                "supportive": sup,
                "skeptical": skp,
            })

    # =====================================================================
    # CHECK 5: the MAIN EVENT's trade reached the _LEANS block (the board)
    # =====================================================================
    # The TRADE BOARD is built mechanically from `## _LEANS`, NOT from the
    # prose. The MAIN EVENT (the lead `###` theme) is the pulse's headline
    # call, so its trade MUST appear in _LEANS or the board silently drops
    # the biggest trade. 2026-06-26 failure: the lead "Long $RSP/$IWM,
    # $GLD" rotation never reached the board because the writer's _LEANS
    # listed only the briefs' trades ($XLE/$XLU/$MU).
    #
    # Conservative: flag ONLY when the MAIN EVENT clearly proposes a trade
    # (a direction verb beside a cashtag) AND none of those instruments
    # overlap _LEANS. Any single overlap passes — extra tickers don't
    # trip it.
    # ---------------------------------------------------------------------
    leans = _leans_instruments(md_text)
    if sections and leans is not None:
        main_event_trade = _trade_tickers_in(sections[0])
        if main_event_trade and not (main_event_trade & leans):
            violations.append({
                "kind": "main-event-lean-missing",
                "severity": "hard",
                "message": (
                    f"The MAIN EVENT proposes a trade "
                    f"({', '.join('$' + t for t in sorted(main_event_trade))}) "
                    f"but none of its instruments appear in the ## _LEANS "
                    f"block (leans: "
                    f"{', '.join('$' + t for t in sorted(leans)) or 'none'}). "
                    f"The board is built from _LEANS, so the lead trade will "
                    f"be missing from the TRADE BOARD. Make the MAIN EVENT's "
                    f"trade the FIRST _LEANS line."
                ),
                "main_event_tickers": sorted(main_event_trade),
                "leans": sorted(leans),
            })

    return violations


def main() -> int:
    if len(sys.argv) < 4:
        print(
            "usage: pulse_draft_validate.py <draft_md> <ctx_json> "
            "<output_json>",
            file=sys.stderr,
        )
        return 2
    draft_md = Path(sys.argv[1])
    ctx_json = Path(sys.argv[2])
    output_json = Path(sys.argv[3])

    if not draft_md.exists():
        print(f"draft markdown not found: {draft_md}", file=sys.stderr)
        return 1
    if not ctx_json.exists():
        print(f"ctx json not found: {ctx_json}", file=sys.stderr)
        return 1

    md = draft_md.read_text(encoding="utf-8")
    try:
        ctx = json.loads(ctx_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"ctx parse error: {e}", file=sys.stderr)
        return 1

    violations = validate(md, ctx)
    hard = [v for v in violations if v.get("kind") in HARD_VIOLATION_KINDS]
    soft = [v for v in violations if v.get("kind") not in HARD_VIOLATION_KINDS]

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(
            {
                "violations": violations,
                "hard_count": len(hard),
                "soft_count": len(soft),
                "exit_code": 3 if hard else (4 if soft else 0),
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    print(f"DRAFT validator: {len(violations)} violation(s)")
    if hard:
        print(f"  HARD ({len(hard)}):")
        for v in hard:
            print(f"    [{v['kind']}] {v['message']}")
    if soft:
        print(f"  soft ({len(soft)}):")
        for v in soft:
            print(f"    [{v['kind']}] {v['message']}")
    if not violations:
        print("  CLEAN.")

    return 3 if hard else (4 if soft else 0)


if __name__ == "__main__":
    sys.exit(main())
