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
            })

    return {"themes": themes, "hc_calls": hc_calls[:20]}


# =====================================================================
# WHAT CHANGED
# =====================================================================

def compute_what_changed(prev_state: dict | None, today_state: dict) -> list[str]:
    """Diff two state snapshots into WHAT CHANGED bullets (max 6).

    Categories, in priority order:
      - stance flips on recurring themes (the trust-killer when silent)
      - fresh high-conviction calls (new source+ticker pairs)
      - new multi-bank themes entering
      - themes dropping out of the top tier
    """
    if not prev_state:
        return []

    bullets: list[str] = []
    prev_themes = {t["label"]: t for t in (prev_state.get("themes") or [])}
    today_themes = {t["label"]: t for t in (today_state.get("themes") or [])}

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

    # Fresh high-conviction calls — (source, ticker) pairs new today.
    prev_calls = {(c.get("source"), c.get("ticker"))
                  for c in (prev_state.get("hc_calls") or [])}
    for c in (today_state.get("hc_calls") or []):
        if (c.get("source"), c.get("ticker")) not in prev_calls:
            pt = f" PT {c['pt']}" if c.get("pt") and c["pt"].upper() not in ("N/A", "") else ""
            bullets.append(
                f"**Fresh high-conviction:** {c['source']} on "
                f"${c['ticker']} ({c.get('action', '?')}{pt})"
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


def render_trade_board(board_rows: list[dict], today: str) -> str:
    """Render the TRADE BOARD as a monospace block (Discord embeds do
    not render markdown tables). Empty string when no live leans."""
    if not board_rows:
        return ""
    from datetime import date as _date
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
        status = "NEW " if is_new else f"d{min(days, 99):<3}"
        direction = (r.get("direction") or "?").upper()
        inst = f"${r.get('instrument', '?')}"
        ctx = (r.get("context_snippet") or "").strip()
        # Strip the lean phrase itself from the context so the line
        # doesn't read "LONG $SOXX  Long $SOXX on the…" — drop the
        # leading verb + filler + cashtag, keep the rationale tail.
        ctx = _LEAN_RE.sub("", ctx, count=1).strip(" ,—-")
        ctx = re.sub(r"\s+", " ", ctx)[:70]
        lines.append(f"{status} {direction:<5} {inst:<11} {ctx}")
    return (
        "## TRADE BOARD\n\n"
        "Leans the pulse is carrying (NEW = opened today, dN = day N "
        "live; leans age off after 5 quiet days):\n\n"
        "```\n"
        + "\n".join(lines)
        + "\n```\n"
    )


# =====================================================================
# Injection
# =====================================================================

def inject_sections(markdown: str, what_changed_md: str, board_md: str) -> str:
    """Insert WHAT CHANGED after the RECAP section and TRADE BOARD before
    WHAT TO WATCH. Idempotent: if either header already exists in the
    markdown (bridge retry), it is not inserted twice."""
    out = markdown

    if what_changed_md and "## WHAT CHANGED" not in out:
        # Insert immediately before the INSIGHTS header (end of RECAP).
        m = re.search(r"^##\s+(?:\d+\.\s+)?INSIGHTS", out, re.MULTILINE | re.IGNORECASE)
        if m:
            out = out[:m.start()] + what_changed_md + "\n" + out[m.start():]
        else:
            out = out.rstrip() + "\n\n" + what_changed_md

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
