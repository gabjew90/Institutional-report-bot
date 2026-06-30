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


def _theme_sig(label: str) -> set[str]:
    toks: set[str] = set()
    for w in re.findall(r"[a-z]+", (label or "").lower()):
        if w in _THEME_SYN:
            toks.add(_THEME_SYN[w])
        elif len(w) >= 4 and w not in _THEME_STOP:
            toks.add(w)
    return toks


def compute_what_changed(
    prev_state: dict | None,
    today_state: dict,
    *,
    lean_flips: list[dict] | None = None,
    body_tickers: set[str] | None = None,
) -> list[str]:
    """Diff two state snapshots into WHAT CHANGED bullets (max 6).

    Categories, in priority order:
      - lean stance flips (we held X long, now short — the loudest signal;
        these come from the lean tracker, not the theme snapshot, so they
        catch the actual trade-direction reversal the reader most needs)
      - stance flips on recurring themes
      - lead-theme change (the dominant story rotated)
      - new multi-bank themes entering (minus the lead, to avoid dup)
      - themes dropping out of the top tier

    DELTAS ONLY. High-conviction calls are deliberately NOT listed here —
    they are owned by the DESK SIGNAL BOARD's HIGH-CONVICTION CALLS
    table. WHAT CHANGED is the day-over-day change; the calls roster is
    the board's current-state job. Listing both duplicated every call
    across two sections (observed 06-17).

    `lean_flips`: [{instrument, from, to}] from db.upsert_pulse_leans —
    instrument-level direction reversals detected today.
    `body_tickers`: accepted for backward-compatible call sites; no
    longer used now that fresh-HC bullets moved to the DESK SIGNAL BOARD.
    """
    if not prev_state:
        return []

    bullets: list[str] = []
    prev_themes = {t["label"]: t for t in (prev_state.get("themes") or [])}
    today_themes = {t["label"]: t for t in (today_state.get("themes") or [])}

    # Lean stance flips — highest priority. A trade we carried long that
    # is now short (or vice-versa) is the single most important "what
    # changed" for a reader following the leans day to day.
    for f in (lean_flips or []):
        inst = (f.get("instrument") or "").upper()
        frm = (f.get("from") or "").lower()
        to = (f.get("to") or "").lower()
        if inst and frm and to:
            bullets.append(f"**Flipped:** ${inst} {frm} → {to}")

    # Stance flips — net direction reversed on a theme present both days.
    for label, t in today_themes.items():
        p = prev_themes.get(label)
        if not p:
            continue
        prev_net = p.get("sup", 0) - p.get("skep", 0)
        today_net = t.get("sup", 0) - t.get("skep", 0)
        if prev_net * today_net < 0 and (
            abs(prev_net) >= 1 and abs(today_net) >= 1
        ):
            prev_dir = "supportive" if prev_net > 0 else "skeptical"
            today_dir = "supportive" if today_net > 0 else "skeptical"
            bullets.append(
                f"**Stance flip:** {label} — desks were net {prev_dir} "
                f"yesterday, net {today_dir} today"
            )

    # Lead-theme change — the dominant story rotated. Gated on the new
    # lead carrying >=3 banks so a trivial reshuffle of one-off themes
    # doesn't fire daily. Tracked so the New-theme block below doesn't
    # ALSO emit it (it was being listed twice — once as lead-change,
    # once as new — observed 06-17).
    lead_label = None
    prev_list = prev_state.get("themes") or []
    today_list = today_state.get("themes") or []
    if prev_list and today_list:
        prev_lead = prev_list[0].get("label")
        today_lead_t = today_list[0]
        today_lead = today_lead_t.get("label")
        # Only a GENUINE rotation — not a clusterer rename of the same
        # topic (06-17: "hormuz reopening oil price impact" -> "strait of
        # hormuz reopening" fired a false lead change). Shared topic
        # signature => rename => skip.
        if (today_lead and prev_lead and today_lead != prev_lead
                and today_lead_t.get("banks", 0) >= 3
                and not (_theme_sig(today_lead) & _theme_sig(prev_lead))):
            lead_label = today_lead
            bullets.append(
                f"**Lead theme:** {today_lead} now top "
                f"({today_lead_t.get('banks', 0)} banks), was {prev_lead}"
            )

    # NOTE: high-conviction calls are NOT listed here — they are owned by
    # the DESK SIGNAL BOARD's HIGH-CONVICTION CALLS table. Listing them
    # in WHAT CHANGED too duplicated every call across two sections
    # (observed 06-17: $AVGO in both). WHAT CHANGED is the day-over-day
    # DELTA; the calls roster is the board's job.

    # New multi-bank themes (>=3 banks, absent yesterday). Collapsed
    # into ONE bullet — every new theme also appears in the DESK SIGNAL
    # CONSENSUS LEDGER right below, so four separate "New theme:" lines
    # read as a duplicate of the ledger (observed 06-17). One compact
    # "New themes: A (9), B (8)" line keeps the delta without mirroring.
    # Skip the lead theme (already reported as the lead-change above).
    # A today-theme is a RENAME (not new) if it shares >=2 topic tokens
    # with a prior theme — conservative threshold so a genuinely-new
    # theme that merely shares one common token ("ai"/"fed"/"oil") still
    # counts as new, but a strong relabel ("Warsh press conference" ->
    # "termination of forward guidance by ... Warsh", 3 shared) is
    # suppressed. Faded uses a looser 1-token rule (low stakes) above.
    prev_sigs = [_theme_sig(lbl) for lbl in prev_themes]

    def _is_rename(label: str) -> bool:
        sig = _theme_sig(label)
        return any(len(sig & ps) >= 2 for ps in prev_sigs)

    new_themes = [
        (label, t.get("banks", 0))
        for label, t in today_themes.items()
        if (t.get("banks", 0) >= 3 and label not in prev_themes
            and label != lead_label and not _is_rename(label))
    ]
    new_themes.sort(key=lambda x: -x[1])
    if len(new_themes) == 1:
        bullets.append(
            f"**New theme:** {new_themes[0][0]} ({new_themes[0][1]} banks)"
        )
    elif new_themes:
        listed = ", ".join(f"{lbl} ({n})" for lbl, n in new_themes[:4])
        bullets.append(f"**New themes:** {listed}")

    # Dropped themes — yesterday's top-6, gone today. SUPPRESS renames:
    # the clusterer often relabels the same topic day-over-day
    # ("us iran geopolitical relief" -> "iran nuclear deal viability",
    # "ai growth" -> "ai semiconductor infrastructure"), which otherwise
    # fires a spurious New + Faded pair for one unchanged story
    # (observed 06-18: 4 new + 4 faded, all renames). A faded theme that
    # shares a topic signature with any current theme is a rename, not a
    # real exit — drop it.
    today_sigs = [_theme_sig(t["label"]) for t in (today_state.get("themes") or [])]
    prev_top6 = [t["label"] for t in (prev_state.get("themes") or [])[:6]]
    for label in prev_top6:
        if label in today_themes:
            continue
        sig = _theme_sig(label)
        if sig and any(sig & ts for ts in today_sigs):
            continue  # renamed, not faded
        bullets.append(f"**Faded:** {label} (no longer in coverage)")

    return bullets[:6]


