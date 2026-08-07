"""Format-overhaul Phase 1: deterministic WHAT CHANGED + TRADE BOARD sections.

Design (2026-06-10): the most actionable content in the corpus is already
structured (theme stances, high-conviction calls, trade leans), so these
two sections are ASSEMBLED BY CODE at bridge post time and injected into
the final pulse markdown — the LLM never touches them. Zero fabrication
surface, zero prompt complexity, perfectly consistent format.

  WHAT CHANGED  — diff of today's theme/call state vs the previous daily
                  pulse's state (new themes, dropped themes, stance flips,
                  fresh high-conviction calls). Injected after RECAP.
  TRADE BOARD   — every lean the pulse ships, tracked across days
                  (NEW / LIVE dN), rendered as a monospace block (Discord
                  embeds don't render markdown tables). Injected before
                  WHAT TO WATCH.

State lives in db.pulse_state (theme/call snapshots per context dump,
stamped by the consuming daily pulse) and db.pulse_leans (leans extracted
from the final markdown, aged out after 5 quiet days).
"""

from __future__ import annotations

import json
import logging
import re

from report.market_data import score_lean_move

log = logging.getLogger(__name__)

# US-tradable ticker shape — mirrors the Robinhood-test filters used in
# the synthesizer (drops Japan numerics like 6981, suffixed Europeans
# like GIVN.S / TPRO.MI).
_US_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")

# Lean extraction: action verb + cashtag inside an INSIGHTS slot's
# CLOSING paragraph. Mirrors scripts/pulse_lint.py's _LEAN_VERB_RE —
# duplicated here (not imported) because scripts/ is not a package the
# worker imports from.
#
# Between the verb and the cashtag, up to 4 filler tokens are allowed
# (no sentence punctuation) so real lean phrasings match:
#   "long energy through $XLE"            → XLE
#   "Long the equal-weight S&P ($RSP)"    → RSP
# The token class excludes . ! ? so a lean can't be stitched across
# sentence boundaries, and the closing-paragraph-only scope keeps the
# false-positive surface small.
_LEAN_RE = re.compile(
    r"\b(long|short|buy|sell|own|add|fade|trim)\s+"
    r"(?:[\w&'()-]+\s+){0,4}?"
    r"\(?\$([A-Za-z]{1,5})\)?\b"
    r"(?:\s+(calls|puts))?",
    re.IGNORECASE,
)

_LONG_VERBS = {"long", "buy", "own", "add"}
_SHORT_VERBS = {"short", "sell", "fade", "trim"}


# =====================================================================
# Thesis-level FLIP detection (2026-07-29 pulse feedback)
# =====================================================================
# The board's existing FLIP tag only catches SAME-ticker direction
# reversals (db.upsert_pulse_leans), so a macro view reversed through a
# DIFFERENT instrument shipped as NEW: Monday "hike risk underpriced →
# Long $UUP" became Tuesday "clean hold → Long $TLT" — a near-reversal
# of the Fed call, unlabeled.
#
# Map macro instruments to (axis, stance-when-LONG). Shorting inverts
# the stance. Two leans on the same axis with opposing stances across
# consecutive boards = a thesis flip. Deliberately narrow: only
# instruments whose macro meaning is unambiguous. Single names carry no
# axis (an NVDA call isn't a Fed view), so they never produce a flip.
_THESIS_AXIS: dict[str, tuple[str, str]] = {
    # Fed / rates path. "hawkish" = higher-for-longer / hike risk.
    "UUP": ("fed", "hawkish"), "DXY": ("fed", "hawkish"),
    "TBT": ("fed", "hawkish"), "TMV": ("fed", "hawkish"),
    "TLT": ("fed", "dovish"), "IEF": ("fed", "dovish"),
    "SHY": ("fed", "dovish"), "TMF": ("fed", "dovish"),
    "TLH": ("fed", "dovish"), "ZROZ": ("fed", "dovish"),
    # Broad risk appetite.
    "SPY": ("risk", "on"), "QQQ": ("risk", "on"), "IWM": ("risk", "on"),
    "SPX": ("risk", "on"), "NDX": ("risk", "on"), "DIA": ("risk", "on"),
    "VIX": ("risk", "off"), "UVXY": ("risk", "off"),
    "VIXY": ("risk", "off"), "VXX": ("risk", "off"),
    "SH": ("risk", "off"), "SQQQ": ("risk", "off"),
    "SPXU": ("risk", "off"), "PSQ": ("risk", "off"),
    # Crude direction.
    "USO": ("oil", "bull"), "BNO": ("oil", "bull"), "XLE": ("oil", "bull"),
    "DBO": ("oil", "bull"), "SCO": ("oil", "bear"), "DRIP": ("oil", "bear"),
}

_STANCE_OPPOSITE = {
    "hawkish": "dovish", "dovish": "hawkish",
    "on": "off", "off": "on",
    "bull": "bear", "bear": "bull",
}


