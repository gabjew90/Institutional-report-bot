"""A desk's rating and price target are posted by code (discord_bot/desk_line.py,
2026-11-17, audit run 12 A1: JPMorgan's Overweight on NVDA, target cut to $215
from $320, was posted as "reiterates its Underweight rating")."""
from datetime import date

from discord_bot import ask_router as R
from discord_bot import desk_line as D
from discord_bot import stock_body as B
from discord_bot import stock_outline as O

JPM = {"source": "JPMorgan", "published": "2026-11-12", "report_type": "equity_research",
       "calls": [{"action": "price_target_change", "rating": "Overweight", "price_target": "$215.00",
                  "rationale": "PT cut reflects removal of China-related revenue from FY28 estimates "
                               "and increased management/regulatory risk.", "conviction": "medium"}],
       "insights": ["JPMorgan maintains an Overweight rating on NVDA but cuts the price target to $215 "
                    "from $320, citing leadership uncertainty and increased regulatory risk regarding China."]}
JPM_LINE = "→ **JPMorgan** (Nov 12): rated Overweight, price target **$215**, cut from $320."
GS = {"source": "Goldman Sachs", "published": "2026-11-10", "report_type": "equity_research",
      "calls": [{"action": "reiterate", "rating": "Buy", "price_target": "N/A",
                 "rationale": "Data center demand is ahead of supply into the print", "conviction": "high"}]}
PRICE = {"status": "ok", "session": "REGULAR", "quotes": [
    {"symbol": "NVDA", "price": 181.20, "change_pct": -2.1, "data_freshness": "live_regular_session"}]}
SNAP = {"status": "ok", "symbol": "NVDA",
        "growth": {"next_report": {"date": "2026-11-17", "session": "after the close"},
                   "next_quarter": {"eps": 2.47, "eps_year_ago": 1.30, "revenue": 108.7e9,
                                    "revenue_year_ago": 57.0e9}}}
TODAY = date(2026, 11, 16)


def _note(target, text, action="price_target_change", rating="Buy"):
    return {"source": "Goldman Sachs", "published": "2026-11-05", "report_type": "equity_research",
            "calls": [{"action": action, "rating": rating, "price_target": target, "rationale": text}]}


def test_the_line_states_the_rating_the_target_and_the_old_target():
    assert D.line(JPM) == JPM_LINE
    assert D.line(JPM, preview=True).startswith("→ **JPMorgan** (Nov 12, before the report): rated Overweight")


def test_the_old_target_comes_from_the_notes_own_sentence():
    """Forms taken from stored notes, 2026-11-17."""
    for target, text, want in (
        ("$61", "The price target was reduced by 5% from $64 to $61 due to a lower multiple.", ("cut", "$64")),
        ("$238", "GS lowered the multiple, resulting in a price target reduction to $238 from $250.", ("cut", "$250")),
        ("$84", "GS price target $84 from $90.", ("cut", "$90")),
        ("$34", "UBS keeps X-energy at Buy with a $34 target (cut from $40), shares at $10.36.", ("cut", "$40")),
        ("$5", "Barclays cuts the target to $5 from $11 (shares $5.69).", ("cut", "$11")),
        ("$820.00", "Reiterating Buy rating and raising price target to $820 from $750.", ("raised", "$750")),
        ("$2,050", "Target raised to $2,050 from $1,900.", ("raised", "$1,900")),
        ("€1.6", "Eutelsat PT cut to €1.6 from €5.22 due to execution risks.", None),   # outside 3x: not a step
    ):
        note = _note(target, text)
        assert D.prior_target(note, note["calls"][0]) == want, text


def test_no_old_target_is_read_from_another_figure():
    for target, text in (
        ("$285", "GS reiterates Buy with an unchanged 12-month price target of $285 (38.8% upside from $205.28)."),
        ("$85.00", "JPMorgan upgrades INTC to Neutral from Underweight as the stock approaches the $85 price "
                   "target after a 36% pullback from June peaks."),
        ("$215", "Revenue estimate moves to $215 from $320 on the China change."),   # no target word
        ("$59", "Barclays cuts the target to $5 from $11."),                          # another target
    ):
        note = _note(target, text)
        assert D.prior_target(note, note["calls"][0]) is None, text
        assert "from" not in D.line(note)


