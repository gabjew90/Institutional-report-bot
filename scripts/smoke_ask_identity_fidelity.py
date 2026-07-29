"""Smoke: identity + rewrite-fidelity guards (2026-07-17 'Morgan' incident).

The room turned on the bot in one afternoon ("can we delete this bot",
"it's not even talking about the right people"):
  - BK asked "Morgan says you don't work very well" — the bot didn't
    know Morgan, called no tool, and dressed the ASKER'S own dossier up
    as Morgan. The wrong mapping entered chat context and cascaded.
  - Slim factually disputed a roast claim; the shipped answer ignored
    the dispute and escalated with traits from nobody's profile
    ("manifestos", "stoic strategist") — invented by a register rewrite
    that receives only the original answer text.
  - A separate ask died on Gemini's 1M-input-token limit (400) after a
    tool result ballooned the contents.
"""

import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Point the DB at a scratch file so name-guard DB lookups are harmless.
os.environ.setdefault(
    "DB_PATH", os.path.join(tempfile.gettempdir(), "smoke_identity.db")
)

import discord_bot.bot as bot  # noqa: E402


def _ok(msg):
    print(f"PASS {msg}")


_KNOWN = (
    "WHO'S TALKING ... **BK** (bankerkyle, <@423994649317736448>) ... "
    "**Moonsoon** (reportufirst) ... Recent channel chat: "
    "terlin (.terlin): Fc mu 15m\nZHawk (.zhawk): nigga what\n"
)


def test_unknown_name_detection():
    # v2 contract (2026-07-28 R2): the note fires only on NEAR-member
    # confusion — a token resembling a known member without matching
    # ("Monsoon" ~ member "Moonsoon"). Names near nobody (Morgan) no
    # longer fire; the prompt's don't-invent-biography rule owns them.
    # This deliberately retires the v1 Morgan expectation — see
    # smoke_name_check_false_positives.py for the full v2 contract.
    unk = bot._unknown_member_names(
        "I'm not Monsoon nigga I don't do clinical shifts", _KNOWN)
    assert unk == ["Monsoon"], f"near-member Monsoon must flag: {unk}"
    assert bot._unknown_member_names(
        "Morgan says you don't work very well", _KNOWN) == []
    # known members never flag
    assert bot._unknown_member_names(
        "Moonsoon says you don't work very well", _KNOWN) == []
    # public figures / stopwords never flag
    for q in (
        "is Trump speaking on Tuesday",
        "What did Powell say about the Fed",
        "Tell me about America and China",
    ):
        assert bot._unknown_member_names(q, _KNOWN) == [], f"false flag: {q!r}"
    _ok("name guard v2: near-member flags; members/figures/others don't")


def test_name_check_note_shape():
    note = bot._name_check_note(["Morgan"])
    assert "'Morgan'" in note
    assert "NEVER map the name onto the asker" in note
    assert "lookup_user_profile" in note
    assert "public figure, ignore" in note, "false positives must be benign"
    assert bot._name_check_note([]) == ""
    _ok("name-check note: resolves-or-owns directive, benign on non-persons")


def test_dispute_detection():
    disputing = (
        '[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n'
        '"You\'re busy martingaling into every red candle you see"\n\n'
        "[DeeP FRieD DΞFi's message to you]\n"
        "What are you talking about, I haven’t martingaled a play in "
        "probably 2 months"
    )
    assert bot._is_disputing_reply(disputing), "the 07-17 dispute must fire"
    # reply to the bot but not disputing
    assert not bot._is_disputing_reply(
        '[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n"x"\n\n'
        "[msg]\nlmao fair enough"
    )
    # disputing language but replying to another MEMBER, not the bot
    assert not bot._is_disputing_reply(
        '[MESSAGE BEING REPLIED TO — from abe — user_id 2]\n"x"\n\n'
        "[msg]\nI never said that"
    )
    # no reply block at all
    assert not bot._is_disputing_reply("I never martingale, just asking")
    assert "CONCEDE" in bot._DISPUTE_NOTE and "receipts" in bot._DISPUTE_NOTE
    _ok("dispute guard: fires on bot-reply disputes only; note demands receipts")