def _lean_thesis(ticker: str, direction: str) -> tuple[str, str] | None:
    """(axis, stance) a lean expresses, or None when the instrument
    carries no unambiguous macro thesis. Shorting inverts the stance."""
    hit = _THESIS_AXIS.get((ticker or "").strip().upper())
    if not hit:
        return None
    axis, stance_long = hit
    if (direction or "").strip().lower() in _SHORT_VERBS:
        return axis, _STANCE_OPPOSITE[stance_long]
    return axis, stance_long


def detect_thesis_flips(
    board_rows: list[dict], today: str, prev_board_date: str | None,
) -> set[str]:
    """Instruments in TODAY's leans that reverse a macro thesis the
    PRIOR board held through a different instrument."""
    if not prev_board_date or prev_board_date >= today:
        return set()
    prior_stances: dict[str, set[str]] = {}
    for r in (board_rows or []):
        if (r.get("last_seen_date") or "") != prev_board_date:
            continue
        for tk in _row_tickers(r):
            th = _lean_thesis(tk, r.get("direction") or "")
            if th:
                prior_stances.setdefault(th[0], set()).add(th[1])
    if not prior_stances:
        return set()

    flips: set[str] = set()
    for r in (board_rows or []):
        if (r.get("last_seen_date") or today) != today:
            continue
        for tk in _row_tickers(r):
            th = _lean_thesis(tk, r.get("direction") or "")
            if not th:
                continue
            axis, stance = th
            held = prior_stances.get(axis) or set()
            # Reversal only when the prior board held the OPPOSITE
            # stance and does not also hold this one (a board carrying
            # both sides isn't reversing anything).
            if _STANCE_OPPOSITE[stance] in held and stance not in held:
                flips.add(tk.upper())
    return flips


# =====================================================================
# State extraction (called by the bridge's context-dump job)
# =====================================================================

def extract_state_from_ctx(ctx: dict) -> dict:
    """Compact state snapshot from a pulse context dict.

    Captures the top themes (label, banks, stance counts, high-conviction
    count) and the high-conviction single-name calls — the two inputs the
    WHAT CHANGED diff needs. Small by construction (~2-4 KB).
    """
    themes = []
    theme_map = ctx.get("theme_map") or {}
    ranked = sorted(
        theme_map.items(),
        key=lambda kv: (-kv[1].get("banks", 0), kv[0]),
    )
    for label, info in ranked[:15]:
        themes.append({
            "label": label,
            "banks": info.get("banks", 0),
            "sup": info.get("supportive", 0),
            "skep": info.get("skeptical", 0),
            "hc": info.get("high_conviction", 0),
            # Dissent names for the DESK SIGNAL BOARD consensus ledger.
            # Capped at 3 to keep the state snapshot small. Absent on
            # state dumped before the synthesizer started serializing it.
            "skep_sources": (info.get("skeptical_sources") or [])[:3],
        })

    hc_calls = []
    seen_calls: set[tuple[str, str]] = set()
    try:
        analyses = json.loads(ctx.get("analyses_json") or "[]")
    except Exception:
        analyses = []
    for a in analyses:
        src = a.get("source") or "?"
        for mm in (a.get("market_movers") or []):
            if (mm.get("conviction") or "").lower() != "high":
                continue
            ticker = (mm.get("ticker") or "").strip().upper()
            if not _US_TICKER_RE.match(ticker):
                continue
            # Dedup by (source, ticker) — the same call often appears in
            # multiple notes from one bank in a single day (observed:
            # Citi's STTK PT change extracted from two PDFs → two
            # near-identical WHAT CHANGED bullets).
            if (src, ticker) in seen_calls:
                continue
            # A call the reader can ACT on carries a rating (Buy/OW/UW)
            # or a price target. Entries with neither are post-hoc
            # recaps — "Earnings miss ... driving a sharp sell-off",
            # "Stock fell 18% after outlook missed" — descriptions of
            # what already happened that were shipping under the
            # "high-conviction single-name calls" banner (2026-08-04
            # review; user flagged the recap-mixing earlier).
            # 'N/A'/'NA'/'None'/'-' are extraction filler, not ratings
            # — literal 'N/A' strings are truthy and passed this filter
            # on 2026-08-05, letting three recap entries occupy capped
            # HC slots while the SPCX inaugural-earnings call got cut.
            _rating = (mm.get("rating") or "").strip()
            _pt = (mm.get("price_target") or "").strip()
            if _rating.upper() in ("N/A", "NA", "NONE", "-"):
                _rating = ""
            if _pt.upper() in ("N/A", "NA", "NONE", "-"):
                _pt = ""
            if not _rating and not _pt:
                continue
            seen_calls.add((src, ticker))
            hc_calls.append({
                "source": src,
                "ticker": ticker,
                "action": (mm.get("action") or "")[:40],
                "pt": (mm.get("price_target") or "")[:20],
                # rating + rationale power the TRADE BOARD's HC subsection.
                "rating": (mm.get("rating") or "")[:12],
                # Was [:80] — too tight, the HC rationale rendered mid-word
                # ("disclosed $100bn…", 2026-06-25 QC). Keep the full desk
                # reasoning; the render does a generous word-boundary clip.
                "rationale": (mm.get("rationale") or "")[:280],
            })

    # Rank before any cap: the render slices calls[:_HC_SUBSECTION_MAX],
    # and until 2026-08-05 that slice ran in EXTRACTION order — whatever
    # PDF happened to be analyzed first won the slots. GS's dedicated
    # note on SpaceX's first-ever earnings (Buy, PT $220, analyzed 3h
    # before the pulse) arrived 13th and was cut while catalyst-watch
    # reiterations shipped. Rank by action signal, then by having both a
    # rating and a PT. Stable sort keeps extraction order inside a tier.
    _ACTION_RANK = {
        "initiate": 0, "upgrade": 0, "downgrade": 0,
        "price_target_change": 1, "reiterate": 2,
        "positive_catalyst_watch": 3, "negative_catalyst_watch": 3,
    }
    hc_calls.sort(key=lambda c: (
        _ACTION_RANK.get((c.get("action") or "").strip().lower(), 2),
        0 if (c.get("rating") and c.get("pt")) else 1,
    ))
    return {"themes": themes, "hc_calls": hc_calls[:20]}


