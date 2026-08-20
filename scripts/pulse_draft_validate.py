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

import datetime
import json
import re
import sys
from pathlib import Path


# Hard violations REQUIRE a re-roll. Soft violations are advisory —
# the routine can lint-warn and ship anyway.
HARD_VIOLATION_KINDS = {
    # A released macro event the pulse discusses without its print, and
    # a figure that matches the consensus on a row that has already
    # printed. Both shipped on 2026-08-12 (see the check docstrings).
    "released-actual-missing",
    "duplicate-sibling-sections",
    "contrarian-buried-in-appendix",
    "main-event-lean-missing",
    "leans-block-missing",
    "weekday-date-mismatch",
    # The previous pulse published a consensus this pulse now denies
    # or ignores while reporting the actual (2026-08-19 Target).
    "consensus-amnesia",
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


def _leans_line_count(md_text: str) -> int | None:
    """Count `- <dir> | <instr> | ...` lines in the `## _LEANS` block.
    Returns None if there is no `## _LEANS` block at all, else the number
    of well-formed lean lines (0 = present-but-empty)."""
    m = re.search(
        r"^##\s+_LEANS\b.*?(?=^##\s|\Z)",
        md_text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    n = 0
    for line in m.group(0).splitlines():
        s = line.strip()
        if s.startswith("-") and s.count("|") >= 1:
            n += 1
    return n


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


# =====================================================================
# CHECK 7 support: numeric-scope-drift
# =====================================================================
# The failure this catches (2026-07-07): the source said "119% Y/Y in
# May, with memory pricing improving materially" and the draft rendered
# it as "May SEMICONDUCTOR INDUSTRY SALES still grew 119%". The number
# is real and present in the source, so a fabrication check passes — the
# defect is that a MEMORY figure was re-attached to the whole industry
# (actual 2026 semi industry growth was ~26%, memory ~39%). The number's
# SUBJECT drifted from narrow to broad.
#
# Structural test: for a stat that appears in BOTH draft and source with
# the same value, do the draft's subject words and the source's subject
# words share anything? Zero overlap => the draft is talking about the
# number in terms the source never used for it => scope drift.
# ---------------------------------------------------------------------

# A "statistic" figure: a percentage or a points/bps figure. These are
# the scope-drift vector (a segment % re-scoped to the whole industry).
# Dollar amounts are excluded on purpose — they're far more often prices
# or draft-side arithmetic ("$700B, a sevenfold jump") than a re-scoped
# source stat, and including them would cost precision.
# Trailing \b lives on each WORD unit (percent/pp/bps); '%' takes none —
# a '%' is non-word and is usually followed by a space, so a trailing \b
# after it would never match (this silently disabled the whole check
# until 2026-07-07).
_STAT_FIGURE_RE = re.compile(
    r"\b\d{1,4}(?:\.\d+)?\s*"
    r"(?:%|percent\b|percentage\s+points?\b|pp\b|bps\b|basis\s+points?\b)",
    re.IGNORECASE,
)

# Words that never denote what a statistic is ABOUT. Two families:
#   1. quant / direction / time / hedge boilerplate
#   2. MEASURE words — the thing being counted (sales, revenue, pricing,
#      capex...). These are the METRIC, not the SUBJECT: "memory sales"
#      and "chip sales" share "sales" but are about different things.
#      Stripping them forces the overlap check onto the ENTITY (memory
#      vs semiconductor), which is where scope-drift actually lives.
# NOTE: "industry"/"sector"/"market" are deliberately NOT here — a
# narrow->broad ENTITY swap is exactly the signal we want to keep.
_SCOPE_NOISE = {
    # quant / direction / time / hedge
    "year", "years", "annual", "annualized", "month", "months", "quarter",
    "growth", "grew", "grow", "grown", "rose", "rise", "rising", "gain",
    "gained", "gains", "jump", "jumped", "surge", "surged", "climb",
    "climbed", "fell", "fall", "drop", "dropped", "decline", "declined",
    "versus", "prior", "from", "over", "under", "above", "below", "about",
    "roughly", "around", "nearly", "still", "already", "average", "averaged",
    "consensus", "estimate", "estimated", "reading", "print", "printed",
    "material", "materially", "improving", "improved", "pace", "running",
    "each", "every", "first", "second", "third", "last", "next", "this",
    "that", "with", "and", "the", "for", "was", "were", "been", "being",
    "into", "their", "they", "them", "which", "while", "after", "than",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    # measure words (the metric, not the subject)
    "sales", "sale", "revenue", "revenues", "pricing", "price", "prices",
    "priced", "capex", "capital", "spending", "spend", "expenditure",
    "expenditures", "buyback", "buybacks", "repurchase", "repurchases",
    "earnings", "profit", "profits", "margin", "margins", "volume",
    "volumes", "shipments", "authorization", "authorizations", "guidance",
    # bank / house names — ATTRIBUTION, not subject. Both the draft cite
    # "(JPMorgan)" and the source's attribution carry these, so leaving
    # them in would create false overlap and mask real drift.
    "goldman", "sachs", "jpmorgan", "morgan", "stanley", "citi", "citigroup",
    "bofa", "barclays", "mizuho", "rabobank", "mufg", "nomura", "bernstein",
    "lombard", "wells", "fargo", "jefferies", "natixis", "deutsche",
    "citadel", "blackrock", "america",
}

# ENTITY synonyms ONLY — merging them avoids a false flag when the draft
# names the same entity a different way (chip vs semiconductor). A narrow
# term is NEVER mapped to its broad parent here: mapping memory->
# semiconductor would mask the exact drift we are trying to catch. So the
# clusters {memory,dram,hbm,nand,flash} and {semiconductor,semi,chip,chips}
# stay separate, and a memory->semiconductor swap surfaces as zero overlap.
_SCOPE_SYNONYMS = {
    "semi": "semiconductor", "semis": "semiconductor",
    "chip": "semiconductor", "chips": "semiconductor",
    "dram": "memory", "hbm": "memory", "nand": "memory", "flash": "memory",
    "deposit": "deposits",
    "hyperscalers": "hyperscaler",
}


def _figure_key(figure_text: str) -> str:
    """The comparable numeric token of a stat: the digit run, unit and
    spacing dropped. '119%' -> '119', '4.53 %' -> '4.53', '45 percentage
    points' -> '45'."""
    m = re.match(r"\s*(\d{1,4}(?:\.\d+)?)", figure_text)
    return m.group(1) if m else figure_text.strip()


def _scope_anchors(window: str) -> set[str]:
    """The subject words in a text window: >=4-char alpha tokens minus
    quant/time/hedge noise, synonym-normalized. This is what a statistic
    is ABOUT."""
    out: set[str] = set()
    for w in re.findall(r"[a-zA-Z]+", window.lower()):
        if len(w) < 4 or w in _SCOPE_NOISE:
            continue
        out.add(_SCOPE_SYNONYMS.get(w, w))
    return out


def _collect_source_strings(ctx: dict) -> list[str]:
    """Every string VALUE in the context, flattened. The research text
    DRAFT was given lives here — notably the big `analyses_json` blob,
    which is itself serialized JSON. That blob is re-parsed so we keep
    only its value strings (each insight isolated) and drop structural
    keys like "source"/"key_insights" — otherwise those keys, and text
    from a neighbouring insight, would bleed into a figure's window and
    muddy its subject."""
    out: list[str] = []

    def walk(o: object) -> None:
        if isinstance(o, str):
            st = o.strip()
            # A string that is itself a JSON container (the analyses_json
            # blob) — parse and recurse so keys are dropped and each
            # value stands alone.
            if st[:1] in "[{" and st[-1:] in "]}":
                try:
                    walk(json.loads(st))
                    return
                except (ValueError, TypeError):
                    pass
            out.append(o)
        elif isinstance(o, dict):
            for v in o.values():   # values only — keys are structural
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(ctx)
    return out


def _source_stat_index(source_strings: list[str]) -> dict[str, set[str]]:
    """Map each source stat value -> the union of subject words seen
    around every occurrence of it. Union (not per-occurrence) is
    deliberate: it makes the downstream overlap check LENIENT, so a stat
    that legitimately spans a couple of framings never false-flags."""
    idx: dict[str, set[str]] = {}
    for s in source_strings:
        for m in _STAT_FIGURE_RE.finditer(s):
            key = _figure_key(m.group(0))
            lo, hi = max(0, m.start() - 90), min(len(s), m.end() + 90)
            idx.setdefault(key, set()).update(_scope_anchors(s[lo:hi]))
    return idx


def _numeric_scope_violations(
    sections: list[str], source_stats: dict[str, set[str]]
) -> list[dict]:
    """Flag INSIGHTS stats whose subject shares NOTHING with the source's
    subject for that same figure value. Only fires when the figure is
    present in the source (so it's a re-scope, not a fabrication), both
    sides have subject words, and the overlap is empty.

    FIRST-OCCURRENCE ONLY: a figure enters the pulse once with its real
    subject; the FIRST mention is where a genuine drift attaches the wrong
    subject. Later mentions are prose callbacks that restate the number in
    generic terms ("60% of the index's earnings rides on one trade") and
    would false-positive against the source's concrete wording. So each
    figure value is decided by its first subject-bearing mention, then
    retired. Trade-off: two DIFFERENT real claims sharing one number would
    only have the first checked — rare, and worth the precision on an
    advisory check."""
    violations: list[dict] = []
    evaluated: set[str] = set()  # figure keys already decided vs the source
    for sec in sections:
        for sent in re.split(r"(?<=[.!?])\s+", sec):
            for m in _STAT_FIGURE_RE.finditer(sent):
                key = _figure_key(m.group(0))
                if key in evaluated:
                    continue
                src_anchors = source_stats.get(key)
                if not src_anchors:
                    continue  # figure not in source as a stat -> skip
                draft_anchors = _scope_anchors(sent)
                if not draft_anchors:
                    continue  # no subject in this mention -> wait for one
                evaluated.add(key)  # first subject-bearing mention decides
                if draft_anchors & src_anchors:
                    continue  # subject agrees -> fine
                violations.append({
                    "kind": "numeric-scope-drift",
                    "severity": "soft",
                    "message": (
                        f"The figure '{m.group(0).strip()}' is attached to "
                        f"[{', '.join(sorted(draft_anchors)[:5])}] but the "
                        f"source uses it for [{', '.join(sorted(src_anchors)[:5])}]"
                        f" — the subjects don't overlap. This is a segment "
                        f"stat re-scoped to a broader subject (e.g. a memory "
                        f"figure called 'industry sales'). Re-scope the number "
                        f"to the source's subject or drop it."
                    ),
                    "figure": m.group(0).strip(),
                    "draft_subject": sorted(draft_anchors)[:8],
                    "source_subject": sorted(src_anchors)[:8],
                })
    return violations


# ---------------------------------------------------------------------------
# CHECK 8 support: weekday-vs-date consistency
# ---------------------------------------------------------------------------
# 2026-07-15 pulse: "Tuesday 7/22: Alphabet, Tesla, ServiceNow" — July 22,
# 2026 is a Wednesday. Purely deterministic to catch; no model judgment
# involved. We scan every "<Weekday> M/D" and "<Weekday>, Month D" pairing
# and recompute the weekday from the ctx date's year.

_WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)
_MONTHS = {
    m.lower(): i + 1 for i, m in enumerate((
        "January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December",
    ))
}
for _m in list(_MONTHS):
    _MONTHS[_m[:3]] = _MONTHS[_m]  # jan/feb/... abbreviations

_WEEKDAY_DATE_RE = re.compile(
    r"\b(" + "|".join(_WEEKDAYS) + r")\s*,?\s+"
    r"(?:"
    r"(\d{1,2})/(\d{1,2})"                       # 7/22
    r"|"
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\b"          # July 22 / Jul 22
    r")"
)


def _ctx_today(ctx: dict) -> "datetime.date | None":
    for key in ("today", "dumped_at_utc"):
        raw = str(ctx.get(key) or "")[:10]
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            continue
    return None


def _weekday_date_violations(md_text: str, ctx: dict) -> list[dict]:
    today = _ctx_today(ctx)
    if today is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for m in _WEEKDAY_DATE_RE.finditer(md_text):
        claimed = m.group(1)
        if m.group(2):
            month, day = int(m.group(2)), int(m.group(3))
        else:
            month = _MONTHS.get((m.group(4) or "").lower())
            if month is None:
                continue  # "Tuesday After ..." — not a month word
            day = int(m.group(5))
        # Resolve the year: the pulse talks about dates near today. Pick
        # the candidate year that lands the date closest to today (handles
        # a December pulse naming a January date).
        candidates = []
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                candidates.append(datetime.date(year, month, day))
            except ValueError:
                continue
        if not candidates:
            continue
        resolved = min(candidates, key=lambda d: abs((d - today).days))
        if abs((resolved - today).days) > 120:
            continue  # far-off date — year resolution too uncertain to judge
        actual_weekday = _WEEKDAYS[resolved.weekday()]
        if actual_weekday != claimed:
            key = f"{claimed}|{month}/{day}"
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "kind": "weekday-date-mismatch",
                "severity": "hard",
                "message": (
                    f'The pulse says "{m.group(0)}" but {resolved.isoformat()} '
                    f"is a {actual_weekday}. Fix the weekday (or the date — "
                    f"check the calendar blocks for the event's real date)."
                ),
                "claimed": claimed,
                "actual": actual_weekday,
                "date": resolved.isoformat(),
            })
    return out


