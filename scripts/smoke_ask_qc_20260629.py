"""Smoke: 2026-06-29 /ask-log QC fixes.

Four defects found QC'ing the 06-26 → 06-30 ask logs:
  #1a  intent router under-routed event schedules + seasonal stats to
       LOCAL (the WC-kickoff and July-4-seasonality answers free-handed
       specifics with no search). Router now sends those to WEB.
  #1b  the grounding backstop's generic-density net over-fired on a
       textbook explainer (an FDIC metaphor with "$250k"/"$10M" got the
       "couldn't verify" hedge). Density net is now security-scoped:
       it only counts when the answer names a ticker (cashtag).
  #2   the em-dash voice-lint mangled numeric ranges: "62–65%" shipped
       as "62, 65%". The dash→comma rule now skips digit-flanked dashes.
  #3   /ask silently dropped PDF attachments (image/* only). Both
       attachment paths now accept application/pdf (Gemini reads it).
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


def test_endash_preserves_numeric_ranges():
    import discord_bot.bot as bot
    # the exact 06-26 mangling: a percent range must survive intact
    out, _ = bot._clean_voice_violations("S&P win rate is 62–65% this week")
    assert "62–65%" in out, f"numeric range mangled: {out!r}"
    assert "62, 65%" not in out, f"range comma-ified: {out!r}"
    # dollar range + hour range also survive
    assert "$861–$881" in bot._clean_voice_violations("range $861–$881 today")[0]
    assert "24–48h" in bot._clean_voice_violations("eyeing it 24–48h out")[0]
    # a STRUCTURAL em-dash (the AI tell) is still comma-ified
    spaced, _ = bot._clean_voice_violations("no stop-loss — just bleeding")
    assert "no stop-loss, just bleeding" in spaced, spaced
    tight, _ = bot._clean_voice_violations("amateur hour—just wasting time")
    assert "amateur hour, just wasting time" in tight, tight
    _ok("#2 en-dash: numeric ranges preserved, structural em-dash still cut")


def test_backstop_skips_ticker_free_explainer():
    import discord_bot.bot as bot
    # the 06-30 FDIC explainer: dense dollar figures, NO ticker -> no hedge
    fdic = ("FDIC insurance covers you up to $250k per bank. If you've got "
            "$10M you sweep it across 40 banks so each stays under the $250k "
            "line and the $10M is fully covered.")
    assert bot._is_ungrounded_market_fact(fdic, None, []) is False, \
        "textbook explainer (no ticker) must NOT trip the hedge"
    # a security-scoped data dump (cashtag + dense specifics) STILL trips it
    sec = ("$ABCD is 12% of the basket, yields 3.5%, and trades 45% above "
           "book value right now.")
    assert bot._is_ungrounded_market_fact(sec, None, []) is True, \
        "ticker + 3 specifics with no source must still hedge"
    # the STRONG analyst-fact marker fires even without a cashtag
    strong = "price target raised to $450 on the print"
    assert bot._is_ungrounded_market_fact(strong, None, []) is True, \
        "a price-target claim must hedge regardless of cashtag"
    _ok("#1b backstop: ticker-free explainer skipped, security claims still caught")


def test_router_routes_schedules_and_seasonality_to_web():
    import discord_bot.bot as bot
    ins = bot._ASK_ROUTER_INSTRUCTION
    assert "SCHEDULE" in ins and "START TIME" in ins, \
        "router must send event schedules / start times to WEB"
    assert "World Cup" in ins, "the WC-kickoff miss should be named"
    assert "SEASONAL" in ins and "win-rate" in ins.lower(), \
        "router must send seasonal/historical stats to WEB"
    _ok("#1a router: event schedules + seasonal stats route to WEB")


def test_ask_accepts_pdf_attachments():
    import discord_bot.bot as bot
    assert bot._PDF_MAX_BYTES == 10 * 1024 * 1024, "PDF cap missing/changed"
    src = inspect.getsource(bot._extract_images_from_message)
    assert "application/pdf" in src, \
        "the attachment extractor must accept PDFs, not image/* only"
    assert "image/" in src, "image path must still work"
    # the referenced-message / reply path (inline in the @mention handler)
    # must accept PDFs too — verified against the module source.
    mod_src = inspect.getsource(bot)
    assert mod_src.count("application/pdf") >= 2, \
        "both attachment paths (direct + referenced-msg) must accept PDFs"
    _ok("#3 PDF: both attachment paths accept application/pdf")


if __name__ == "__main__":
    print("=== /ask 2026-06-29 QC-fixes smoke ===")
    test_endash_preserves_numeric_ranges()
    test_backstop_skips_ticker_free_explainer()
    test_router_routes_schedules_and_seasonality_to_web()
    test_ask_accepts_pdf_attachments()
    print("\nALL /ASK QC-FIX SMOKE TESTS PASS")
