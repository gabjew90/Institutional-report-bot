"""Answers whose body is a list or a calculation, written in code (2026-10-27).

Run 6 of the quality audit (2026-10-26) found the same failure under
five shapes: the model, handed the data, listed three days of a five-day
earnings week, skipped the margin arithmetic, computed returns after an
unlock wrong, and got a forward P/E wrong. Each got its own module and
a check that repaired the answer afterwards (the week slate's fill-in
check added the sessions it skipped). The harness run of 2026-10-27
showed the limit of repair: the model's lines out of order with code's
lines after them, and returns the model computed itself from the series
even with the event-returns path available.

So for a question the router has recognised as one of these shapes, the
body of the answer is rendered here from the prefetch result, and the
model writes only the opening. The model sees the exact lines it will be
posted above (`note`), and `compose` keeps its first paragraph as the
opening when that paragraph is not itself a list and carries no figure
the lines do not, then appends the lines.

Blocks come from prefetches only. A prefetch means the router decided
what the question is; a tool the model called on a catch-all route may
be one part of a broader answer, which a code-built body would erase.
"""
from __future__ import annotations

from datetime import date

NOTE = ("\n\nPOSTED AS WRITTEN: the system posts the lines below as the body of your reply, "
        "exactly as they are. Write only the opening, one or two sentences on what stands out, "
        "in the room's voice, with no figure the lines do not carry. Do not list, repeat or "
        "recompute them.\n")

# The bot's own no-answer and refusal lines: never an opening.
_NOT_A_LEAD = ("→ No response came back", "→ Thought myself in circles",
               "→ don't have that one", "→ ⚠️ Couldn't verify")


def build(tool: str, result: dict, today: date, morning_reported: bool = False) -> dict | None:
    """The block for one prefetch result, or None when the tool has no
    code-built body or the result has nothing to render."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    outline = None
    if tool == "lookup_earnings_slate" and result.get("week_outline"):
        from discord_bot import slate_week
        outline = result["week_outline"]
        lines = slate_week.block_lines(outline, today, morning_reported)
    elif tool == "margin_math":
        from discord_bot import margin_math
        lines = margin_math.block_lines(result)
    elif tool == "event_returns":
        from discord_bot import event_returns
        lines = event_returns.block_lines(result)
    else:
        return None
    if not lines:
        return None
    # plain data: the ask log serializes _ask_meta
    return {"tool": tool, "lines": lines, "outline": outline, "today": today.isoformat()}


def note(block: dict) -> str:
    """What the model is shown under the prefetch block."""
    return NOTE + "\n\n".join(block["lines"])


def _lead(answer: str) -> str:
    from discord_bot.data_footer import strip_model_sources
    from discord_bot.figure_provenance import _lines
    text = strip_model_sources(answer or "").strip()
    parts = _lines(text)
    lead = parts[0].strip() if parts else ""
    return "" if lead.startswith(_NOT_A_LEAD) else lead


def _lead_ok(lead: str, blocks: list[dict], body: str, question: str) -> bool:
    """An opening, not a list line and not a figure of the model's own."""
    from discord_bot import slate_week
    from discord_bot.figure_provenance import unsourced_figures
    for b in blocks:
        if (b.get("outline")
                and slate_week.mentions_any(lead, b["outline"], date.fromisoformat(b["today"])) >= 3):
            return False
    _, missing = unsourced_figures(lead, body + "\n" + (question or ""))
    return not missing


def compose(answer: str, blocks: list[dict], question: str = "") -> tuple[str, bool]:
    """(the posted answer, whether the model's opening was kept)."""
    body = "\n\n".join(ln for b in blocks for ln in b["lines"])
    if not body:
        return answer, False
    lead = _lead(answer)
    if lead and not _lead_ok(lead, blocks, body, question):
        lead = ""
    return ((lead + "\n\n") if lead else "") + body, bool(lead)