# ---------------------------------------------------------------------------
# CHECK 9 support: released-figure reconciliation against calendar ACTUALs
# ---------------------------------------------------------------------------
# 2026-07-15 pulse: June CPI shipped as "3.88% ... core at 2.86%" — those
# were the consensus ESTIMATES; the print was 3.5%/2.6%. The desk-preview
# figures reached the draft via research PDFs and nothing reconciled them
# against the released number. This check walks every %-figure that sits
# in a sentence naming a calendar event with an ACTUAL= value and flags
# figures that match nothing in that event's calendar row(s) — the exact
# signature of an estimate (or stale/wrong number) presented as the print.
# SOFT severity: the calendar usually carries one measure (m/m) while
# prose may legitimately cite another (y/y) that simply isn't verifiable
# from context — the flag tells AUDIT "verify or reframe", not "wrong".

_ECON_ROW_RE = re.compile(
    r"^\s*(\d{2})-(\d{2})\s+\d{2}:\d{2}\s+ET\s*\|\s*\[[A-Z]{2}\]\s*([^|]+?)\s*\|(.*)$"
)
_NUM_TOKEN_RE = re.compile(r"-?\d+(?:\.\d+)?")
_PCT_FIGURE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
# Expectation markers: a figure already labeled as an expectation within
# this many chars is exempt (it's presented AS an estimate — fine).
_EXPECT_MARKERS = ("estimate", "est.", "est ", "expected", "consensus",
                   "forecast", "preview", "penciled", "eyeing")
