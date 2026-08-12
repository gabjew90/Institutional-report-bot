"""Smoke: a roast about a third party is judged against THAT person.

2026-08-12. SV asked "is @Tulch still the donkey of the room? He's been on
a hot streak". The reply was correct on every receipt — Tulch's own
messages ("Everything I've sent in member alerts is zero", "Other thank
SNDK"), his own 86.93% SPXW close, his own catchphrase — and the room
still answered "Once again the bot is thinking about the wrong person".

Two separate things came out of that:

1. The clapback fidelity guard scoped its receipts pool to the ASKER.
   ask_prompt.py already says a question about another member takes its
   substance from the SUBJECT'S profile, so the guard and the prompt
   disagreed about whose material counts. Scoped to SV, every correct
   Tulch receipt looks unsourced, so a guard that fired would have
   rewritten a correct roast into a vague one — and it still could not
   see the real cross-attribution case it was built for (2026-07-29,
   kyle wearing ZHawk's receipts), because that is a third-party case too.

2. The reply never named Tulch. Threaded under the asker's message, an
   unnamed "him" has nothing anchoring it.
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


PROFILES = (
    '- **SV** (sv77788, <@1095941993957437521>) — _trader-rank #9/56_:\n'
    '**SV (sv77788) — 2202 msgs**\n'
    '**Personality and style.** Pharmacy professional, index weeklies.\n'
    '**Recent trades.**\n- XSP puts, closed red.\n'
    '- **Tulch** (tulch, <@704361827290579084>) — _trader-rank #5/56_:\n'
    '**Tulch (tulch) — 3321 msgs**\n'
    '**Personality and style.** Hyperactive scalper, memory and semis.\n'
    '**Voice.**\n- "Give me a cock rocket" — [begging for a pump]\n'
    '**Recent trades.**\n- SPXW call close, 86.93%.\n- SNDK calls.\n'
)

CHAT = (
    "SV (sv77788): is tulch still the donkey\n"
    "Tulch (tulch): Everything I've sent in member alerts is zero\n"
    "Tulch (tulch): Other thank SNDK\n"
)

# The real question, with the injected VERBATIM block above it.
THIRD_PARTY_Q = (
    "[VERBATIM RECENT MESSAGES — Tulch (tulch) — for accurate quoting]\n"
    "  2026-08-12T15:36 #stonks — Everything I've sent in member alerts is zero\n"
    "is @Tulch  still the donkey of the room? He's been on a hot streak"
)
SELF_Q = "how do i trade, what's my tell"


def test_third_party_subject_is_detected():
    from discord_bot.bot import _roast_subject
    got = _roast_subject(THIRD_PARTY_Q, PROFILES, "sv77788", "SV")
    if not got:
        _fail("no subject detected on a question that names @Tulch")
    if got[1].lower() != "tulch":
        _fail(f"wrong subject: {got}")
    _ok("third-party subject resolved from the question line")


def test_asker_is_never_their_own_subject():
    from discord_bot.bot import _roast_subject
    if _roast_subject(SELF_Q, PROFILES, "sv77788", "SV") is not None:
        _fail("self-directed question produced a subject")
    if _roast_subject("SV what do you think", PROFILES, "sv77788",
                      "SV") is not None:
        _fail("the asker was treated as their own subject")
    _ok("self-directed banter keeps the asker-scoped pool")


def test_injected_blocks_do_not_create_false_subjects():
    """The VERBATIM and REPLIED-TO blocks quote other members wholesale.
    Only the real question line may nominate a subject."""
    from discord_bot.bot import _roast_subject
    q = (
        "[VERBATIM RECENT MESSAGES — Tulch (tulch) — for accurate quoting]\n"
        "  2026-08-12T15:36 #stonks — Everything I've sent is zero\n"
        "what time does cpi release today"
    )
    if _roast_subject(q, PROFILES, "sv77788", "SV") is not None:
        _fail("a quoted member in the VERBATIM block was read as the subject")
    _ok("injected quote blocks do not nominate a subject")


def test_subject_material_is_the_subjects_not_the_askers():
    """The core defect: Tulch's receipts must be judged against Tulch."""
    from discord_bot.bot import _member_material_surface
    subj = _member_material_surface(PROFILES, CHAT, "tulch", "Tulch",
                                    THIRD_PARTY_Q)
    asker = _member_material_surface(PROFILES, CHAT, "sv77788", "SV",
                                     THIRD_PARTY_Q)
    for receipt in ("86.93", "SNDK", "cock rocket"):
        if receipt not in subj:
            _fail(f"subject pool is missing {receipt!r}")
        if receipt in asker:
            _fail(f"asker pool wrongly contains the subject's {receipt!r}")
    if "XSP" not in asker:
        _fail("asker pool lost the asker's own material")
    _ok("subject pool holds the subject's receipts, asker pool does not")