def test_the_action_sets_the_wording():
    assert D.line(_note("$106", "", action="upgrade")) == "→ **Goldman Sachs** (Nov 5): upgraded to Buy, price target **$106**."
    assert D.line(_note("N/A", "", action="downgrade", rating="Neutral")) == "→ **Goldman Sachs** (Nov 5): downgraded to Neutral."
    assert D.line(_note("$40", "", action="initiate", rating="Outperform")) == (
        "→ **Goldman Sachs** (Nov 5): initiated at Outperform, price target **$40**.")
    assert D.line(_note("$40", "", rating="N/A")) == "→ **Goldman Sachs** (Nov 5): price target **$40**."
    # a note with neither has no line: its view stays a slot for the model
    assert D.line(_note("N/A", "Guide above Street", action="positive_catalyst_watch", rating="N/A")) == ""
    relayed = _note("N/A", "Piper Sandler downgraded it to Neutral on pricing", action="downgrade", rating="Neutral")
    relayed["calls"][0]["call_by"] = "Piper Sandler"
    assert D.line(relayed) == "→ **Piper Sandler**, relayed by Goldman Sachs (Nov 5): downgraded to Neutral."


# The Market Ear's roundups of what other brokers did, as stored 2026-11-17.
TME_UNSTATED = {"source": "The Market Ear", "published": "2026-11-17",
                "calls": [{"action": "price_target_change", "rating": "Neutral", "price_target": "$220",
                           "rationale": "Goldman's James Schneider cut the multiple from 30x to 22x following "
                                        "Jensen Huang's resignation.", "conviction": "medium", "call_by": "Goldman"}]}
TME_STATED = {"source": "The Market Ear", "published": "2026-11-17",
              "calls": [{"action": "price_target_change", "rating": "Buy", "price_target": "$220",
                         "rationale": "Goldman Sachs cut the target from $300, citing concerns that AI "
                                      "infrastructure growth could slow.", "call_by": "Goldman Sachs"}],
              "insights": ["Goldman Sachs cut its NVDA price target to $220 from $300, citing concerns that "
                           "growth could slow without Nvidia-provided financing."]}
TME_OWN = {"source": "The Market Ear", "published": "2026-11-16",
           "calls": [{"action": "positive_catalyst_watch", "rating": "Neutral", "price_target": "N/A",
                      "rationale": "Rebounded 4% as part of the broader AI trade revival."}]}


def test_a_roundups_field_is_posted_only_when_its_text_states_it():
    """The first live sample posted "Goldman ... rated Neutral, price target
    $220" from a roundup whose extraction had filled in "Neutral". Goldman's
    rating that day was Buy."""
    assert D.line(TME_UNSTATED) == ""
    assert D.fields(TME_UNSTATED) == {"holder": "Goldman", "relayed": True, "action": "", "rating": "",
                                      "target": "", "prior": None, "credit": False}
    # the slot the model words carries no unstated field either
    slot = O._desk_text(TME_UNSTATED)
    assert slot.startswith("reports Goldman's call, not its own: Goldman's James Schneider cut the multiple")
    assert "Neutral" not in slot and "$220" not in slot
    # the target is in a sentence naming the broker, the rating is in none
    assert D.line(TME_STATED) == ("→ **Goldman Sachs**, relayed by The Market Ear (Nov 17): "
                                  "price target **$220**, cut from $300.")
    # a publication has no rating of its own
    assert D.line(TME_OWN) == "" and "Neutral" not in O._desk_text(TME_OWN)
    assert not D.own_broker("The Market Ear") and not D.own_broker("Bloomberg")
    assert not D.own_broker("Zerohedge , summarizing HSBC and Goldman Sachs research")
    assert D.own_broker("Goldman Sachs Global Investment Research") and D.own_broker("J.P. Morgan")
    assert all(D.own_broker(s) for s in ("JPMorgan", "JPM", "Goldman Sachs", "BofA", "Morgan Stanley", "UBS"))
    # "buy" as a verb in a sentence about the broker is not its rating
    verb = {"source": "The Market Ear", "published": "2026-11-17",
            "calls": [{"action": "reiterate", "rating": "Buy", "price_target": "N/A", "call_by": "Goldman",
                       "rationale": "Goldman reiterated that clients should buy the dip."}]}
    assert D.line(verb) == ""
    # the check on the model's words credits a roundup with stated fields only
    assert D.strip_wrong_calls("→ The Market Ear keeps a Neutral rating on it.", [TME_OWN])[1]


def test_a_relayed_line_sits_with_the_arrow_naming_the_broker():
    results = {R.T_PRICE: PRICE, R.T_SNAPSHOT: SNAP, R.T_RESEARCH: {"status": "ok", "notes": [TME_STATED]}}
    body = B.build("ticker_opinion", "NVDA", results, asked="what do you think of nvda", today=TODAY)
    line = body["desks"][0]["line"]
    answer = ("→ CNN Business noted the pressure on the stock ahead of the report.\n\n"
              "→ The Market Ear highlights that Goldman's analyst lowered the multiple he applies after the "
              "change of chief executive.")
    parts = B.compose(answer, body)[0].split("\n\n")
    assert parts[-2] == line and parts[-3].startswith("→ CNN Business"), parts


