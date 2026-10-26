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
    note = {"calls": [{"action": "downgrade", "rating": "Neutral", "rationale": "Piper cut it",
                       "call_by": "Piper Sandler"}]}
    assert S._desk_text(note).startswith("reports Piper Sandler's call, not its own: downgrade")
