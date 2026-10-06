"""Desk idiom in /ask answers (2026-10-05 audit #12)."""
from discord_bot import plain_english as P

BUYBACK = ("→ Treasury is buying back up to **$6 billion** of 10Y to 20Y coupons to ease "
           "pressure on dealer balance sheets and absorb long-end duration supply.")


def test_the_buyback_answer_is_caught():
    got = P.find(BUYBACK)
    assert "dealer balance sheets" in got and "duration supply" in got
    assert "long-end" in got
    assert "dealer balance sheet" not in got, "the longer form covers it"


def test_room_options_words_and_everyday_words_are_not_jargon():
    assert P.find("Gamma and IV are both up, skew is rich, theta eats it by Friday.") == {}
    assert P.find("They carry a lot of inventory and the issuance date is Friday.") == {}
    assert P.find("") == {}
    # everyday meanings and named metrics are not rewritten
    assert P.find("Build the front-end in React, then sit at the long end of the table.") == {}
    assert P.find("NII rose 6% to $24.1B and the SLR change frees capital.") == {}
    assert "long end of the curve" in P.find("Selling hit the long end of the curve.")


def test_terms_inside_links_and_code_are_ignored():
    assert P.find("see [term premium](https://x.example/term-premium) and `NII`") == {}


def test_a_rewrite_must_keep_every_number():
    assert P.keeps_numbers(BUYBACK, "Treasury is buying back up to $6 billion of 10- to 20-year bonds.")
    assert not P.keeps_numbers(BUYBACK, "Treasury is buying back bonds to help banks.")
    assert P.keeps_numbers("41,000 contracts", "41000 contracts")


def test_the_directive_carries_each_meaning():
    d = P.directive(P.find(BUYBACK))
    assert "do not keep the term" in d and "Keep every number exactly" in d
    assert "new long-dated bonds coming to market" in d