def render_what_changed(bullets: list[str]) -> str:
    """Render the WHAT CHANGED section. Empty string when nothing to say
    (first run, or a genuinely unchanged day) — no empty-section noise."""
    if not bullets:
        return ""
    return (
        "## WHAT CHANGED\n\n"
        + "\n".join(f"- {b}" for b in bullets)
        + "\n"
    )


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
    "equalweight": "EW", "market perform": "Hold", "outperform": "OP",
    "underperform": "UP", "buy": "Buy", "sell": "Sell", "hold": "Hold",
    "neutral": "Neutral", "add": "Add", "reduce": "Reduce",
}
# Non-USD price-target markers — calls with these are foreign-listed
# names a US options trader can't act on; they were cluttering the board
# (ZAR280, €21.50 observed 06-17). Drop them from the HC table.
_FOREIGN_PT_RE = re.compile(
    r"(€|£|¥|₩|R\$|HK\$|A\$|C\$|GBp|p$"
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
    """Short, clean rating token (rating preferred, action fallback)."""
    raw = _clean_inline(rating or action or "")
    key = raw.lower()
    if key in _RATING_NORM:
        return _RATING_NORM[key]
    # word-boundary clip to 8 so we never ship a mid-word stub
    if len(raw) > 8:
        raw = raw[:8].rsplit(" ", 1)[0] or raw[:8]
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


def render_desk_signal_board(today_state: dict | None) -> str:
    """Render the DESK SIGNAL BOARD — two deterministic sub-blocks built
    from the stamped pulse state (no LLM, no fabrication surface):

      HIGH-CONVICTION CALLS — every high-conviction single-name call
        extracted from the corpus (source, ticker, rating/action, PT,
        one-line rationale).
      CONSENSUS LEDGER — multi-bank themes with their bull/bear split,
        bank count, high-conviction count, and named dissent.

    Returns "" when the state carries neither (nothing to show — no
    empty-section noise). Monospace block: Discord embeds don't render
    markdown tables.
    """
    if not today_state:
        return ""
    # Drop foreign-listed calls (non-USD price targets) — a US options
    # trader can't act on them and they cluttered the board (06-17).
    hc_calls = [c for c in (today_state.get("hc_calls") or [])
                if not _is_foreign_pt(c.get("pt") or "")]
    themes = [t for t in (today_state.get("themes") or [])
              if t.get("banks", 0) >= 2]
    if not hc_calls and not themes:
        return ""

    lines: list[str] = []

    if hc_calls:
        lines.append("HIGH-CONVICTION CALLS")
        for c in hc_calls[:_HC_CALLS_MAX]:
            src = _clean_inline(c.get("source") or "?")[:13]
            tk = f"${(c.get('ticker') or '?').upper()}"[:7]
            rd = _norm_rating(c.get("rating") or "", c.get("action") or "")[:8]
            pt_raw = _clean_inline(c.get("pt") or "")
            pt = (f"PT {pt_raw}" if pt_raw and pt_raw.upper() not in ("N/A", "")
                  else "")[:11]
            rat = _clip_rationale(c.get("rationale") or "")
            lines.append(f"  {src:<13} {tk:<7} {rd:<8} {pt:<11} {rat}".rstrip())
        if len(hc_calls) > _HC_CALLS_MAX:
            lines.append(f"  ...+{len(hc_calls) - _HC_CALLS_MAX} more")

    if themes:
        if lines:
            lines.append("")
        lines.append("CONSENSUS LEDGER")
        for t in themes[:_LEDGER_THEMES_MAX]:
            label = _clean_inline(t.get("label") or "?")
            sup = t.get("sup", 0)
            skep = t.get("skep", 0)
            banks = t.get("banks", 0)
            hc = t.get("hc", 0)
            hc_str = f" · {hc} HC" if hc else ""
            if not sup and not skep:
                # All-neutral coverage: "0 bull / 0 bear" read as empty
                # (observed 06-17). Say what it actually is.
                lines.append(
                    f"  {label} — {banks} banks tracking · no directional lean"
                    f"{hc_str}"
                )
                continue
            tail = "" if skep else " · no dissent"
            lines.append(
                f"  {label} — {sup} bull / {skep} bear · {banks} banks"
                f"{hc_str}{tail}"
            )
            if skep:
                names = ", ".join(t.get("skep_sources") or [])
                lines.append(
                    f"     dissent: {names}" if names
                    else f"     dissent: {skep} desk(s)"
                )

    if not lines:
        return ""
    return (
        "## DESK SIGNAL BOARD\n\n"
        "```\n"
        + "\n".join(lines)
        + "\n```\n"
    )


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
    return f"{head} — {rationale}" if rationale else head


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


def extract_leans_from_markdown(md: str) -> list[dict]:
    """Pull trade leans from the INSIGHTS slots' CLOSING paragraphs.

    Only the last paragraph of each H3 slot counts (mid-body ticker
    mentions are evidence, not leans) — same boundary rule as the
    slot-lean-overlap lint. Returns [{instrument, direction, context}].
    """
    m = re.search(
        r"##\s+(?:\d+\.\s+)?INSIGHTS.*?(?=^##\s|\Z)",
        md, re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return []
    insights = m.group(0)
    slot_starts = [s.start() for s in re.finditer(r"^###\s+", insights, re.MULTILINE)]
    if not slot_starts:
        return []
    slot_starts.append(len(insights))

    leans: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i in range(len(slot_starts) - 1):
        body = insights[slot_starts[i]:slot_starts[i + 1]]
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        if not paragraphs:
            continue
        last_para = paragraphs[-1]
        for lm in _LEAN_RE.finditer(last_para):
            verb = lm.group(1).lower()
            ticker = lm.group(2).upper()
            qualifier = (lm.group(3) or "").lower()
            direction = "long" if verb in _LONG_VERBS else "short"
            # 'puts' flips an ostensibly-long verb to a short expression
            # ("own $TLT puts" = short rates... actually long TLT puts =
            # short TLT). Encode the instrument as the option when
            # qualified so the board reads naturally.
            instrument = ticker
            if qualifier:
                instrument = f"{ticker} {qualifier}"
                if qualifier == "puts" and direction == "long":
                    direction = "short"
            key = (instrument, direction)
            if key in seen:
                continue
            seen.add(key)
            # Context = the sentence containing the lean, cleaned into a
            # full board display line (so render is uniform with the
            # structured _LEANS path — render just prefixes the status).
            sentences = re.split(r"(?<=[.!?])\s+", last_para)
            sentence = next(
                (s for s in sentences if lm.group(0) in s), last_para
            )
            rationale = _clean_board_context(sentence, instrument, direction)
            instruments_expr = f"${ticker}" + (f" {qualifier}" if qualifier else "")
            display = _build_lean_display(direction, instruments_expr, rationale)
            leans.append({
                "instrument": instrument,
                "direction": direction,
                "context": display,
            })
    # When both a bare ticker and its options-qualified form were
    # extracted ("$TLT" + "$TLT calls" from "own protection in $TLT …
    # Long $TLT calls"), keep only the qualified lean — it's the more
    # specific expression of the same idea.
    qualified_roots = {
        l["instrument"].split()[0] for l in leans
        if " " in l["instrument"]
    }
    leans = [
        l for l in leans
        if " " in l["instrument"] or l["instrument"] not in qualified_roots
    ]
    return leans


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
    for c in calls[:_HC_SUBSECTION_MAX]:
        bank = _clean_inline(c.get("source") or "?")
        tk = (c.get("ticker") or "?").upper()
        rd = _norm_rating(c.get("rating") or "", c.get("action") or "")
        if rd.upper() in ("N/A", "NA", "NONE"):
            rd = ""
        pt_raw = _clean_inline(c.get("pt") or "")
        pt = f"PT {pt_raw}" if pt_raw and pt_raw.upper() not in ("N/A", "") else ""
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
        lines.append("- " + " — ".join(parts))
    return "\n".join(lines) + "\n"


def render_trade_board(
    board_rows: list[dict], today: str, flips: set[str] | None = None,
    hc_calls: list[dict] | None = None,
) -> str:
    """Render the TRADE BOARD: the leans THIS pulse is making (clean
    markdown bullets) plus a HIGH-CONVICTION single-name calls subsection.
    Empty string when the pulse made neither.

    2026-06-22 QC: show only leans re-affirmed today (last_seen == today)
    so the board ALWAYS matches the pulse. 2026-06-24: HC calls returned
    as a subsection (from the retired DESK SIGNAL BOARD). Labels: NEW =
    first flagged today, FLIP = reversed today, "held since <date>" =
    carried from an earlier pulse and repeated today.
    """
    flips = {f.upper() for f in (flips or set())}
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
        elif is_new:
            status = "NEW"
        else:
            since = _fmt_since(first)
            status = f"held since {since}" if since else "held"
        # context_snippet IS the full display line (built upstream by
        # parse_lean_block or extract_leans_from_markdown) — render just
        # prefixes the status. Fall back to a bare position if a legacy
        # row stored no display.
        display = (r.get("context_snippet") or "").strip()
        if not display:
            raw_inst = r.get("instrument", "?")
            low = raw_inst.lower()
            if low.endswith(("calls", "call", "puts", "put")):
                display = f"${raw_inst}"
            else:
                d = (r.get("direction") or "").strip().capitalize()
                display = f"{d} ${raw_inst}".strip()
        lean_lines.append(f"- **{status}** {display}")

    hc_block = _render_hc_subsection(hc_calls)
    if not lean_lines and not hc_block:
        return ""

    out = "## TRADE BOARD\n\n"
    if lean_lines:
        out += (
            "Leans this pulse is making — NEW = first flagged today, FLIP "
            "= reversed today, \"held since …\" = carried from an earlier "
            "pulse and repeated today:\n\n"
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
