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
}


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


def _theme_in_section(theme_key: str, section_text: str) -> bool:
    """Heuristic: a theme is 'in' a section if at least one distinctive
    long-word (>=5 chars, not stopword) from the theme key appears in
    the section body.

    The 5-char floor filters generic short words ('ai', 'us', 'fed',
    'gdp') that would false-positive every section. Stopwords filter
    structural words. What's left ('capex', 'infrastructure',
    'hormuz', 'sentiment', 'warsh') is distinctive enough that a single
    occurrence in a section is meaningful. Empirically this matches
    section headers (which paraphrase but keep the noun) AND body
    references."""
    STOP = {
        "and", "the", "for", "with", "from", "this", "that", "into",
        "over", "such", "what", "when", "where", "which", "their",
        "they", "them", "are", "was", "were", "been", "being",
        "their", "about", "these", "those", "while", "after",
    }
    words = [
        w for w in re.findall(r"[a-zA-Z0-9]+", theme_key.lower())
        if len(w) >= 5 and w not in STOP
    ]
    if not words:
        # Theme has no distinctive long words — fall back to >=4 char
        # words. Edge case for short theme labels like "fed cuts" or
        # "iran risk".
        words = [
            w for w in re.findall(r"[a-zA-Z0-9]+", theme_key.lower())
            if len(w) >= 4 and w not in STOP
        ]
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
    for theme, sibling in siblings_to_pair.items():
        theme_sections = [
            i for i, s in enumerate(sections) if _theme_in_section(theme, s)
        ]
        sibling_sections = [
            i for i, s in enumerate(sections) if _theme_in_section(sibling, s)
        ]
        # If both have sections AND they're different sections, that's
        # a duplicate (the synthesizer folded them; DRAFT split them).
        if theme_sections and sibling_sections:
            overlap = set(theme_sections) & set(sibling_sections)
            if not overlap:
                violations.append({
                    "kind": "duplicate-sibling-sections",
                    "severity": "hard",
                    "message": (
                        f"Theme '{theme}' and its cap-blocked sibling "
                        f"'{sibling}' shipped as separate INSIGHTS sections "
                        f"(indices {theme_sections} vs {sibling_sections}). "
                        f"They should be folded into one section per the "
                        f"theme_coverage block's sub-bullet structure."
                    ),
                    "theme": theme,
                    "sibling": sibling,
                })

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