# =====================================================================
# WHAT CHANGED
# =====================================================================

# Theme topic-signature — for suppressing day-over-day clusterer
# RENAMES in WHAT CHANGED (a relabeled topic firing a false New+Faded
# or false lead-rotation). Light synonym folding collapses concept
# renames the clusterer produces ("artificial intelligence"~"ai
# semiconductor", "fomc easing"~"hawkish fed", "us iran relief"~"iran
# nuclear deal"). Used ONLY for low-stakes rename suppression, never for
# ranking — mild over-folding here just hides a churn bullet.
_THEME_STOP = {"first", "next", "into", "with", "from", "this", "that",
               "over", "more", "less", "than", "your", "their"}
_THEME_SYN = {
    "artificial": "ai", "intelligence": "ai", "ai": "ai",
    "semiconductor": "ai", "semiconductors": "ai", "semis": "ai",
    "chip": "ai", "chips": "ai", "silicon": "ai",
    "fed": "fed", "fomc": "fed", "federal": "fed", "warsh": "fed",
    "powell": "fed", "guidance": "fed", "hawkish": "fed", "dovish": "fed",
    "easing": "fed", "tightening": "fed",
    "hormuz": "oil", "crude": "oil", "brent": "oil", "oil": "oil",
    "opec": "oil", "energy": "oil",
    "iran": "iran", "tehran": "iran",
}


# =====================================================================
# DESK SIGNAL BOARD (format-overhaul Phase 2)
# =====================================================================

_HC_CALLS_MAX = 10
_LEDGER_THEMES_MAX = 7

# Ratings normalized to short, fixed-width tokens so the monospace
# column never mid-truncates ("Overweight" was clipping to "Overweigh",
# observed 06-17). Anything unmapped is word-boundary clipped.
_RATING_NORM = {
    "overweight": "OW", "underweight": "UW", "equal-weight": "EW",
    "equalweight": "EW", "equal weight": "EW", "market perform": "Hold",
    "outperform": "OP",
    "underperform": "UP", "buy": "Buy", "sell": "Sell", "hold": "Hold",
    "neutral": "Neutral", "add": "Add", "reduce": "Reduce",
    # UBS house scale + RBC's Hold-equivalent (2026-07-09 board shipped
    # "Most" for "Most Preferred" — the word-boundary clip kept only the
    # first word, which reads as gibberish on the board).
    "most preferred": "Most Pref", "least preferred": "Least Pref",
    "sector perform": "Hold", "sector outperform": "OP",
    "strong buy": "Strong Buy",
}