def test_real_answer_passes_under_subject_scope_and_fails_under_asker():
    """The 2026-08-12 answer, verbatim. Correct against Tulch, and it is
    only the asker-scoped pool that makes it look invented."""
    from discord_bot.bot import (_clapback_fidelity_violations,
                                 _member_material_surface)
    answer = (
        "hot streak is doing some heavy lifting for a guy whose entire "
        "member alert ledger is a graveyard except for SanDisk. hitting "
        "one accidental 86% SPX print because he forgot he left the "
        "terminal open doesn't wash away a trail of expired lottos."
    )
    subj = _member_material_surface(PROFILES, CHAT, "tulch", "Tulch",
                                    THIRD_PARTY_Q)
    asker = _member_material_surface(PROFILES, CHAT, "sv77788", "SV",
                                     THIRD_PARTY_Q)
    v_subj = _clapback_fidelity_violations(answer, subj)
    v_asker = _clapback_fidelity_violations(answer, asker)
    if len(v_subj) >= len(v_asker) and v_asker:
        _fail(f"subject scope is no better than asker scope "
              f"(subject={v_subj}, asker={v_asker})")
    _ok(f"subject scope flags {len(v_subj)}, asker scope flags "
        f"{len(v_asker)} on the real answer")


def test_guard_uses_subject_scope_and_stamps_it():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    i = src.find("Clapback fidelity guard")
    # Bound at the next guard rather than a fixed width — a comment edit
    # should not silently move the checks out of range.
    end = src.find("Subject-naming guard", i)
    block = src[i:end if end > i else i + 6000]
    if "_roast_subjects(" not in block:
        _fail("the fidelity guard does not resolve the roast subject(s)")
    if "fidelity_scope" not in block or "subject:" not in block:
        _fail("the fidelity guard does not stamp which subject pool it used")
    if "_member_material_surface(" not in block:
        _fail("the fidelity guard still builds an asker-only pool")
    if "fidelity_scope" not in block:
        _fail("the chosen scope is not stamped into the ask log — the next "
              "post-mortem would have to re-derive it")
    _ok("fidelity guard scopes to the subject and records which pool it used")


REAL_UNNAMED = (
    "hot streak is doing some heavy lifting for a guy whose entire "
    "member alert ledger is a graveyard except for SanDisk. give him a "
    "week and he'll be right back to begging for a cock rocket."
)


MULTI = (
    "- **SV** (sv77788, <@1>) — _r_:\n**SV**\n"
    "- **Monsoon** (reportufirst, <@2>) — _r_:\n**Monsoon**\n"
    "- **Tulch** (tulch, <@3>) — _r_:\n**Tulch**\n"
)


def test_two_tags_resolve_by_question_order_not_profile_order():
    """Monsoon is listed FIRST in the profiles block. The primary subject
    must still follow the question, not the block."""
    from discord_bot.bot import _roast_subject
    a = _roast_subject("is @Tulch worse than @Monsoon", MULTI, "sv77788", "SV")
    b = _roast_subject("is @Monsoon worse than @Tulch", MULTI, "sv77788", "SV")
    if a[0] != "Tulch":
        _fail(f"primary subject should be Tulch, got {a}")
    if b[0] != "Monsoon":
        _fail(f"primary subject should be Monsoon, got {b}")
    _ok("two tags resolve by question order, not profiles-block order")


def test_all_tagged_members_are_returned():
    from discord_bot.bot import _roast_subjects
    got = [d for d, _u in _roast_subjects(
        "is @Tulch worse than @Monsoon", MULTI, "sv77788", "SV")]
    if got != ["Tulch", "Monsoon"]:
        _fail(f"expected both subjects in question order, got {got}")
    one = _roast_subjects("is @Tulch still the donkey", MULTI, "sv77788", "SV")
    if len(one) != 1:
        _fail(f"single-tag question returned {len(one)} subjects")
    _ok("every tagged member is returned, ordered by first mention")