def _turn(notes, asked="what do you think of nvda"):
    results = {R.T_PRICE: PRICE, R.T_SNAPSHOT: SNAP, R.T_RESEARCH: {"status": "ok", "notes": notes}}
    body = B.build("ticker_opinion", "NVDA", results, asked=asked, today=TODAY)
    return body, O.build_outline(results, "ticker_opinion", "NVDA", asked=asked, posted=body)


def test_the_body_posts_the_line_and_the_model_is_left_the_reason():
    body, text = _turn([JPM, GS])
    assert [d["line"] for d in body["desks"]] == [
        "→ **Goldman Sachs** (Nov 10): rated Buy.", JPM_LINE]       # high conviction first
    assert JPM_LINE not in body["lines"]
    head = text.split("Do not write them")[0]
    assert JPM_LINE in head, "the model is shown the line as posted"
    slot = next(ln for ln in text.splitlines() if "DESK: JPMorgan" in ln)
    assert "its rating and price target are a posted line" in slot and "PT cut reflects removal" in slot
    assert "Overweight" not in slot and "$215" not in slot, "the fields are not handed over to be worded"
    # a reply to an earlier answer gets no desk lines, as it gets no desk slots
    results = {R.T_PRICE: PRICE, R.T_SNAPSHOT: SNAP, R.T_RESEARCH: {"status": "ok", "notes": [JPM]}}
    reply = B.build("follow_up", "NVDA", results, asked="when do they report", today=TODAY)
    assert not (reply or {}).get("desks")


def test_a_rated_note_with_no_reason_has_a_line_and_no_slot():
    bare = {**GS, "calls": [{"action": "reiterate", "rating": "Buy", "price_target": "$250"}]}
    body, text = _turn([bare])
    assert body["desks"][0]["line"] == "→ **Goldman Sachs** (Nov 10): rated Buy, price target **$250**."
    assert "DESK:" not in text


def test_the_line_sits_in_front_of_the_models_reason_for_that_desk():
    body, _ = _turn([JPM])
    answer = ("→ Data center demand is the driver, and supply is what limits it this quarter.\n\n"
              "→ JPMorgan's cut takes China revenue out of its fiscal 2028 estimates and adds risk "
              "around management.")
    out, dropped = B.compose(answer, body)
    parts = out.split("\n\n")
    assert dropped == 0 and parts[:len(body["lines"])] == body["lines"]
    assert parts[-2] == JPM_LINE and parts[-1].startswith("→ JPMorgan's cut takes China revenue")
    # no arrow names the desk: the line stays with the posted lines
    out, _ = B.compose("→ Data center demand is the driver, and supply is what limits it this quarter.", body)
    assert out.split("\n\n")[len(body["lines"])] == JPM_LINE
    # an arrow that only says the line again is dropped
    out, dropped = B.compose("→ JPMorgan rated it Overweight with a $215 price target.", body)
    assert dropped == 1 and out.count("JPMorgan") == 1


def test_a_rating_the_banks_notes_do_not_carry_is_removed():
    said = ("→ NVDA trades near $181.\n\n"
            "→ JPMorgan reiterates its Underweight rating with a $215 price target, citing China risk. "
            "Supply is the limit this quarter.")
    out, cut = D.strip_wrong_calls(said, [JPM, GS])
    assert cut == ["JPMorgan reiterates its Underweight rating with a $215 price target, citing China risk."]
    assert out == "→ NVDA trades near $181.\n\n→ Supply is the limit this quarter."
    # the whole arrow goes when that sentence was all of it
    out, cut = D.strip_wrong_calls("→ NVDA trades near $181.\n\n→ JPM keeps its Underweight rating.", [JPM])
    assert out == "→ NVDA trades near $181." and len(cut) == 1


def test_a_target_the_banks_notes_do_not_carry_is_removed():
    out, cut = D.strip_wrong_calls("→ JPMorgan cut its price target to $125 on China risk.", [JPM])
    assert out == "" and len(cut) == 1
    for ok in ("→ JPMorgan cut its price target to **$215** from $320 on China risk.",
               "→ JPMorgan's $320 target came down on China risk."):
        assert D.strip_wrong_calls(ok, [JPM]) == (ok, []), ok


