"""The unknown-name check reads the asker's words only (2026-10-18 audit A30:
"Kill" from a quoted "Kill yourself" and "Whatever" were flagged)."""
from unittest.mock import patch

from discord_bot import bot as B

Q = ("[VERBATIM RECENT MESSAGES — Monsoon (reportufirst) — for accurate quoting]\n"
     "  2026-10-14T18:40 #💬-stonks-yapping-💬 — Kill yourself Whatever\n\n"
     "[bulch's message to you]\nwhat do you think of monsoon")


def test_quoted_words_are_not_checked():
    with patch.object(B.db, "find_users_mentioned_in_text", return_value=[]), \
         patch.object(B, "_near_known_name", return_value=True):
        assert B._unknown_member_names(Q, "monsoon bulch") == []