def test_comparison_pool_covers_both_dossiers():
    """A vs B question must not flag B's receipts as unsourced."""
    from discord_bot.bot import _roast_subjects, _member_material_surface
    profiles = MULTI.replace(
        "**Monsoon**\n",
        "**Monsoon**\n**Recent trades.**\n- NVDA calls.\n",
    ).replace(
        "**Tulch**\n",
        "**Tulch**\n**Recent trades.**\n- SNDK calls.\n",
    )
    subs = _roast_subjects("is @Tulch worse than @Monsoon", profiles,
                           "sv77788", "SV")
    pool = "\n".join(
        _member_material_surface(profiles, "", u, d, "") for d, u in subs)
    for receipt in ("SNDK", "NVDA"):
        if receipt not in pool:
            _fail(f"union pool is missing {receipt!r} from a tagged member")
    _ok("comparison pool covers every tagged member's material")


def test_naming_gate_catches_the_motivating_case():
    """The 2026-08-12 answer verbatim. _is_clapback_shaped() returns False
    for it, so a naming guard behind that gate could not fire on the very
    case it was built for. The gate must be third-person reference."""
    from discord_bot.bot import _is_clapback_shaped, _THIRD_PERSON_REF_RE
    if _is_clapback_shaped(REAL_UNNAMED):
        _fail("premise changed: the real answer is now clapback-shaped, "
              "re-derive whether the naming gate still needs to differ")
    if not _THIRD_PERSON_REF_RE.search(REAL_UNNAMED):
        _fail("third-person gate does not match the real unnamed roast")
    _ok("naming gate catches the case _is_clapback_shaped misses")


def test_second_person_replies_are_not_flagged():
    """"you" is unambiguous — the referent is whoever is being replied to."""
    from discord_bot.bot import _THIRD_PERSON_REF_RE
    for a in ("you're crying about a leaderboard while you're stuck at the "
              "pharmacy", "your entire risk model is vibes and hope"):
        if _THIRD_PERSON_REF_RE.search(a):
            _fail(f"second-person reply matched the third-person gate: {a[:50]}")
    _ok("second-person replies do not trip the naming guard")


def test_naming_guard_is_advisory_only():
    """Presentation problems must not vandalize correct roasts."""
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    i = src.find("Subject-naming guard")
    if i < 0:
        _fail("no subject-naming guard")
    # Bound the block at the next guard rather than a fixed width, so a
    # comment edit cannot silently move the checks out of range.
    end = src.find("Roast-recycle guard", i)
    block = src[i:end if end > i else i + 6000]
    if "_is_clapback_shaped(answer)" in block:
        _fail("naming guard is gated on clapback shape again — that gate "
              "excludes the 2026-08-12 case it exists for")
    if "_THIRD_PERSON_REF_RE" not in block:
        _fail("naming guard does not gate on a third-person reference")
    if "subject-unnamed" not in block:
        _fail("naming guard does not stamp a guard name")
    if "_clapback_fidelity_violations(" not in block:
        _fail("naming rewrite is accepted without re-checking fidelity — it "
              "could introduce material that is not the subject's")
    if "keeping the" not in block:
        _fail("naming guard does not fall back to the original answer")
    _ok("naming guard rewrites once and keeps the original on failure")


def test_prompt_requires_naming_the_subject():
    from discord_bot import ask_prompt
    src = inspect.getsource(ask_prompt)
    if "Name the subject once" not in src:
        _fail("ask prompt does not ask for the subject to be named")
    _ok("ask prompt requires naming the subject once")


if __name__ == "__main__":
    test_third_party_subject_is_detected()
    test_asker_is_never_their_own_subject()
    test_injected_blocks_do_not_create_false_subjects()
    test_subject_material_is_the_subjects_not_the_askers()
    test_real_answer_passes_under_subject_scope_and_fails_under_asker()
    test_guard_uses_subject_scope_and_stamps_it()
    test_two_tags_resolve_by_question_order_not_profile_order()
    test_all_tagged_members_are_returned()
    test_comparison_pool_covers_both_dossiers()
    test_naming_gate_catches_the_motivating_case()
    test_second_person_replies_are_not_flagged()
    test_naming_guard_is_advisory_only()
    test_prompt_requires_naming_the_subject()
    print("\nAll roast-subject-scope smoke tests passed.")