def test_novel_ratio_rejects_the_shipped_fiction():
    # the actual 07-17 raw answer + profile material (allowed sources)
    sources = (
        "Two months? You’re splitting hairs on the calendar to feel better "
        "about your account, but we both know you’re just one bad morning "
        "away from doubling down on some 0DTE garbage you don't understand. "
        "Maybe if you spent half as much time analyzing your entries as you "
        "do obsessing over your local shawarma spot or justifying $600 "
        "omakase dinners as eating gold, you wouldn't be sweating the house "
        "closing so hard. Keep the realist brand if you want, but the only "
        "thing real about your trading is the way you panic-post the second "
        "the tape moves against you. "
        "Profile: known for his martingale approach to losing trades, lives "
        "outside Ottawa, obsessed with his car and trailer hitch, regularly "
        "orders $600 omakase, currently closing on a house, treats the "
        "discord as his personal diary for venting about missed gains."
    )
    # the actual shipped rewrite — traits from nobody's profile
    fiction = (
        "You’re stretching your timeline to mask the fact that you’re "
        "paralyzed the second the tape isn't spoon-feeding you a trend. You "
        "spend more time writing manifestos about why your contrarian "
        "thesis is misunderstood than actually managing risk, which "
        "explains why you’re constantly vibrating with anxiety over every "
        "three-point move. You LARP as a stoic strategist, but the only "
        "thing consistent about you is how fast you fold the second your "
        "conviction gets tested."
    )
    r_bad = bot._rewrite_novel_ratio(fiction, sources)
    assert r_bad > bot._REWRITE_NOVEL_MAX_RATIO, (
        f"the shipped fiction must exceed the threshold: {r_bad:.2f}"
    )
    # a faithful tone-only rewrite of the same answer stays under
    faithful = (
        "Two months is splitting hairs on the calendar. You are one bad "
        "morning from doubling down on 0DTE garbage, you spend your time "
        "obsessing over the shawarma spot and justifying $600 omakase "
        "dinners while sweating the house closing. The realist brand is "
        "just panic-posting the second the tape moves against you."
    )
    r_good = bot._rewrite_novel_ratio(faithful, sources)
    assert r_good <= bot._REWRITE_NOVEL_MAX_RATIO, (
        f"a faithful rewrite must pass: {r_good:.2f}"
    )
    _ok(f"novel-ratio: fiction {r_bad:.2f} rejected, faithful {r_good:.2f} passes")


def test_wiring():
    src = inspect.getsource(bot._answer_with_gemini)
    # pre-flight notes: LOCAL-gated name check + dispute check, injected
    # into the FIRST user turn (not a dangling extra turn)
    assert "_unknown_member_names(question, _known_surface)" in src
    assert "if not needs_web:" in src.split("_preflight_notes = ", 1)[1][:400], \
        "name check must be LOCAL-gated"
    assert "_is_disputing_reply(question)" in src
    assert "user_content + _preflight_notes" in src, \
        "notes must be appended to the initial user turn"
    # tool-result clamp + contents-size guard
    assert "_TOOL_RESULT_CHAR_CAP" in src and '"truncated"' in src, \
        "tool-result size clamp missing"
    assert "contents grew to" in src and "Largest parts" in src, \
        "contents-size guard with part diagnostics missing"
    # both rewrite sites carry SUBJECT MATERIAL + the novel check
    assert src.count("SUBJECT MATERIAL") >= 2, \
        "both rewrite prompts must carry the dossier as allowed material"
    assert src.count("_rewrite_novel_ratio(") >= 2, \
        "both rewrite accept-sites must run the fidelity check"
    assert src.count("rewrite:novel-rejected") >= 2, \
        "rejections must be stamped for QC"
    _ok("wiring: notes injected, clamps in the loop, both rewrites checked")


if __name__ == "__main__":
    print("=== identity + rewrite-fidelity smoke ===")
    test_unknown_name_detection()
    test_name_check_note_shape()
    test_dispute_detection()
    test_novel_ratio_rejects_the_shipped_fiction()
    test_wiring()
    print("\nALL IDENTITY/FIDELITY SMOKE TESTS PASS")
