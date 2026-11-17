"""A desk's rating and price target are posted by code (discord_bot/desk_line.py,
2026-11-17, audit run 12 A1: JPMorgan's Overweight on NVDA, target cut to $215
from $320, was posted as "reiterates its Underweight rating")."""
from datetime import date

from discord_bot import ask_router as R
from discord_bot import desk_line as D
from discord_bot import stock_body as B
from discord_bot import stock_outline as O

JPM = {"source": "JPMorgan", "published": "2026-11-12",
       "calls": [{"action": "price_target_change", "rating": "Overweight", "price_target": "$215.00",
                  "rationale": "PT cut reflects removal of China-related revenue from FY28 estimates "
                               "and increased management/regulatory risk.", "conviction": "medium"}],
       "insights": ["JPMorgan maintains an Overweight rating on NVDA but cuts the price target to $215 "
                    "from $320, citing leadership uncertainty and increased regulatory risk regarding China."]}
JPM_LINE = "→ **JPMorgan** (Nov 12): rated Overweight, price target **$215**, cut from $320."
GS = {"source": "Goldman Sachs", "published": "2026-11-10",
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
    return {"source": "Goldman Sachs", "published": "2026-11-05",
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
    relayed = _note("N/A", "cut to Neutral at Piper Sandler", action="downgrade", rating="Neutral")
    relayed["calls"][0]["call_by"] = "Piper Sandler"
    assert D.line(relayed) == "→ **Piper Sandler**, as relayed in a Goldman Sachs note (Nov 5): downgraded to Neutral."


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