def test_what_the_notes_carry_is_left_alone():
    for ok in (
        "→ JPMorgan stays Overweight and trims its target on China risk.",
        JPM_LINE,
        "→ Goldman Sachs rates it Buy on data center demand running ahead of supply.",
        "→ JPMorgan keeps a hold on its China estimates and sees a buy signal only after the print.",
        # two of the turn's banks in one sentence: a rating cannot be tied to either
        "→ JPMorgan is Overweight while Goldman Sachs sits at Neutral.",
        # a firm with no note in the turn is the news's, not judged here
        "→ Mizuho downgraded it to Underperform with a $150 target.",
    ):
        assert D.strip_wrong_calls(ok, [JPM, GS]) == (ok, []), ok
    # a rating the note states only in its words is the note's
    worded = {"source": "Citi", "published": "2026-11-11", "insights": ["Citi stays Equal Weight on NVDA"]}
    ok = "→ Citi keeps its Equal-weight rating into the print."
    assert D.strip_wrong_calls(ok, [worded]) == (ok, [])
    assert D.strip_wrong_calls("→ Citi keeps its Overweight rating.", [worded])[1]


def test_with_no_feed_line_the_turn_runs_without_a_body():
    """The old checks stand down when a body is posted, so a body of desk
    lines alone is not built."""
    results = {R.T_RESEARCH: {"status": "ok", "notes": [JPM]}}
    assert B.build("ticker_opinion", "NVDA", results, asked="what do you think of nvda", today=TODAY) is None
    text = O.build_outline({**results, R.T_PRIMER: {"status": "ok", "primer": "DRIVERS: Data center"}},
                           "ticker_opinion", "NVDA", asked="what do you think of nvda")
    assert "DESK: JPMorgan (2026-11-12): price target change, Overweight" in text


def test_the_rating_check_reads_a_ticker_between_the_verb_and_the_rating():
    assert D.strip_wrong_calls("→ JPMorgan upgraded NVDA to Underweight.", [JPM])[1]
    ok = "→ JPMorgan upgraded NVDA to Overweight."
    assert D.strip_wrong_calls(ok, [JPM]) == (ok, [])
    # ordinary words between them are not a ticker
    ok = "→ JPMorgan keeps a tight Hold on its estimates for now."
    assert D.strip_wrong_calls(ok, [JPM]) == (ok, [])
    assert D.strip_wrong_calls("→ JPMorgan has a target price of $125 on it.", [JPM])[1]


def test_the_reason_slot_reads_cleanly():
    note = {**JPM, "earnings": ["JPM sees F3Q revenue of $108B against the Street's $107B"]}
    assert D.reason(note) == ("PT cut reflects removal of China-related revenue from FY28 estimates and "
                              "increased management/regulatory risk. Its figures: JPM sees F3Q revenue of "
                              "$108B against the Street's $107B")


# JPMorgan on NVDA in the week of 2026-11-10, as stored.
JPM_SALES = {"source": "JPMorgan", "published": "2026-11-13", "report_type": "sales_trading",
             "title": "JPM Market Intel Morning Update",
             "calls": [{"action": "reiterate", "rating": "Underweight", "price_target": "$215",
                        "rationale": "Leadership transition, regulatory fallout, and circularity concerns in "
                                     "AI ecosystem spending.", "conviction": "medium"}],
             "insights": ["Credit analysts remain underweight NVDA due to leadership transition risks and "
                          "potential regulatory fallout from the new Congress."]}
JPM_CREDIT = {"source": "JPMorgan", "published": "2026-11-13", "report_type": "credit",
              "title": "NVIDIA Corporation: Can the Vision Outlast the Visionary?",
              "calls": [{"action": "reiterate", "rating": "Underweight", "price_target": "N/A",
                         "rationale": "Continued spread weakness expected due to leadership transition and "
                                      "legal/regulatory flux."}],
              "insights": ["JPMorgan remains Underweight on NVIDIA, citing spread weakness and the need for "
                           "clarity on governance."]}
GS_SALES = {"source": "Goldman Sachs", "published": "2026-11-09", "report_type": "sales_trading",
            "calls": [{"action": "positive_catalyst_watch", "rating": "Overweight", "price_target": "N/A",
                       "rationale": "GS favors NVDA in AI compute, noting current levels imply no growth "
                                    "from 2027."}]}