_EXPECT_WINDOW = 40
# Only rows for these events are worth reconciling — matches the hard
# calendar whitelist that reaches synthesis.
_EVENT_KEYWORDS = ("CPI", "PPI", "PCE", "GDP", "NFP", "PAYROLLS",
                   "RETAIL SALES", "ISM", "UNEMPLOYMENT")


def _seg_numbers(row_tail: str, label: str) -> set[float]:
    """Numeric values from the `label=...` segments of a calendar row."""
    out: set[float] = set()
    for seg in row_tail.split("|"):
        seg = seg.strip()
        if not seg.upper().startswith(label.upper()):
            continue
        # Strip the "(for 2026-06)" period tag before harvesting numbers.
        seg = re.sub(r"\(for [^)]*\)", "", seg)
        out.update(float(t) for t in _NUM_TOKEN_RE.findall(seg))
    return {n for n in out if abs(n) < 1000}


def _released_event_rows(ctx: dict) -> dict[str, dict[str, set[float]]]:
    """Map event keyword -> {'ok': actual+prev values, 'est': estimate
    values} from its RELEASED calendar rows. A prose figure matching 'ok'
    traces to the calendar; matching ONLY 'est' is the estimate-as-print
    signature."""
    cal = str(ctx.get("economic_calendar") or "")
    out: dict[str, dict[str, set[float]]] = {}
    in_released = False
    for line in cal.splitlines():
        up = line.upper()
        if "ALREADY RELEASED" in up:
            in_released = True
            continue
        if "STILL UPCOMING" in up:
            in_released = False
            continue
        if not in_released or "ACTUAL=" not in up:
            continue
        m = _ECON_ROW_RE.match(line)
        name = (m.group(3) if m else line).upper()
        tail = line.split("|", 2)[-1] if "|" in line else line
        actual = _seg_numbers(tail, "ACTUAL=")
        ok = actual | _seg_numbers(tail, "prev=")
        est = _seg_numbers(tail, "est=")
        for kw in _EVENT_KEYWORDS:
            if kw in name:
                slot = out.setdefault(
                    kw, {"ok": set(), "est": set(), "actual": set()})
                slot["ok"] |= ok
                slot["est"] |= est
                # Kept separate from 'ok' (which folds in prev) so the
                # missing-print check can ask specifically whether the
                # ACTUAL reached the reader.
                slot["actual"] |= actual

    # Second ground-truth source: the CORPUS's own released/forecast
    # labels. Finnhub's calendar (the only feed carrying actuals) went
    # behind a paid tier 2026-06-11 and the ForexFactory fallback
    # hardcodes actual=None — so calendar-only ground truth meant this
    # check usually never ran, while the extractor was labeling exactly
    # these figures (MacroIndicator.status, KeyDataPoint.figure_status)
    # in the analyses the pulse synthesizes. The 08-04 pulse told
    # readers the ISM actual "has not reached our feed" while a DB note
    # in its corpus carried it (55.6 vs 53.9 expected) — 2026-08-04
    # review, G3.
    try:
        aj = ctx.get("analyses_json")
        analyses = json.loads(aj) if isinstance(aj, str) else (aj or [])
    except (json.JSONDecodeError, TypeError):
        analyses = []
    for a in analyses if isinstance(analyses, list) else []:
        for mi in (a.get("macro") or []):
            status = (mi.get("status") or "").lower()
            name = (mi.get("indicator") or "").upper()
            vals = {
                float(m.group(1))
                for m in _PCT_FIGURE_RE.finditer(mi.get("reading") or "")
            }
            if not vals:
                continue
            for kw in _EVENT_KEYWORDS:
                if kw in name:
                    slot = out.setdefault(
                        kw, {"ok": set(), "est": set(), "actual": set()})
                    if status == "released":
                        slot["ok"] |= vals
                        slot["actual"] |= vals
                    elif status == "forecast":
                        slot["est"] |= vals
        for kdp in (a.get("data_points") or []):
            status = (kdp.get("figure_status") or "").lower()
            name = (kdp.get("metric") or "").upper()
            vals = {
                float(m.group(1))
                for m in _PCT_FIGURE_RE.finditer(kdp.get("figure") or "")
            }
            if not vals:
                continue
            for kw in _EVENT_KEYWORDS:
                if kw in name:
                    slot = out.setdefault(
                        kw, {"ok": set(), "est": set(), "actual": set()})
                    if status == "released":
                        slot["ok"] |= vals
                        slot["actual"] |= vals
                    elif status == "forecast":
                        slot["est"] |= vals
    return out


