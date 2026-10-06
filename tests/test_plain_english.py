"""Desk idiom in /ask answers (2026-10-05 audit #12)."""
from discord_bot import plain_english as P

BUYBACK = ("→ Treasury is buying back up to **$6 billion** of 10Y to 20Y coupons to ease "
           "pressure on dealer balance sheets and absorb long-end duration supply.")
# the first live harness sample after the fix, 2026-10-05
LIVE = ("→ **Official mandate:** Treasury liquidity-support buybacks target seasoned, "
        "less-liquid \"off-the-run\" **10- to 20-year** securities to improve secondary "
        "market functioning and help dealers recycle balance sheets\n"
        "→ **Scale and timing:** Operations expanded up to **$4B–$6B** per auction to "
        "backstop the long end after yields touched multi-year highs\n"
        "→ **Yield impact:** Purchasing long-duration bonds removes supply")


def test_the_audit_answer_and_the_live_sample_are_caught():
    assert {"dealer balance sheets", "duration", "long end"} <= set(P.find(BUYBACK))
    assert {"dealer balance sheets", "duration", "long end", "off-the-run"} <= set(P.find(LIVE))


def test_room_options_words_and_everyday_words_are_not_jargon():
    assert P.find("Gamma and IV are both up, skew is rich, theta eats it by Friday.") == {}
    assert P.find("They carry a lot of inventory and the issuance date is Friday.") == {}
    assert P.find("Apple's balance sheet holds $60B of cash.") == {}
    assert P.find("Build the front-end in React, then sit at a long end table.") == {}
    assert P.find("NII rose 6% to $24.1B and the SLR change frees capital.") == {}
    assert P.find("") == {}
    # rates idioms count only in an answer about bonds
    assert P.find("EOSE ripped on long-duration storage contracts with utilities.") == {}
    assert P.find("CarMax says franchise dealers' balance sheets are stretched.") == {}
    assert "duration" in P.find("Long-duration bonds rallied as yields fell.")


def test_terms_inside_links_and_code_are_ignored():
    assert P.find("see [term premium](https://x.example/term-premium) and `convexity`") == {}


def test_a_rewrite_must_keep_every_number():
    assert P.keeps_numbers(BUYBACK, "Treasury is buying back up to $6 billion of 10- to 20-year bonds.")
    assert not P.keeps_numbers(BUYBACK, "Treasury is buying back bonds to help banks.")
    assert P.keeps_numbers("41,000 contracts", "41000 contracts")


def test_the_directive_carries_each_meaning():
    d = P.directive(P.find(BUYBACK))
    assert "do not keep the term" in d and "Keep every number exactly" in d
    assert "long-dated Treasuries" in d
