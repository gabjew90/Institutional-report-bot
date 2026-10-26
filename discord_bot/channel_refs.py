"""A question about a channel the bot does not read is answered in code.

2026-10-26 audit A15: "any activity on these names from <#unusual-options>"
named a webhook-feed channel the bot never stores (channel_config's
exclude list). The model answered from bank-note titles as if it had read
the channel.
"""
from __future__ import annotations

import re

_REF_RE = re.compile(r"<#(\d{15,22})>")


def unread_channels(asker_text: str) -> list[int]:
    """Channel IDs the asker wrote as <#id> whose messages are not stored."""
    import channel_config
    out: list[int] = []
    for m in _REF_RE.finditer(asker_text or ""):
        cid = int(m.group(1))
        if cid not in out and not channel_config.ingests(cid):
            out.append(cid)
    return out


def asks_more(asker_text: str) -> bool:
    """The question names a ticker of its own besides the channel ("saw it in
    <#news-flow>, what's NVDA at?"): answer that, with a note, instead of
    only the canned line (code review 2026-10-26)."""
    from discord_bot.ask_router import extract_tickers
    return bool(extract_tickers(_REF_RE.sub(" ", asker_text or "")))


def context_note(ids: list[int]) -> str:
    """The block for a question that asks more than the channel."""
    named = ", ".join(f"<#{c}>" for c in ids)
    return (f"UNREAD CHANNEL: the bot does not store {named}, so nothing posted there is "
            "in your data. Say so in one clause if the asker relies on it, and answer the "
            "rest of the question from the tools.")


def note(ids: list[int]) -> str:
    """The answer: the bot cannot see those channels."""
    named = ", ".join(f"<#{c}>" for c in ids)
    verb = "that channel" if len(ids) == 1 else "those channels"
    return (f"→ I don't read {named}. Posts in {verb} never reach my data, so I can't say "
            "what showed up there. Ask me about the names themselves and I'll pull prices, "
            "research and news.")