def _released_figure_violations(md_text: str, ctx: dict) -> list[dict]:
    rows = _released_event_rows(ctx)
    if not rows:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    # Scope = one markdown LINE (a bullet or a prose paragraph). Sentence
    # splitting separated the event keyword from its figures ("...June CPI
    # came in cold. Headline prices fell 0.42%...") and silently skipped
    # exactly the failure shape this check exists for.
    for sent in md_text.splitlines():
        up = sent.upper()
        hit_kws = [kw for kw in rows if kw in up]
        if not hit_kws:
            continue
        valid: set[float] = set()
        est_vals: set[float] = set()
        for kw in hit_kws:
            valid |= rows[kw]["ok"]
            est_vals |= rows[kw]["est"]
        figures = list(_PCT_FIGURE_RE.finditer(sent))
        # An expectation marker exempts only its NEAREST figure — in
        # "against a +0.2% estimate, landing at 2.86%", the marker labels
        # 0.2, not the 2.86 stated as the print right after it.
        exempt: set[int] = set()
        low_sent = sent.lower()
        for mk in _EXPECT_MARKERS:
            start = 0
            while True:
                pos = low_sent.find(mk, start)
                if pos < 0:
                    break
                start = pos + 1
                near = [
                    (min(abs(pos - f.start()), abs(pos - f.end())), i)
                    for i, f in enumerate(figures)
                ]
                if near:
                    dist, idx = min(near)
                    if dist <= _EXPECT_WINDOW:
                        exempt.add(idx)
        for f_idx, fm in enumerate(figures):
            val = float(fm.group(1))
            if f_idx in exempt:
                continue
            # Magnitude match: prose writes direction verbally ("fell
            # 0.42%") while the calendar carries the signed value (-0.4).
            if any(abs(abs(val) - abs(v)) <= 0.06 for v in valid):
                continue
            key = f"{hit_kws[0]}|{fm.group(1)}"
            if key in seen:
                continue
            seen.add(key)
            is_est_hit = any(
                abs(abs(val) - abs(v)) <= 0.06 for v in est_vals
            )
            if is_est_hit:
                msg = (
                    f"{fm.group(1)}% stated for {hit_kws[0].title()} "
                    f"MATCHES THE CONSENSUS ESTIMATE on the calendar row, "
                    f"not the ACTUAL — near-certain estimate-shipped-as-"
                    f"print (the 2026-07-15 CPI failure). Replace with the "
                    f"ACTUAL= value or label it as the expectation."
                )
            else:
                msg = (
                    f"{fm.group(1)}% is stated for {hit_kws[0].title()} but "
                    f"matches neither the ACTUAL nor prev on that event's "
                    f"RELEASED calendar row (row values: "
                    f"{sorted(valid) if valid else 'none parsed'}). Desk "
                    f"previews carry ESTIMATES (2026-07-15: consensus "
                    f"3.88%/2.86% shipped as the CPI print; actual was "
                    f"3.5%/2.6%). Verify the figure is the real print, "
                    f"label it as an expectation, or drop it."
                )
            out.append({
                "kind": "unreconciled-release-figure",
                # HARD when the figure matches the CONSENSUS on a row
                # that has already printed. That is not an ambiguous
                # stray number, it is the estimate-shipped-as-print
                # signature, and it has now shipped twice: 2026-07-15
                # (consensus 3.88%/2.86% published as the CPI print,
                # actual 3.5%/2.6%) and 2026-08-12 (0.22% and 0.12%
                # published as JPMorgan's forecasts when 0.22% was the
                # actual core print). Both times the check fired soft
                # and the pulse shipped. The generic no-match case stays
                # soft — a stray percentage in a long bullet is the
                # noisy shape (three false positives on 2026-08-07).
                "severity": "hard" if is_est_hit else "soft",
                "message": msg,
                "figure": fm.group(1),
                "event": hit_kws[0],
                "matches_estimate": is_est_hit,
                "sentence": sent.strip()[:200],
            })
    return out