# Plain-English decode for the compact rating tokens — rendered as a
# one-line legend under the HC subsection when any of these ship
# (2026-07-15 review: OW/UW/EW reached readers with no explanation).
_RATING_LEGEND = {
    "OW": "overweight (own more than the index does)",
    "UW": "underweight (own less than the index does)",
    "EW": "equal-weight (match the index)",
    "OP": "outperform (expected to beat its sector)",
    "UP": "underperform (expected to lag its sector)",
    "PT": "price target",
}
_RATING_LEGEND_ORDER = ("OW", "UW", "EW", "OP", "UP", "PT")
# Non-USD price-target markers — calls with these are foreign-listed
# names a US options trader can't act on; they were cluttering the board
# (ZAR280, €21.50 observed 06-17). Drop them from the HC table.
_FOREIGN_PT_RE = re.compile(
    # $-suffixed symbols: NT$ (New Taiwan), S$ (Singapore), NZ$ added
    # 2026-07-06 after "$TSM PT NT$3,000" shipped (TWD ISO was covered,
    # the NT$ symbol form wasn't). Longer symbols first so NT$ isn't
    # partially eaten by a shorter alternative.
    r"(€|£|¥|₩|R\$|HK\$|NT\$|NZ\$|S\$|A\$|C\$|GBp|p$"
    # 3-letter ISO codes for non-USD currencies (observed: 'PT EUR315'
    # slipped past the symbol-only set, 2026-06-24).
    # No trailing \b — codes attach directly to digits ("EUR315",
    # "ZAR280") with no word boundary between the letter and the number.
    r"|\b(?:EUR|GBP|JPY|CHF|AUD|CAD|NZD|HKD|SGD|CNY|CNH|INR|KRW|TWD|"
    r"SEK|NOK|DKK|ZAR|BRL|MXN|RUB|TRY|PLN|THB|IDR|MYR|PHP))",
    re.IGNORECASE)


