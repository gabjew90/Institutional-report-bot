"""A denial needs a source that denies the same thing (2026-10-30 audit A5:
"The Department of Defense holds no direct reported equity stake" after an
empty news search; the stake was $450M, announced 9-14)."""
from discord_bot import absence_claims as A

HARNESS = ("→ **The Elmet Group ($ELMT)** makes tungsten and molybdenum parts for defense and "
           "semiconductors.\n\n"
           "→ **DOD stake:** The Department of Defense holds no direct reported equity stake in "
           "the company. Its ties run through supply contracts for the F-35 and Javelin.")
LIVE_10_28 = ("Direct DOD involvement functions through defense production grants and supply "
              "contracts rather than a direct equity ownership stake.")


def test_the_elmt_denials_are_found():
    assert A.denials(HARNESS) == [(
        "The Department of Defense holds no direct reported equity stake in the company.", "stake")]
    assert A.denials(LIVE_10_28)[0][1] == "stake"
    assert A.denials("The company has not announced any deal with Nvidia.")[0][1] == "deal"


def test_an_unbacked_denial_is_removed_and_the_rest_stays():
    out, cut = A.check(HARNESS, '{"status": "no_data", "error": "No dated news on ELMT in the last 90 days."}')
    assert cut == ["The Department of Defense holds no direct reported equity stake in the company."]
    assert "no direct reported equity stake" not in out
    assert "Its ties run through supply contracts" in out and "tungsten" in out


def test_a_payload_that_shows_the_stake_does_not_back_the_denial():
    ev = "2026-09-14: Department of War invests $450M in preferred equity, a stake of up to 19.9% (war.gov)"
    assert A.check(LIVE_10_28, ev)[1]


def test_a_denial_the_evidence_makes_is_kept():
    ev = "NEWS TODAY: the news search found no company news dated today; say so plainly"
    answer = "There is no company news dated today. The stock is down 3% with the sector."
    assert A.check(answer, ev) == (answer, [])
    ev = "Filings in the last year: none (no S-1, S-3 or 424B5 filings)"
    assert A.check("No S-3 or 424B5 filings in the last year.", ev)[1] == []


def test_ordinary_negatives_are_not_denials():
    for s in ("MU has no debt maturities until 2028.", "No bank note covers the name.",
              "The stock is not a buy here.", "It did not give guidance.",
              "Revenue did not grow last quarter."):
        assert A.denials(s) == [], s


def test_an_arrow_left_empty_goes():
    answer = "→ The stake was never announced.\n→ Shares are up 4% today."
    out, cut = A.check(answer, "")
    assert cut and out == "→ Shares are up 4% today."


def test_a_denial_at_the_end_of_an_evidence_sentence_counts():
    assert A.supported("stake", "The filing says the fund holds no equity stake.")
    assert A.supported("deal", "The company hasn't signed a deal.")
