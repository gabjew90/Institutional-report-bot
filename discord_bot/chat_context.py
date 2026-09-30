"""The shape of the recent-chat block /ask prepends to a question. Shared by
the channel path (bot._fetch_chat_context, from Discord's history) and the
live harness (scripts/ask_live.py, from chat_messages) so the replica cannot
drift from the channel."""
from __future__ import annotations

CHAT_HEADER = ("Recent channel chat (oldest → newest, for context only — "
               "the actual question follows after):\n")
BOT_SPEAKER = "[YOU said earlier]"
# The footer every /ask answer carries; identifies the bot's own posts in
# chat_messages when no user id is configured.
NFA_FOOTER = "Hi, I'm AI-powered - NFA"


def speaker(display: str | None, username: str | None) -> str:
    """'display (username)' when they differ, else whichever is set."""
    if display and username and display.lower() != username.lower():
        return f"{display} ({username})"
    return display or username or ""


def line(who: str, text: str, image_placeholder: str = "") -> str:
    return f"{who}: {text or '(image)'}{image_placeholder}"


def block(lines: list[str]) -> str:
    return CHAT_HEADER + "\n".join(lines)
