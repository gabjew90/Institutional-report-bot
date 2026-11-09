"""Changes from the 2026-11-09 review of /ask's after-the-fact checks.

A replay of every answer a check changed over 60 days found the grounding
re-run replacing drafts built from the turn's own data, and the fantasy
win-percent strip skipping the floor every other strip obeys."""
from types import SimpleNamespace as NS

from google.genai import types

from discord_bot import bot as B


def _contents(*texts, payload=None):
    out = [types.Content(role="user", parts=[types.Part.from_text(text=t)]) for t in texts]
    if payload is not None:
        out.append(types.Content(role="user", parts=[types.Part.from_function_response(
            name="lookup_ticker_snapshot", response=payload)]))
    return out


_NO_RESP = NS(candidates=[])


def test_a_draft_built_from_a_tool_payload_is_on_its_own_data():
    """2026-10-07: CRWD's fundamentals off the snapshot were replaced by a
    Google answer about last quarter."""
    draft = ("→ **Market cap** **$285.5B** at **$278.86**\n"
             "→ **Free cash flow** **$2.0B TTM**, **$5.01B** cash, **$820M** debt")
    ev = B._ask_evidence_text(
        _contents(payload={"market_cap": 285.5e9, "price": 278.86, "fcf_ttm": 2.0e9,
                           "cash": 5.01e9, "debt": 820e6}),
        _NO_RESP, "what are the fundamentals for CRWD", "", chat=False)
    assert B._draft_on_own_data(draft, ev)


def _own_evidence(contents, question, user_content=""):
    """What phase 7 passes: code-supplied data and the bot's replied message."""
    from discord_bot.fantasy_guard import _bot_parent
    return B._ask_evidence_text(contents, _NO_RESP, _bot_parent(question), user_content,
                                chat=False)


def test_a_figure_from_the_bots_replied_message_is_own_data():
    """2026-09-25: a reply to the bot's own trade-log line about Abe's SMCI
    close was re-run into an answer about the Strait of Hormuz."""
    q = ('[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n'
         '"Logged: CLOSE SMCI 42C 09-25 (+28.0%)"\n'
         "[Monsoon's message to you] So strait is closed ?")
    draft = "→ **Yes, closed**, abe locked in **+28.0%** on the **SMCI 42C 09-25**"
    assert B._draft_on_own_data(draft, _own_evidence(_contents(), q))


def test_the_askers_own_figure_is_not_own_data():
    """Code review 2026-11-09: echoing the asker's claim must not skip the search."""
    q = "is it true TSLA delivered 500,000 cars last quarter?"
    assert not B._draft_on_own_data("→ Yes, $TSLA delivered **500,000** cars.",
                                    _own_evidence(_contents(), q))


def test_repeat_span_only_records_2026_11_09():
    import inspect
    src = inspect.getsource(B._ask_06_roast_subject_guards)
    i = src.index('append("repeat-span")')
    assert "repeat-span:stripped" not in src and "_strip_sentences" not in src[i:]


def test_a_figure_only_in_chat_is_not_own_data():
    """A market figure a member pasted still needs a source."""
    uc = "Recent channel chat (oldest first):\nbk: nvda market cap is 5.2T"
    draft = "→ **$NVDA** market cap sits at **$5.2T**"
    ev = B._ask_evidence_text(_contents(), _NO_RESP, "nvda market cap?", uc, chat=False)
    assert not B._draft_on_own_data(draft, ev)
    # the figure check itself still reads chat
    assert "5.2T" in B._ask_evidence_text(_contents(), _NO_RESP, "nvda market cap?", uc)


def test_an_invented_figure_or_no_figure_is_not_own_data():
    ev = B._ask_evidence_text(_contents(payload={"price": 278.86}), _NO_RESP, "crwd?", "",
                              chat=False)
    assert not B._draft_on_own_data("→ **$278.86**, and FY revenue of **$6.4B**", ev)
    assert not B._draft_on_own_data("→ it trades on the Nasdaq", ev)


def test_the_rule_covers_all_three_nets_in_phase_7():
    import inspect
    src = inspect.getsource(B._ask_07_validation_ladder)
    assert "(_ground_trigger_web or _ground_trigger_shape or _ground_trigger_calendar)" in src
    assert "_draft_on_own_data(" in src and "chat=False" in src


def test_the_fantasy_percent_strip_obeys_the_floor():
    answer = ("→ Tulch is at 55.52 against your 32.04 with three players left, and "
              "Lloyd, Kraft and Dicker still have to play on your side tonight, which "
              "is a 62% win chance if Kraft gets loose in the second half.")
    meta = {"route_shape": "fantasy", "fantasy_estimates": {"Tulch": 81, "BK": 19},
            "guards": []}
    out = B._fantasy_percent_guard(answer, "how am I looking", meta)
    assert out == answer and "fantasy-percent:kept" in meta["guards"]


def test_the_fantasy_percent_strip_still_cuts_one_sentence_of_many():
    answer = ("→ Tulch is at 55.52 against your 32.04 with three players left on "
              "your side and none on his, so the gap is all yours to close tonight.\n"
              "→ Lloyd, Kraft and Dicker are still to play, and Kraft has the best "
              "matchup of the three against a soft secondary.\n"
              "→ Call it a 62% win chance.")
    meta = {"route_shape": "fantasy", "fantasy_estimates": {"Tulch": 81, "BK": 19},
            "guards": []}
    out = B._fantasy_percent_guard(answer, "how am I looking", meta)
    assert "62%" not in out and "fantasy-percent:stripped" in meta["guards"]
