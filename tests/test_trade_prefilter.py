"""Alert-channel pre-filter (2026-09-26).

Every alert-channel message went to Gemini: ~41,900 calls in 30 days for
910 trades. Half had no digit, no attachment and no reply parent, and
none of those was a trade. The 32 digit-free trades were all replies or
screenshots, so they still reach the model.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from analyst_log import watcher as W


def test_chatter_is_skipped():
    for c in ("gm", "lol this market is cooked", "shortly after lunch", "anyone around"):
        assert not W.could_be_trade_caption(c, is_reply=False), c


def test_every_digit_free_trade_seen_in_30_days_still_reaches_the_model():
    # the 32 were replies; verbatim samples from production
    for c in ("SOLD", "yes", "same", "took these", "i papered", "Dump it !!"):
        assert W.could_be_trade_caption(c, is_reply=True), c


def test_numbers_tickers_and_trade_verbs_pass():
    for c in ("NVDA 190c", "$HOOD looking weak", "bought NVDA", "all out of ARM",
              "cutting HOOD puts here"):
        assert W.could_be_trade_caption(c, is_reply=False), c


def _msg(content, reference=None):
    author = SimpleNamespace(name="member", display_name="Member", id=1)
    return SimpleNamespace(content=content, attachments=[], reference=reference,
                           author=author, id=42, channel=SimpleNamespace(name="alerts"),
                           created_at=SimpleNamespace(isoformat=lambda: "2026-09-26T00:00:00"))


def test_a_skipped_message_never_calls_gemini_or_discord():
    with patch.object(W, "extract_trade_from_caption", new=AsyncMock()) as ex, \
         patch.object(W, "_fetch_reply_parent_caption", new=AsyncMock(return_value="")) as parent:
        asyncio.run(W.watch_message(None, _msg("gm"), tracking_mode="member"))
    ex.assert_not_called()
    parent.assert_not_called()


def test_a_reply_still_goes_through():
    """An official caller's reply stays on the live path (member text
    posts are batched since 2026-09-26, see test_member_batch)."""
    caller = {"username": "member", "name": "member", "display": "Member"}
    with patch.object(W, "extract_trade_from_caption", new=AsyncMock(return_value=None)) as ex, \
         patch.object(W, "_fetch_reply_parent_caption", new=AsyncMock(return_value="NVDA 190c")), \
         patch.object(W.db, "analyst_trade_exists", return_value=False):
        asyncio.run(W.watch_message(None, _msg("SOLD", reference=object()),
                                    caller=caller, tracking_mode="caller"))
    ex.assert_called_once()
