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


def note(ids: list[int]) -> str:
    """The answer: the bot cannot see those channels."""
    named = ", ".join(f"<#{c}>" for c in ids)
    verb = "that channel" if len(ids) == 1 else "those channels"
    return (f"→ I don't read {named}. Posts in {verb} never reach my data, so I can't say "
            "what showed up there. Ask me about the names themselves and I'll pull prices, "
            "research and news.")