def _clean_inline(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _norm_rating(rating: str, action: str) -> str:
    """Short, clean rating token (rating preferred, action fallback).

    2026-07-09 board shipped "Improvin" (from "Improving") — the old
    8-char "word-boundary" clip was a plain slice for single words, so
    it shipped exactly the mid-word stub it claimed to prevent. Rules:
      - known ratings map to their short form (_RATING_NORM)
      - a single word ships whole (extraction caps the field at 12)
      - multi-word falls back to a word-boundary clip with an ellipsis
        so a dropped word is visible instead of silent
    """
    raw = _clean_inline(rating or action or "")
    key = raw.lower()
    if key in _RATING_NORM:
        return _RATING_NORM[key]
    if " " not in raw:
        return raw
    if len(raw) > 12:
        clipped = raw[:12].rsplit(" ", 1)[0].rstrip(" ,;:") or raw[:12]
        return clipped + "…"
    return raw


def _clip_rationale(s: str, limit: int = 40) -> str:
    """Word-boundary clip + ellipsis (never mid-word, observed 06-17)."""
    s = _clean_inline(s)
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (cut or s[:limit]) + "…"


def _is_foreign_pt(pt: str) -> bool:
    return bool(_FOREIGN_PT_RE.search(pt or ""))


# =====================================================================
# TRADE BOARD
# =====================================================================

_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
_LEANS_HDR_RE = re.compile(r"^##\s+_LEANS\b[^\n]*$", re.MULTILINE | re.IGNORECASE)
_DISPLAY_RATIONALE_LIMIT = 90


def _build_lean_display(direction: str, instruments_expr: str,
                        rationale: str) -> str:
    """Compose the board's human-readable lean line (everything after the
    status prefix). Options carry their direction in the contract type
    (calls = bullish, puts = bearish), so no Long/Short prefix; otherwise
    prefix the direction. Rationale clipped at a word boundary."""
    instruments_expr = (instruments_expr or "").strip()
    low = instruments_expr.lower()
    if low.endswith(("calls", "call", "puts", "put")):
        head = instruments_expr
    else:
        d = (direction or "").strip().capitalize()
        head = f"{d} {instruments_expr}".strip()
    rationale = re.sub(r"\s+", " ", (rationale or "")).strip(" .,;:—-")
    if len(rationale) > _DISPLAY_RATIONALE_LIMIT:
        cut = rationale[:_DISPLAY_RATIONALE_LIMIT].rsplit(" ", 1)[0].rstrip(" ,;:—-")
        rationale = (cut or rationale[:_DISPLAY_RATIONALE_LIMIT]) + "…"
    return f"{head} · {rationale}" if rationale else head


def parse_lean_block(md: str) -> list[dict]:
    """Parse the DRAFT-emitted hidden `## _LEANS` block — the STRUCTURAL
    source of the TRADE BOARD (2026-06-23). Each line is
    `- <direction> | <instruments> | <rationale>`, e.g.
    `- long | $VST, $CEG, $XLU | power and infra over chasing $SMH`.

    Returns [{instrument (primary ticker, for keying/flip), direction,
    context (the full board display line)}]. Empty when the block is
    absent — the caller then falls back to prose extraction. This is the
    robust path: the writer states the lean explicitly, so the board no
    longer guesses leans out of varied prose."""
    if not md:
        return []
    m = _LEANS_HDR_RE.search(md)
    if not m:
        return []
    body_start = m.end()
    nxt = re.search(r"^##\s", md[body_start:], re.MULTILINE)
    block = md[body_start:body_start + nxt.start()] if nxt else md[body_start:]
    leans: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith(("- ", "* ")):
            continue
        parts = [p.strip() for p in line[2:].split("|")]
        if len(parts) < 2:
            continue
        d = parts[0].lower().strip()
        if d in ("long", "buy", "bull", "bullish"):
            direction = "long"
        elif d in ("short", "sell", "bear", "bearish"):
            direction = "short"
        else:
            continue
        instruments_expr = parts[1].strip()
        rationale = parts[2].strip() if len(parts) >= 3 else ""
        tickers = _CASHTAG_RE.findall(instruments_expr)
        if not tickers:
            continue
        primary = tickers[0].upper()
        key = (primary, direction)
        if key in seen:
            continue
        seen.add(key)
        leans.append({
            "instrument": primary,
            "direction": direction,
            "context": _build_lean_display(direction, instruments_expr, rationale),
        })
    return leans


def strip_lean_block(md: str) -> str:
    """Remove the hidden `## _LEANS` block from the markdown. Called by
    the bridge AFTER the board is built and BEFORE the pulse is posted/
    archived, so the internal source never reaches Discord. Robust to a
    missing block (returns input unchanged)."""
    if not md:
        return md
    m = _LEANS_HDR_RE.search(md)
    if not m:
        return md
    start = m.start()
    nxt = re.search(r"^##\s", md[m.end():], re.MULTILINE)
    end = m.end() + nxt.start() if nxt else len(md)
    out = md[:start] + md[end:]
    return re.sub(r"\n{3,}", "\n\n", out).rstrip() + "\n"


_BOARD_CTX_LIMIT = 64
# Leading dangling connectives left after a clause split — dropping
# these turns "is the bet he doesn't…" into "the bet he doesn't…".
_LEAD_JUNK_RE = re.compile(r"^(and|but|so|or|is|are|was|were)\b\s*", re.IGNORECASE)


def _clean_board_context(ctx: str, instrument: str, direction: str) -> str:
    """Turn a stored lean sentence into a clean board descriptor.

    The fix for the 06-15 garble ("For US-listed exposure, is the bet
    the yen firms…", "$MU  The lean is , Morgan Stanley Overweight)"):
      - strip a lean phrase ONLY when the sentence OPENS with it (so a
        leading "Short $USO, with…" becomes "with…", but a mid-sentence
        lean like "Long $TLT is the bet…" is kept whole — stripping it
        mid-clause is what produced broken grammar)
      - drop leading dangling punctuation + connectives
      - collapse whitespace, clip at a WORD boundary with an ellipsis
        (never mid-word), capitalize the first letter
    Returns "" when nothing usable remains (board line shows no tail).
    """
    ctx = (ctx or "").strip()
    if not ctx:
        return ""
    lead = _LEAN_RE.match(ctx)
    if lead:
        ctx = ctx[lead.end():]
    # Strip SELF-REFERENTIAL lean phrases naming the row's OWN
    # instrument. A two-instrument lean ("Short $TLT, paired with long
    # $UUP, into PCE") staples the whole sentence onto BOTH rows, so the
    # $UUP row would read "paired with long $UUP" — describing itself.
    # Remove only verb-prefixed mentions (long/short/own $TICKER) so a
    # bare in-prose mention ("long memory ($MU, Morgan Stanley OW)")
    # stays intact.
    ticker = (instrument or "").split()[0].strip("$").upper() if instrument else ""
    if ticker:
        self_ref = re.compile(
            r",?\s*(?:paired with\s+|and\s+|plus\s+)?"
            r"(?:long|short|own|buy|sell|bought|sold)\s+\$?"
            + re.escape(ticker) + r"\b(?:\s+(?:calls?|puts?))?",
            re.IGNORECASE,
        )
        ctx = self_ref.sub("", ctx)
    ctx = ctx.lstrip(" ,;:.—-)")
    ctx = _LEAD_JUNK_RE.sub("", ctx)
    ctx = re.sub(r"\s+", " ", ctx).strip(" ,;:—-")
    if not ctx:
        return ""
    if len(ctx) > _BOARD_CTX_LIMIT:
        cut = ctx[:_BOARD_CTX_LIMIT].rsplit(" ", 1)[0].rstrip(" ,;:—-")
        ctx = (cut or ctx[:_BOARD_CTX_LIMIT]) + "…"
    return ctx[0].upper() + ctx[1:]


_BOARD_MAX_ROWS = 10  # safety cap; today's calls rarely exceed this
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fmt_since(iso: str) -> str:
    """'2026-06-17' -> 'Jun 17'. Empty on a bad date."""
    from datetime import date as _date
    try:
        d = _date.fromisoformat(iso)
        return f"{_MONTHS[d.month - 1]} {d.day}"
    except (ValueError, IndexError, TypeError):
        return ""


_HC_SUBSECTION_MAX = 6


def _render_hc_subsection(hc_calls: list[dict] | None) -> str:
    """Render the HIGH-CONVICTION single-name calls as a clean markdown
    sub-block under the TRADE BOARD (2026-06-24 — brought back from the
    retired DESK SIGNAL BOARD, per user request). Reuses the desk-board
    cleaners but renders as bullets, NOT a monospace table — fixing the
    mid-word truncation / foreign-PT / N-A noise that got the original
    cut. Empty string when there are no US-actionable HC calls."""
    # Drop foreign-PT calls AND calls with no usable ticker — the
    # extractor now blanks the ticker for foreign / collision-risk names
    # (BAE Systems, Zalando), and a tickerless call has nothing to
    # cashtag, so it has no place on a US-actionable board (2026-06-30).
    calls = [
        c for c in (hc_calls or [])
        if not _is_foreign_pt(c.get("pt") or "")
        and (c.get("ticker") or "").strip().strip("?").strip()
    ]
    if not calls:
        return ""
    lines = ["**High-conviction single-name calls** (the desks' standout bets):", ""]
    used_tokens: set[str] = set()
    for c in calls[:_HC_SUBSECTION_MAX]:
        bank = _clean_inline(c.get("source") or "?")
        tk = (c.get("ticker") or "?").upper()
        rd = _norm_rating(c.get("rating") or "", c.get("action") or "")
        if rd.upper() in ("N/A", "NA", "NONE"):
            rd = ""
        if rd.upper() in _RATING_LEGEND:
            used_tokens.add(rd.upper())
        pt_raw = _clean_inline(c.get("pt") or "")
        pt = f"PT {pt_raw}" if pt_raw and pt_raw.upper() not in ("N/A", "") else ""
        if pt:
            used_tokens.add("PT")
        tag = ", ".join(x for x in (rd, pt) if x)
        # 60 was clipping the desk reasoning mid-thought (2026-06-25 QC);
        # 170 fits a full sentence, word-boundary + ellipsis only when a
        # rationale is genuinely longer.
        rat = _clip_rationale(c.get("rationale") or "", 170)
        parts = [f"**{bank}** ${tk}"]
        if tag:
            parts.append(tag)
        if rat:
            parts.append(rat)
        lines.append("- " + " · ".join(parts))
    # Reader-facing decode for the rating shorthand (2026-07-15 review:
    # OW/UW/EW shipped with no legend — exactly the jargon the plain-
    # English rule exists to kill). One italic line, only the tokens
    # actually used above.
    legend_bits = [
        f"{tok} = {_RATING_LEGEND[tok]}"
        for tok in _RATING_LEGEND_ORDER if tok in used_tokens
    ]
    if legend_bits:
        lines.append("")
        lines.append(f"*({', '.join(legend_bits)})*")
    return "\n".join(lines) + "\n"


def _lean_display_from_row(r: dict) -> str:
    """The board display line for a lean row: the stored full display
    (context_snippet) when present, else a bare position rebuilt from
    direction + instrument (legacy rows)."""
    display = (r.get("context_snippet") or "").strip()
    if display:
        return display
    raw_inst = r.get("instrument", "?")
    low = raw_inst.lower()
    if low.endswith(("calls", "call", "puts", "put")):
        return f"${raw_inst}"
    d = (r.get("direction") or "").strip().capitalize()
    return f"{d} ${raw_inst}".strip()


def _row_tickers(r: dict) -> set[str]:
    """Every ticker a lean row involves — cashtags from the full display
    line (covers multi-instrument leans like 'Long $RSP, $XLU') plus the
    primary instrument key."""
    out = {
        t.upper() for t in _CASHTAG_RE.findall(r.get("context_snippet") or "")
    }
    prim = (r.get("instrument") or "").strip().upper()
    if prim:
        out.add(prim.split()[0])
    return out


_BOARD_DROPPED_MAX = 6


def render_trade_board(
    board_rows: list[dict], today: str, flips: set[str] | None = None,
    hc_calls: list[dict] | None = None,
    prev_board_date: str | None = None,
    reversal_counts: dict[str, int] | None = None,
) -> str:
    """Render the TRADE BOARD: the leans THIS pulse is making (clean
    markdown bullets) plus a HIGH-CONVICTION single-name calls subsection.
    Empty string when the pulse made neither.

    2026-06-22 QC: show only leans re-affirmed today (last_seen == today)
    so the board ALWAYS matches the pulse. 2026-06-24: HC calls returned
    as a subsection (from the retired DESK SIGNAL BOARD). Labels: NEW =
    first flagged today, FLIP = reversed today, "held since <date>" =
    carried from an earlier pulse and repeated today, "off board since
    <date>" = on the prior board but not repeated today (2026-07-10:
    four of five leans vanished silently; 2026-07-13 rename from DROPPED
    — that word implied a deliberate exit, but non-mention usually just
    means today's themes went elsewhere).
    """
    flips = {f.upper() for f in (flips or set())}
    # Merge in THESIS flips — the caller's set only covers same-ticker
    # direction reversals, so a macro view reversed through a different
    # instrument (Long $UUP → Long $TLT) shipped as NEW (2026-07-29).
    try:
        flips |= detect_thesis_flips(board_rows, today, prev_board_date)
    except Exception as e:
        log.info(f"thesis-flip detection failed (non-fatal): {e}")
    # Leans: only what TODAY's pulse actually says — last seen today.
    rows = [r for r in (board_rows or [])
            if (r.get("last_seen_date") or today) == today]
    lean_lines = []
    for r in rows[:_BOARD_MAX_ROWS]:
        first = r.get("first_seen_date") or today
        is_new = first == today
        inst_name = (r.get("instrument") or "?").upper()
        ticker0 = inst_name.split()[0]
        if is_new and ticker0 in flips:
            status = "FLIP"
            # Churn honesty (2026-08-04 review): the SMH/SOXX complex
            # flipped five times in seven sessions and every FLIP
            # rendered as if it were the first. A repeat reversal names
            # its count so a follower sees the chop, not just today's
            # conviction. First flips stay clean.
            n = (reversal_counts or {}).get(ticker0, 0)
            if n >= 2:
                ord_sfx = {1: "st", 2: "nd", 3: "rd"}.get(
                    n if n < 20 else n % 10, "th")
                status = (
                    f"FLIP ({n}{ord_sfx} reversal in 10 sessions, "
                    f"fast tape, size accordingly)"
                )
        elif is_new:
            status = "NEW"
        else:
            since = _fmt_since(first)
            status = f"held since {since}" if since else "held"
        lean_lines.append(f"- **{status}** {_lean_display_from_row(r)}")

    # DROPPED lines — leans that were on the IMMEDIATELY-PRIOR board and
    # didn't make today's. `prev_board_date` is the previous SCHEDULED
    # pulse date (the bridge passes db.get_prev_scheduled_pulse_date) —
    # it can't be derived from the rows themselves: re-affirmed leans
    # carry last_seen == today, leaving no trace of yesterday, so a
    # max(last_seen < today) heuristic re-drops the same lean every day
    # until age-out (caught by smoke_board_dropped's retire test). With
    # the real date, each drop renders exactly once and a bridge retry
    # is idempotent. Rows sharing any ticker with a lean affirmed today
    # are skipped — those are FLIPs (already labeled above) or partial
    # re-affirmations, not abandonments. Only renders when today has
    # leans at all: a missing _LEANS block is a validator failure, not a
    # mass abandonment.
    if lean_lines and prev_board_date and prev_board_date < today:
        today_tickers: set[str] = set()
        for r in rows:
            today_tickers |= _row_tickers(r)
        n_dropped = 0
        for r in (board_rows or []):
            if (r.get("last_seen_date") or "") != prev_board_date:
                continue
            if _row_tickers(r) & today_tickers:
                continue
            if n_dropped >= _BOARD_DROPPED_MAX:
                break
            # "off board since <date>" not "DROPPED" (2026-07-13 user
            # feedback): DROPPED implied the author deliberately exited
            # the call, but mechanically this only means "not repeated
            # today" — often just today's themes going elsewhere. The
            # label states the observation; a deliberate reversal shows
            # as FLIP.
            _since = _fmt_since(prev_board_date)
            _tag = f"off board since {_since}" if _since else "off board"
            # Score the exit (2026-07-29 feedback): a lean that leaves
            # the board silently is the credibility hole — "off board"
            # was doing the work "stopped out, here's the damage" should
            # do (Long $BNO ate a 6% oil crash and just vanished).
            # Direction-aware, from the lean's OWN first_seen date.
            # Failure is non-fatal: the board must ship regardless.
            _outcome = ""
            try:
                _tk = (r.get("instrument") or "").upper().split()[0]
                _first_seen = r.get("first_seen_date") or prev_board_date
                if _US_TICKER_RE.match(_tk) and _first_seen:
                    _sc = score_lean_move(
                        _tk, r.get("direction") or "", _first_seen
                    )
                    if _sc:
                        _outcome = f" · {_sc}"
            except Exception as e:
                log.info(f"trade-board exit scoring failed (non-fatal): {e}")
            lean_lines.append(
                f"- **{_tag}** {_lean_display_from_row(r)}{_outcome}"
            )
            n_dropped += 1

    hc_block = _render_hc_subsection(hc_calls)
    if not lean_lines and not hc_block:
        return ""

    out = "## TRADE BOARD\n\n"
    if lean_lines:
        out += (
            "Leans this pulse is making.\n\n"
            "**NEW** first flagged today. **FLIP** reverses a view this "
            "board held, same ticker or the same macro call expressed "
            "another way. **held since …** carried from an earlier "
            "pulse and repeated today. **off board since …** was on the "
            "last board, not repeated today; the move since it was "
            "flagged is scored where price data exists.\n\n"
            + "\n".join(lean_lines) + "\n"
        )
    if hc_block:
        out += ("\n" if lean_lines else "") + hc_block
    return out


# =====================================================================
# Injection
# =====================================================================

def inject_sections(markdown: str, board_md: str) -> str:
    """Insert the TRADE BOARD before WHAT TO WATCH. Idempotent: a board
    already present (bridge retry) is not inserted twice.

    WHAT CHANGED and DESK SIGNAL BOARD were removed 2026-06-19 (read as
    debug telemetry / duplicated the prose). The TRADE BOARD is the one
    deterministic section kept — cross-day position accountability — and
    it anchors before WHAT TO WATCH so the final order (after the MAIN
    EVENT/BRIEFS split) reads RECAP → MAIN EVENT → BRIEFS → TRADE BOARD
    → WHAT TO WATCH.
    """
    out = markdown
    if board_md and "## TRADE BOARD" not in out:
        m = re.search(r"^##\s+(?:\d+\.\s+)?WHAT TO WATCH", out, re.MULTILINE | re.IGNORECASE)
        if m:
            out = out[:m.start()] + board_md + "\n" + out[m.start():]
        else:
            out = out.rstrip() + "\n\n" + board_md
    return out


# =====================================================================
# MAIN EVENT + BRIEFS split (format-overhaul Phase 3)
# =====================================================================

def split_main_event_briefs(markdown: str) -> str:
    """Deterministically split the INSIGHTS section into THE MAIN EVENT
    (the lead theme) + BRIEFS (the rest).

    Runs at bridge post-time AFTER inject_sections, so the DRAFT / AUDIT
    / lint / validator machinery upstream all keep operating on the
    single `## 2. INSIGHTS & ALPHA` header they were tuned for — only
    the final rendered markdown carries the new labels. The DRAFT prompt
    writes the lead theme deep and the rest compressed; this function
    just relabels: the first `### ` slot becomes THE MAIN EVENT, the
    remaining slots become BRIEFS, and WHAT TO WATCH is renumbered.

    Idempotent (a retry that already has `## ... THE MAIN EVENT` is a
    no-op) and defensive (an INSIGHTS section with <2 H3 slots renames
    to THE MAIN EVENT without spinning an empty BRIEFS section; with 0
    slots it is left untouched).
    """
    if not markdown or re.search(
        r"^##\s+(?:\d+\.\s+)?THE MAIN EVENT", markdown, re.MULTILINE | re.IGNORECASE
    ):
        return markdown

    hdr = re.search(
        r"^(##[ \t]+)(?:(\d+)\.[ \t]+)?INSIGHTS[^\n]*$",
        markdown, re.MULTILINE | re.IGNORECASE,
    )
    if not hdr:
        return markdown

    sec_num = hdr.group(2) or "2"
    body_start = hdr.end()
    nxt = re.search(r"^##[ \t]", markdown[body_start:], re.MULTILINE)
    sec_end = body_start + nxt.start() if nxt else len(markdown)

    body = markdown[body_start:sec_end]
    slot_starts = [m.start() for m in re.finditer(r"^###[ \t]", body, re.MULTILINE)]
    if not slot_starts:
        return markdown  # nothing slot-shaped to split; leave as-is

    new_header = f"{hdr.group(1)}{sec_num}. THE MAIN EVENT"
    briefs_num = str(int(sec_num) + 1)

    if len(slot_starts) >= 2:
        cut = slot_starts[1]
        new_body = (
            body[:cut].rstrip()
            + f"\n\n##{' '}{briefs_num}. BRIEFS\n\n"
            + body[cut:].lstrip("\n")
        )
    else:
        new_body = body  # single theme — MAIN EVENT only, no BRIEFS

    out = markdown[:hdr.start()] + new_header + new_body + markdown[sec_end:]

    # Renumber WHAT TO WATCH to follow BRIEFS (was N+1, now N+2 when a
    # BRIEFS section was spun). Only touch the number, never the label.
    if len(slot_starts) >= 2:
        watch_num = str(int(sec_num) + 2)
        out = re.sub(
            r"^(##[ \t]+)\d+\.([ \t]+WHAT TO WATCH)",
            rf"\g<1>{watch_num}.\g<2>",
            out, count=1, flags=re.MULTILINE | re.IGNORECASE,
        )
    return out


def replace_body_after_frontmatter(raw: str, new_body: str) -> str:
    """Rebuild a frontmattered document with a new body, preserving the
    original frontmatter block verbatim. If no frontmatter, returns the
    new body as-is."""
    s = raw.lstrip()
    if s.startswith("---"):
        end = s.find("\n---", 3)
        if end != -1:
            fm = s[:end + 4]
            return fm + "\n\n" + new_body.lstrip()
    return new_body
