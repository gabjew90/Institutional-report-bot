"""A desk is credited only with its own calls (2026-10-26 audit A21)."""
import db  # noqa: F401  (loads the facade before db_parts)
from db_parts import pdf as P
from discord_bot import stock_outline as S


def test_placeholder_rating_is_not_a_call():
    assert not P._is_rating_call({"action": "negative_catalyst_watch", "rating": "N/A",
                                  "price_target": "N/A", "rationale": "shares sank 9%"})
    assert P._is_rating_call({"action": "downgrade", "rating": "N/A"})
    assert P._is_rating_call({"action": "x", "price_target": "$150"})


def test_relayed_broker_is_credited():
    assert P._quoted_broker("downgrade: cut to Neutral at Piper Sandler on pricing", "Safra") == "Piper Sandler"
    assert P._quoted_broker("JPMorgan raises target", "JPM") == ""
    assert P._quoted_broker("Bank of America reiterates Buy", "BofA") == ""
    assert P._quoted_broker("reiterate overweight on margins", "Goldman") == ""


def test_outline_says_whose_call_it_is():
    note = {"source": "Safra",
            "calls": [{"action": "downgrade", "rating": "Neutral", "call_by": "Piper Sandler",
                       "rationale": "Piper Sandler downgraded it to Neutral on pricing"}]}
    assert S._desk_text(note).startswith("reports Piper Sandler's call, not its own: downgrade, Neutral: ")
    # a relayed action or rating the note's words do not state is left out
    # (2026-11-17: a roundup's extracted fields are not the broker's)
    vague = {"source": "Safra", "calls": [{"action": "downgrade", "rating": "Neutral",
                                           "rationale": "Piper cut it", "call_by": "Piper Sandler"}]}
    assert S._desk_text(vague) == "reports Piper Sandler's call, not its own: Piper cut it"


def test_one_source_cannot_crowd_the_banks_research_out():
    """2026-11-17: the 12 newest notes naming NVDA were a day of The Market
    Ear and sales notes, and JPMorgan's equity note of the day before never
    reached the answer."""
    flood = [{"source": "The Market Ear", "title": f"note {i}", "report_type": "strategy", "calls": []}
             for i in range(14)]
    others = [{"source": f"Desk {i}", "title": "x", "report_type": "sales_trading", "calls": []} for i in range(11)]
    jpm = {"source": "JPMorgan", "title": "NVDA", "report_type": "equity_research",
           "calls": [{"rating": "Overweight", "price_target": "$215.00"}]}
    picked = P._pick_notes([*flood, *others, jpm], 12)
    assert len(picked) == 12 and jpm in picked
    assert sum(n["source"] == "The Market Ear" for n in picked) == P.MAX_NOTES_PER_SOURCE
    # newest first is kept, and a short list is returned whole
    assert [n["title"] for n in picked[:3]] == ["note 0", "note 1", "note 2"] and picked[-1] is jpm
    assert P._pick_notes([jpm, flood[0]], 12) == [jpm, flood[0]]
    assert P.rating_house("JPM") and P.rating_house("Goldman Sachs Global Investment Research")
    assert not P.rating_house("The Market Ear") and not P.rating_house("Zerohedge , summarizing Goldman Sachs")
    # a bank's own newer sales, morning and credit notes do not take its places from its equity note
    newer = [{"source": "JPMorgan", "title": t, "report_type": t, "calls": []}
             for t in ("sales_trading", "morning_briefing", "credit", "macro")]
    got = P._pick_notes([*newer, jpm], 12)
    assert jpm in got and len(got) == P.MAX_NOTES_PER_SOURCE and got[-1] is jpm
