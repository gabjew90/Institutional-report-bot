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
                # rating + rationale power the DESK SIGNAL BOARD HC table.
                "rating": (mm.get("rating") or "")[:12],
                "rationale": (mm.get("rationale") or "")[:80],
            })

    return {"themes": themes, "hc_calls": hc_calls[:20]}


# =====================================================================
# WHAT CHANGED
# =====================================================================

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
      - fresh high-conviction calls (new source+ticker pairs)
      - new multi-bank themes entering
      - themes dropping out of the top tier

    `lean_flips`: [{instrument, from, to}] from db.upsert_pulse_leans —
    instrument-level direction reversals detected today.
    `body_tickers`: cashtags actually present in the pulse body. When
    given, fresh-HC-call bullets are suppressed unless their ticker
    appears in the body — a WHAT CHANGED line citing a ticker the pulse
    never discusses (observed 06-15: $GALP, $WULF, $CIFR) is noise, not
    signal. The pulse's own HIGH-CONVICTION surfacing decides what's
    body-worthy; WHAT CHANGED should reflect changes to what the pulse
    actually says.
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
    # doesn't fire daily.
    prev_list = prev_state.get("themes") or []
    today_list = today_state.get("themes") or []
    if prev_list and today_list:
        prev_lead = prev_list[0].get("label")
        today_lead_t = today_list[0]
        today_lead = today_lead_t.get("label")
        if (today_lead and prev_lead and today_lead != prev_lead
                and today_lead_t.get("banks", 0) >= 3):
            bullets.append(
                f"**Lead theme:** {today_lead} now top "
                f"({today_lead_t.get('banks', 0)} banks), was {prev_lead}"
            )

    # Fresh high-conviction calls — (source, ticker) pairs new today,
    # filtered to tickers the pulse body actually mentions.
    prev_calls = {(c.get("source"), c.get("ticker"))
                  for c in (prev_state.get("hc_calls") or [])}
    for c in (today_state.get("hc_calls") or []):
        if (c.get("source"), c.get("ticker")) in prev_calls:
            continue
        ticker = (c.get("ticker") or "").upper()
        if body_tickers is not None and ticker not in body_tickers:
            continue
        pt = f" PT {c['pt']}" if c.get("pt") and c["pt"].upper() not in ("N/A", "") else ""
        bullets.append(
            f"**Fresh high-conviction:** {c['source']} on "
            f"${ticker} ({c.get('action', '?')}{pt})"
        )

    # New multi-bank themes (>=3 banks, absent yesterday).
    for label, t in today_themes.items():
        if t.get("banks", 0) >= 3 and label not in prev_themes:
            bullets.append(
                f"**New theme:** {label} ({t['banks']} banks)"
            )

    # Dropped themes — yesterday's top-6, gone today.
    prev_top6 = [t["label"] for t in (prev_state.get("themes") or [])[:6]]
    for label in prev_top6:
        if label not in today_themes:
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