def test_only_a_houses_own_equity_research_is_trusted_with_its_fields():
    """The answer the audit flagged, "JPMorgan reiterates its Underweight
    rating with a $215 price target", came from the sales note's stored
    fields: the credit analysts' view beside the equity target."""
    assert D.line(JPM_SALES) == "" and D.line(JPM_CREDIT) == "" and D.line(GS_SALES) == ""
    assert D.fields(JPM_CREDIT)["credit"] and not D.fields(JPM)["credit"]
    for note in (JPM_SALES, JPM_CREDIT, GS_SALES):
        call = D.clean_note(note)["calls"][0]
        assert "rating" not in call and "price_target" not in call and call["rationale"]
    assert D.clean_note(JPM_SALES)["calls"][0].get("action") is None          # "reiterate" of nothing
    assert D.clean_note(GS_SALES)["calls"][0]["action"] == "positive_catalyst_watch"
    assert "debt" in D.clean_note(JPM_CREDIT)["desk"]
    # the equity note keeps its fields, with the target as it is posted
    assert D.clean_note(JPM)["calls"][0] == {**JPM["calls"][0], "price_target": "$215"}
    # what the model is handed for the other notes carries no rating
    assert "Underweight" not in O._desk_text(JPM_SALES) and "$215" not in O._desk_text(JPM_SALES)
    assert O._desk_text(JPM_CREDIT).startswith("its credit analysts, on the company's debt and not the stock: ")
    # a sales note's field is kept when its own sentence about the house states it
    stated = {**GS_SALES, "insights": ["Goldman Sachs reiterates its Buy rating on NVDA with a $250 target."],
              "calls": [{**GS_SALES["calls"][0], "action": "reiterate", "rating": "Buy", "price_target": "$250"}]}
    assert D.line(stated) == "→ **Goldman Sachs** (Nov 9): rated Buy, price target **$250**."


def test_of_one_banks_notes_the_equity_research_gets_the_desk_arrow():
    """Newest first put the sales note ahead of the equity note of the day before."""
    body, text = _turn([JPM_SALES, JPM_CREDIT, JPM])
    assert [d["line"] for d in body["desks"]] == [JPM_LINE]
    assert text.count("DESK: JPMorgan") == 1 and "PT cut reflects removal" in text
    assert "Underweight" not in text
    # the check on the model's words: the equity rating is Overweight
    assert D.strip_wrong_calls("→ JPMorgan reiterates its Underweight rating with a $215 price target.",
                               [D.clean_note(JPM_SALES), D.clean_note(JPM)])[1]


def test_a_note_with_a_postable_rating_ranks_ahead_of_newer_notes_without_one():
    """Second live sample, 2026-11-17: a strategy note and a roundup, both
    newer and neither with a postable rating, took the two desk arrows and
    JPMorgan's equity note was left out."""
    ms = {"source": "Morgan Stanley", "published": "2026-11-17", "report_type": "strategy",
          "calls": [{"action": "reiterate", "rating": "Overweight", "price_target": "$300.00",
                     "rationale": "Highest-conviction compute bucket.", "conviction": "high"}]}
    body, text = _turn([ms, TME_UNSTATED, JPM])
    assert [d["line"] for d in body["desks"]] == [JPM_LINE]
    slots = [ln for ln in text.splitlines() if "DESK:" in ln]
    assert len(slots) == O.MAX_DESKS and "DESK: JPMorgan" in slots[0] and "DESK: Morgan Stanley" in slots[1]
    assert "Overweight" not in slots[1] and "$300" not in slots[1]


def test_a_target_is_posted_only_when_it_is_one_price():
    """2026-11-18 (audit run 13 A1): "Morgan Stanley (Nov 17): price target
    31% upside" was posted on UNH from a strategy note's screen."""
    ms = {"source": "Morgan Stanley", "published": "2026-11-17", "report_type": "strategy",
          "calls": [{"action": "positive_catalyst_watch", "price_target": "31% upside",
                     "rationale": "AI Adoption Leader with 31% upside to the MS price target."}]}
    assert D.fields(ms)["target"] == "" and D.line(ms) == ""
    assert "price_target" not in D.clean_note(ms)["calls"][0]
    # the house's own equity research is held to the same form
    for bad in ("31% upside", "$550-$570", "150-146", "3,260 TWD / 620.00 USD", "raised"):
        assert D.fields(_note(bad, "x"))["target"] == "", bad
        assert "price target" not in D.line(_note(bad, "x")), bad
    for good, shown in (("$215.00", "$215"), ("HK$156", "HK$156"), ("GBX 1,940", "GBX 1,940"),
                        ("155p", "155p"), ("W500,000", "W500,000"), ("E80", "E80"),
                        ("$580 (from $710)", "$580"), ("€63.5", "€63.5")):
        assert D.fields(_note(good, "x"))["target"] == shown, good