def _missing_release_actual_violations(
    md_text: str, ctx: dict,
) -> list[dict]:
    """A released event the pulse discusses must carry its ACTUAL.

    2026-08-12: CPI printed at 8:30 ET and the pulse fired at 10:09 with
    the numbers in its context under a header reading "ECONOMIC EVENTS
    ALREADY RELEASED (belongs in RECAP...)". The RECAP bullet gave
    Goldman's, JPMorgan's and Deutsche Bank's forecasts and stopped. A
    reader learned what three desks expected and never learned what the
    print was, while the pulse asserted it "landed close to what the
    desks had penciled in" — core actually came in hot on both measures
    (0.22% vs 0.2% est m/m, 2.67% vs 2.5% est y/y).

    Nothing caught it. unreconciled-release-figure only fires on numbers
    that fail to reconcile; a bullet that discusses CPI and simply never
    states the result trips no check at all. This is the positive
    requirement: if it printed and we talk about it, say what it was.

    HARD. A macro recap missing its headline number is the most visible
    failure this product has, and the fix is mechanical — the value is
    already in the context.
    """
    rows = _released_event_rows(ctx)
    if not rows:
        return []
    body = md_text
    figures = {float(m.group(1)) for m in _PCT_FIGURE_RE.finditer(body)}
    up = body.upper()
    out: list[dict] = []
    for kw, slot in sorted(rows.items()):
        actual = slot.get("actual") or set()
        if not actual:
            continue          # nothing printed, or the feed lacks it
        if kw not in up:
            continue          # the pulse does not discuss this event
        # Rounding match, NOT the 0.06 tolerance the reconcile check
        # uses. That tolerance exists there to absorb sign and rounding
        # noise while hunting for a mismatch; here it would let a
        # FORECAST stand in for the print by coincidence — Deutsche
        # Bank's 0.26% estimate sits 0.04 from the 0.22% actual and
        # would have satisfied the check on the very bullet that
        # prompted it. Accept the actual written to 2dp or rounded to
        # 1dp (2.67 -> "2.7") and nothing else.
        # The 1dp fallback only applies at magnitudes where it cannot
        # collapse distinct readings: month-over-month prints live in
        # 0.0-0.5 where 0.19, 0.21 and 0.22 all round to 0.2, so a
        # forecast would pass for the print. Year-over-year prints
        # (2.67 -> "2.7") are far enough apart for it to be safe.
        if any(
            round(abs(a), 2) == round(abs(f), 2)
            or (abs(a) >= 1.0 and round(abs(a), 1) == round(abs(f), 1))
            for a in actual for f in figures
        ):
            continue          # the print reached the reader
        out.append({
            "kind": "released-actual-missing",
            "severity": "hard",
            "message": (
                f"{kw.title()} has already printed "
                f"(ACTUAL={', '.join(f'{a:g}%' for a in sorted(actual))}) "
                f"and the pulse discusses it, but none of those values "
                f"appear anywhere in the text. The 2026-08-12 CPI recap "
                f"shipped three banks' forecasts and never the print. "
                f"State the actual figure."
            ),
            "event": kw,
            "actual": sorted(actual),
        })
    return out