def _clean_inline(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


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
    hc_calls = today_state.get("hc_calls") or []
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
            # rating preferred, fall back to action verb
            rd = _clean_inline(c.get("rating") or c.get("action") or "")[:9]
            pt_raw = _clean_inline(c.get("pt") or "")
            pt = (f"PT {pt_raw}" if pt_raw and pt_raw.upper() not in ("N/A", "")
                  else "")[:11]
            rat = _clean_inline(c.get("rationale") or "")[:42]
            lines.append(f"  {src:<13} {tk:<7} {rd:<9} {pt:<11} {rat}".rstrip())
        if len(hc_calls) > _HC_CALLS_MAX:
            lines.append(f"  …+{len(hc_calls) - _HC_CALLS_MAX} more")

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
            tail = "" if skep else " · no dissent"
            lines.append(
                f"  {label} — {sup} bull / {skep} bear · {banks} banks"
                f"{hc_str}{tail}"
            )
            if skep:
                names = ", ".join(t.get("skep_sources") or [])
                lines.append(
                    f"     └ dissent: {names}" if names
                    else f"     └ dissent: {skep} desk(s)"
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
            # Context = the sentence containing the lean, clipped.
            sentences = re.split(r"(?<=[.!?])\s+", last_para)
            context = next(
                (s for s in sentences if lm.group(0) in s), last_para
            )
            leans.append({
                "instrument": instrument,
                "direction": direction,
                "context": context.strip()[:160],
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
    ctx = ctx.lstrip(" ,;:.—-)")
    ctx = _LEAD_JUNK_RE.sub("", ctx)
    ctx = re.sub(r"\s+", " ", ctx).strip(" ,;:—-")
    if not ctx:
        return ""
    if len(ctx) > _BOARD_CTX_LIMIT:
        cut = ctx[:_BOARD_CTX_LIMIT].rsplit(" ", 1)[0].rstrip(" ,;:—-")
        ctx = (cut or ctx[:_BOARD_CTX_LIMIT]) + "…"
    return ctx[0].upper() + ctx[1:]


def render_trade_board(
    board_rows: list[dict], today: str, flips: set[str] | None = None
) -> str:
    """Render the TRADE BOARD as a monospace block (Discord embeds do
    not render markdown tables). Empty string when no live leans.

    `flips`: instruments whose direction reversed today — shown as FLIP
    instead of NEW so the reversal reads at a glance.
    """
    if not board_rows:
        return ""
    from datetime import date as _date
    flips = {f.upper() for f in (flips or set())}
    lines = []
    for r in board_rows:
        first = r.get("first_seen_date") or today
        is_new = first == today
        try:
            d_first = _date.fromisoformat(first)
            d_today = _date.fromisoformat(today)
            days = (d_today - d_first).days + 1
        except ValueError:
            days = 1
        inst_name = (r.get("instrument") or "?").upper()
        if is_new and inst_name.split()[0] in flips:
            status = "FLIP"
        elif is_new:
            status = "NEW "
        else:
            status = f"d{min(days, 99):<3}"
        direction = (r.get("direction") or "?").upper()
        inst = f"${r.get('instrument', '?')}"
        ctx = _clean_board_context(
            r.get("context_snippet") or "", inst_name, direction
        )
        lines.append(f"{status} {direction:<5} {inst:<11} {ctx}".rstrip())
    return (
        "## TRADE BOARD\n\n"
        "Leans the pulse is carrying (NEW = opened today, FLIP = "
        "reversed today, dN = day N live; leans age off after 5 quiet "
        "days):\n\n"
        "```\n"
        + "\n".join(lines)
        + "\n```\n"
    )


# =====================================================================
# Injection
# =====================================================================

def inject_sections(
    markdown: str,
    what_changed_md: str,
    board_md: str,
    desk_signal_md: str = "",
) -> str:
    """Insert the deterministic sections in their target order:
    WHAT CHANGED → DESK SIGNAL BOARD before INSIGHTS (end of RECAP),
    TRADE BOARD before WHAT TO WATCH. Idempotent: a header already
    present in the markdown (bridge retry) is not inserted twice.

    WHAT CHANGED and DESK SIGNAL BOARD both anchor before the INSIGHTS
    header; DESK SIGNAL is inserted AFTER WHAT CHANGED so the final
    order reads RECAP → WHAT CHANGED → DESK SIGNAL BOARD → INSIGHTS.
    """
    out = markdown

    if what_changed_md and "## WHAT CHANGED" not in out:
        # Insert immediately before the INSIGHTS header (end of RECAP).
        m = re.search(r"^##\s+(?:\d+\.\s+)?INSIGHTS", out, re.MULTILINE | re.IGNORECASE)
        if m:
            out = out[:m.start()] + what_changed_md + "\n" + out[m.start():]
        else:
            out = out.rstrip() + "\n\n" + what_changed_md

    if desk_signal_md and "## DESK SIGNAL BOARD" not in out:
        # Anchor before INSIGHTS too; since WHAT CHANGED is already in
        # place above the INSIGHTS header, inserting here lands DESK
        # SIGNAL between WHAT CHANGED and INSIGHTS.
        m = re.search(r"^##\s+(?:\d+\.\s+)?INSIGHTS", out, re.MULTILINE | re.IGNORECASE)
        if m:
            out = out[:m.start()] + desk_signal_md + "\n" + out[m.start():]
        else:
            out = out.rstrip() + "\n\n" + desk_signal_md

    if board_md and "## TRADE BOARD" not in out:
        m = re.search(r"^##\s+(?:\d+\.\s+)?WHAT TO WATCH", out, re.MULTILINE | re.IGNORECASE)
        if m:
            out = out[:m.start()] + board_md + "\n" + out[m.start():]
        else:
            out = out.rstrip() + "\n\n" + board_md

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
