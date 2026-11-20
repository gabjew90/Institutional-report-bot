"""Which rating and price target the pulse may credit a note with (2026-11-20).

The TRADE BOARD of 2026-11-20 published "Natixis $VLVLY · UP". The note, a
Natixis morning briefing, says Moody's cut Volvo Cars' credit rating to Ba2
from Ba1. The extraction had filled the mover's `rating` with "Underperform"
and its `action` with "downgrade", and the board printed both as Natixis's
call on the stock. Of that morning's 24 board candidates, 4 came from a
house's own equity research. The rest came from morning briefings, sales
notes, strategy pieces and a newsletter, where the extraction fills the
fields whether or not the note states a rating. Measured on 21 days of
notes against each house's own equity research on the same stock (415
house-and-stock pairs): the stored rating on a non-research note disagreed
124 times and agreed 326.

/ask met the same stored fields on 2026-11-16 ("JPMorgan reiterates its
Underweight rating", from a sales note carrying the credit analysts' view)
and has one rule for them, `discord_bot/desk_line.fields`: trusted on a
rating house's own equity research, otherwise kept only when a sentence of
the note names the broker and states it, never from a sentence about the
company's debt, and a target only when it is one price.

This module hands each market mover to that rule the way /ask's research
lookup does (`db_parts/pdf.py :: research_for_ticker`): only the note's
lines about that stock count as its sentences, and a call the note relays
from another broker is not the note's own. `credit` is called first thing
in `build_pulse_context` and `synthesize_daily_pulse`, so the serialized
analyses, the high-conviction block, the HIGH_CONVICTION summaries and the
board's state all carry the same fields. A mover that loses its rating and
target keeps its ticker and rationale: what the note said still reaches
the writer, without a call the note never made.
"""
from __future__ import annotations

from dataclasses import asdict, replace


def credit_mover(source: str, report_type: str, mover: dict, insights: list | None = None,
                 earnings: list | None = None, company: str = "") -> dict:
    """The mover with `action`, `rating` and `price_target` as the note may
    be credited with them. A field that does not pass is ''. `company`: the
    stock's name as the note's entities give it."""
    import db  # noqa: F401  (the facade loads before db_parts)
    from db_parts.pdf import _quoted_broker, about_matcher
    from discord_bot import desk_line
    if not (desk_line._clean(mover.get("rating")) or desk_line._clean(mover.get("price_target"))
            or str(mover.get("action") or "").lower() in desk_line._RATING_ACTIONS):
        return dict(mover)   # no rating, target or rating action to credit
    about = about_matcher(str(mover.get("ticker") or ""), company)
    call = dict(mover)
    other = _quoted_broker(f"{mover.get('action') or ''} {mover.get('rationale') or ''}", source or "")
    if other:
        # another broker's action, relayed: not this note's call
        return {**mover, "action": "", "rating": "", "price_target": ""}
    f = desk_line.fields({"source": source, "report_type": report_type, "calls": [call],
                          "insights": [str(x) for x in insights or [] if about(str(x))],
                          "earnings": [str(x) for x in earnings or [] if about(str(x))]})
    return {**mover, "action": f["action"], "rating": f["rating"], "price_target": f["target"]}


def _company(entities: list | None, ticker: str) -> str:
    tk = (ticker or "").strip().upper()
    for e in entities or []:
        name = e.get("name") if isinstance(e, dict) else getattr(e, "name", "")
        sym = e.get("ticker") if isinstance(e, dict) else getattr(e, "ticker", "")
        if tk and (sym or "").strip().upper() == tk and name:
            return str(name)
    return ""


def credit(analyses: list) -> list:
    """Each analysis with its market movers passed through `credit_mover`.
    The input is not changed."""
    out = []
    for a in analyses:
        if not a.market_movers:
            out.append(a)
            continue
        movers = []
        for mm in a.market_movers:
            kept = credit_mover(a.source, a.report_type, asdict(mm), a.key_insights, a.earnings_insights,
                                _company(a.entities_mentioned, mm.ticker))
            movers.append(replace(mm, action=kept["action"], rating=kept["rating"],
                                  price_target=kept["price_target"]))
        out.append(replace(a, market_movers=movers))
    return out