_CONSENSUS_COMPARE_RE = re.compile(
    r"\b(beat|miss(?:ed)?|above|below|against|versus|vs\.?|short of|"
    r"topped|in line|matched|ahead of|under(?:shot)?|over(?:shot)?|"
    r"expected|consensus|the street|wanted|looked for)\b",
    re.IGNORECASE,
)
_NO_CONSENSUS_RE = re.compile(
    r"\bno consensus\b|\bconsensus (?:figures? )?"
    r"(?:was|were)(?: not|n't) (?:posted|published|available)\b",
    re.IGNORECASE,
)
# Words too generic to identify a company from a ledger line.
_LEDGER_STOPWORDS = frozenset({
    "the", "and", "with", "before", "after", "open", "close", "reports",
    "consensus", "share", "revenue", "earnings", "morning", "wednesday",
    "thursday", "friday", "monday", "tuesday", "expected", "per",
})


def _ledger_names(ctx: dict) -> list[str]:
    """Company identifiers from the prev_consensus_block ledger lines:
    cashtags plus Capitalized words that aren't stopwords. Loose on
    purpose — a false name here only fires when the RECAP also reports
    an actual for it."""
    block = str(ctx.get("prev_consensus_block") or "")
    if block.strip().lower() in ("", "(none)", "(unavailable)"):
        return []
    names: list[str] = []
    for ln in block.splitlines():
        if not ln.strip().startswith("-"):
            continue
        names.extend(m.group(0) for m in _CASHTAG_RE.finditer(ln))
        for w in re.findall(r"\b[A-Z][a-z]{2,}(?:'s)?\b", ln):
            w = w.rstrip("'s").rstrip("'")
            if w.lower() not in _LEDGER_STOPWORDS:
                names.append(w)
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def _consensus_amnesia_violations(md_text: str, ctx: dict) -> list[dict]:
    """HARD. The 2026-08-19 failure class: the previous pulse published
    a consensus for a name; today's RECAP reports that name's actual
    print and either (a) claims no consensus exists, or (b) states the
    actual with no comparison against the expectation. The ledger comes
    in via ctx['prev_consensus_block'] (rendered from the previous
    scheduled pulse's own text), so 'no consensus' about a ledger name
    is a factual error about this product's own prior edition."""
    names = _ledger_names(ctx)
    if not names:
        return []
    m = re.search(r"^## 1\. RECAP\s*$(.*?)(?=^## )", md_text, re.M | re.S)
    recap = m.group(1) if m else md_text
    out: list[dict] = []
    # Split into bullets/paragraphs so the comparison-language test is
    # local to the sentence block reporting the print.
    blocks = [b.strip() for b in re.split(r"\n(?=- )|\n\n", recap) if b.strip()]
    reported_re = re.compile(
        r"\b(reported|printed|came in|landed|posted|delivered|earned)\b",
        re.IGNORECASE,
    )
    for b in blocks:
        hit = [n for n in names if re.search(rf"\b{re.escape(n)}\b", b)]
        if not hit:
            continue
        if _NO_CONSENSUS_RE.search(b):
            out.append({
                "kind": "consensus-amnesia",
                "severity": "hard",
                "message": (
                    f"RECAP claims no consensus for {hit[0]} but the "
                    f"previous pulse published one (see the CONSENSUS "
                    f"LEDGER). State the actual against that figure as "
                    f"a beat or a miss."
                ),
                "name": hit[0],
            })
            continue
        if (reported_re.search(b) and re.search(r"\$\d", b)
                and not _CONSENSUS_COMPARE_RE.search(b)):
            out.append({
                "kind": "consensus-amnesia",
                "severity": "hard",
                "message": (
                    f"RECAP reports {hit[0]}'s actual print but never "
                    f"frames it against the consensus the previous "
                    f"pulse published. Beat or miss is the content of "
                    f"an earnings recap — state it."
                ),
                "name": hit[0],
            })
    return out


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
    # =====================================================================
    # CHECK 6: the `## _LEANS` block exists and is non-empty
    # =====================================================================
    # The TRADE BOARD is built ONLY from `## _LEANS` (the prose-scrape
    # fallback was removed 2026-06-30 because it shipped truncated /
    # wrong-ticker rationales). A missing or empty block => the board
    # renders no leans. Hard-require it so the board is never silently
    # empty.
    # ---------------------------------------------------------------------
    n_leans = _leans_line_count(md_text)
    if not n_leans:  # None (no block) or 0 (block present but empty)
        violations.append({
            "kind": "leans-block-missing",
            "severity": "hard",
            "message": (
                "No usable `## _LEANS` block. The TRADE BOARD is built "
                "ONLY from this block, so without it the board is empty. "
                "Emit `## _LEANS` with one `- <direction> | <instruments> "
                "| <rationale>` line per trade, the MAIN EVENT's trade "
                "first."
            ),
        })

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

    # =====================================================================
    # CHECK 7: numeric-scope-drift (a cited figure re-scoped off its source)
    # =====================================================================
    # Runs over INSIGHTS sections only — RECAP carries live-market figures
    # from a different source (market_data), not the research corpus, so a
    # provenance check there would false-flag. See the helper header above
    # for the mechanism.
    # ---------------------------------------------------------------------
    source_stats = _source_stat_index(_collect_source_strings(ctx))
    if source_stats:
        violations.extend(_numeric_scope_violations(sections, source_stats))

    # =====================================================================
    # CHECK 8: weekday-vs-date consistency ("Tuesday 7/22" on a Wednesday)
    # =====================================================================
    violations.extend(_weekday_date_violations(md_text, ctx))

    # =====================================================================
    # CHECK 9: released figures reconcile against calendar ACTUAL rows
    # =====================================================================
    violations.extend(_released_figure_violations(md_text, ctx))
    violations.extend(_missing_release_actual_violations(md_text, ctx))
    violations.extend(_consensus_amnesia_violations(md_text, ctx))

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
