"""Smoke: screenshot intake + receipt-to-ledger + phantom-read guard
(2026-07-10).

The 2pale SOXL exchange exposed a full intake chain failure:
  01:41:58 — 2pale replies DIRECTLY to the bot with an image-only
             receipt, no ping → the mention-only trigger dropped it.
  01:42:07 — his follow-up ping carried no image → the bot answered
             with an invented reading ("6.1x isn't 7x ... you finally
             posted a fill") of a screenshot it never saw, validating
             an undocumented 7x claim. Nothing reached the trade ledger.

Fixes, all structural:
  1. TRIGGER — a direct reply to one of the bot's own messages fires
     the handler like a mention (ping on/off no longer decides).
     A bare tag/reply with an image attached counts as a question.
  2. LOOK-BACK — screenshot-first-ask-second: if the ask carried no
     image, the asker's own last messages (5-min window) are scanned
     and the image pulled into the call.
  3. RECEIPT → LEDGER — the image-bearing message is dispatched to the
     member-mode analyst watcher (OCR → extraction → ledger row, no
     announce, idempotent, silent no-op on non-trade images).
  4. PHANTOM-READ GUARD — with zero images in the call, "your
     screenshot shows / you posted a fill" sentences are stripped.
  5. Audit stamp carries images: N.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _handler_src():
    import discord_bot.bot as bot
    # the on_message handler is a nested closure inside create_bot —
    # assert against the module source.
    return inspect.getsource(bot)


def test_reply_to_bot_triggers():
    src = _handler_src()
    assert "_is_reply_to_bot" in src, "reply-to-bot trigger missing"
    gate = src.split("bot.user not in message.mentions and not "
                     "_is_reply_to_bot", 1)
    assert len(gate) == 2, "trigger gate must accept replies to the bot"
    # parent resolution falls back to a fetch when the gateway didn't
    # resolve it
    assert "fetch_message(" in gate[0][-2500:], \
        "unresolved reply parents must be fetched"
    # bare tag/reply with an image counts as a question
    assert 'has_images = bool(getattr(message, "attachments", None))' in src
    assert "has_reference or has_snapshot or has_images" in src
    _ok("trigger: direct replies to the bot + image-only asks fire")


def test_lookback_image_pull():
    src = _handler_src()
    win = src.split("Look-back image fallback", 1)
    assert len(win) == 2, "look-back block missing"
    lb = win[1][:2600]
    assert "limit=8, before=message" in lb, "must scan recent history"
    assert "_prev.author.id != message.author.id" in lb, \
        "must only pull the ASKER's own images"
    assert "> 300" in lb, "5-minute window cap missing"
    assert "_image_source_msg = _prev" in lb, \
        "the image-bearing message must be tracked for ledger dispatch"
    _ok("look-back: asker's just-posted screenshot reaches the call")


def test_receipt_ledger_dispatch():
    src = _handler_src()
    win = src.split("Receipt → ledger", 1)
    assert len(win) == 2, "receipt-to-ledger dispatch missing"
    rl = win[1][:2200]
    assert 'tracking_mode="member"' in rl, "must use member mode (no announce)"
    assert "resolve_chat_eager_ocr_channels" in rl and \
        "caller_by_channel" in rl, \
        "must skip channels the watcher already covers"
    assert "create_task" in rl, "dispatch must be non-blocking"
    _ok("receipt→ledger: image-bearing message dispatched to member-mode "
        "watcher")


def test_phantom_read_detector():
    import discord_bot.bot as bot
    # the exact 2026-07-10 shipped sentences
    shipped = [
        "So you actually have a screenshot.",
        "6.1x isn't 7x, but I'll give you the win since you finally "
        "stopped larping and posted a fill.",
        "Your screenshot shows a 3x on those calls.",
        "I can see the chart you posted, that wick is brutal.",
    ]
    for s in shipped:
        v = bot._phantom_image_read_violations(s)
        assert len(v) == 1, f"phantom read must flag: {s!r} -> {v}"
    # legitimate demand / negation / ledger-attribution forms stay
    for clean in [
        "Post the receipt or keep the cope to yourself.",
        "You never posted a screenshot of that exit.",
        "The ledger has no fill for that trade.",
        "You didn't post the receipt, so it doesn't count.",
    ]:
        assert bot._phantom_image_read_violations(clean) == [], \
            f"false positive: {clean!r}"
    _ok("phantom-read detector: shipped bluffs flag; demands/negations safe")


def test_phantom_guard_wired_and_gated():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert "if answer and not images:" in src, \
        "phantom guard must be gated on NO image in the call"
    win = src.split("Phantom image-read guard", 1)[1][:2200]
    assert "_phantom_image_read_violations(answer)" in win
    assert '_ask_meta["guards"].append("phantom-image")' in win
    assert "_strip_sentences(answer, _phantom)" in win
    assert "repost it" in win, \
        "full-strip fallback must ask for a repost, not ship empty"
    _ok("phantom guard: gated on images==0, detect→strip, repost fallback")


def test_images_in_audit_stamp():
    import inspect as _i
    import discord_bot.bot as bot
    import db as _db
    bsrc = _i.getsource(bot._answer_with_gemini)
    assert '"images": len(images or [])' in bsrc, \
        "meta must carry the image count"
    dsrc = _i.getsource(_db.append_ask_interaction)
    assert 'meta.get("images")' in dsrc, "db stamp must render images: N"
    _ok("audit stamp: images count recorded + rendered")


if __name__ == "__main__":
    print("=== /ask receipt-intake + phantom-read smoke ===")
    test_reply_to_bot_triggers()
    test_lookback_image_pull()
    test_receipt_ledger_dispatch()
    test_phantom_read_detector()
    test_phantom_guard_wired_and_gated()
    test_images_in_audit_stamp()
    print("\nALL RECEIPT-INTAKE SMOKE TESTS PASS")
