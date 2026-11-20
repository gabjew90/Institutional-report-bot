"""scripts/ask_outcomes.py: room corrections of /ask answers (2026-10-28).

The pattern was measured against 316 hand-labelled replies to the bot from
2026-09-01 to 10-26: 44 of 50 corrections caught, 3 false alarms. The
examples below are taken from those labels."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ask_outcomes as O  # noqa: E402

CORRECTIONS = [
    "you missed dell",
    "you forgot panw",
    "this is inaccurate as it was over 30000",
    "GMS is owned by home depot silly",
    "Useless robot it's already live",
    "But you just said it's closed.",
    "Be more specific",
    "Wrong team. I am asking about the team in our league with McCaffrey",
    "I said gay not racist, pls read",
    "That's a lie. All -95%",
    "hmm, I am only in one of those",
    "Jsn is playing tonight not Monday",
    "That's too long",
    "No u buffoon im referring to western digital and seagate",
    "Why would I care about before the bell idiot",
]
NOT_CORRECTIONS = [
    "Thanks man much nicer than Abe",
    "Correct thats what i was getting too",
    "What time is the announcement",
    "Good shit man keep up the good work",
    "So a 100$ parlay wouldve paid 17.5k?",
    "Nice I'm already in",
    "Will trump save us",
    "what is his record on just puts in that timeframe?",
    "Who does he have left",
]


def test_corrections_and_banter():
    for t in CORRECTIONS:
        assert O.is_correction(t), t
    for t in NOT_CORRECTIONS:
        assert not O.is_correction(t), t


def _reply(text: str, parent: str) -> str:
    return ("[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n"
            f"\"{parent}\"\n\n[Z's message to you]\n{text}")


def _e(ts, question, answer="", asker="z", channel="stonks"):
    return {"ts": ts, "question": question, "answer": answer, "asker": asker, "channel": channel}


def test_a_correction_lands_on_the_answer_it_quotes():
    slate = _e("2026-10-20 19:30:00", "who reports today",
               "→ **After the bell:** NFLX, ISRG, TXN report after the close")
    other = _e("2026-10-20 19:35:00", "price of NVDA", "→ **NVDA** at $181.20")
    fix = _e("2026-10-20 19:38:00",
             _reply("You forgot spcx", "→ **After the bell:** NFLX, ISRG, TXN report after the close"))
    thanks = _e("2026-10-20 19:39:00", _reply("thanks bro", "→ **NVDA** at $181.20"))
    es = [slate, other, fix, thanks]
    assert O.mark(es) == {"unplaced_corrections": 0}
    assert slate["corrected"] and slate["corrections"] == ["You forgot spcx"]
    assert not other["corrected"] and not fix["corrected"]


def test_marking_twice_counts_once_and_stays_in_the_channel():
    a = _e("2026-10-20 19:30:00", "q", "→ the same canned reply about the slate", channel="stonks")
    b = _e("2026-10-20 19:31:00", "q", "→ the same canned reply about the slate", channel="crypto")
    fix = _e("2026-10-20 19:32:00", _reply("that's wrong", "→ the same canned reply about the slate"),
             channel="stonks")
    es = [a, b, fix]
    O.mark(es)
    O.mark(es)
    assert a["corrections"] == ["that's wrong"] and not b["corrected"]


def test_a_correction_quoting_nothing_known_is_counted_unplaced():
    es = [_e("2026-10-20 19:38:00", _reply("that's wrong", "an answer from before the window"))]
    assert O.mark(es) == {"unplaced_corrections": 1}


def test_the_same_question_again_within_ten_minutes_is_a_reask():
    a = _e("2026-09-09 22:00:00", "how many total messages have I sent in this channel")
    b = _e("2026-09-09 22:05:00", "how many total messages have I sent in this channel??")
    c = _e("2026-09-09 22:30:00", "how many total messages have I sent in this channel")
    other = _e("2026-09-09 22:31:00", "how many total messages have I sent in this channel", asker="bk")
    O.mark([a, b, c, other])
    assert a["reasked"] and not b["reasked"] and not c["reasked"]


# 2026-11-20: a fresh hand-labelled month (10-27 to 11-19) showed what the
# measure never tested. The examples are written for the test, in the shape
# of the labelled replies.

def test_a_reask_posted_as_a_reply_counts():
    answer = "→ **AAPL** closed at $255.46. Reports today after the close."
    a = _e("2026-11-17 21:26:45", "what time is the Apple earnings call", answer)
    b = _e("2026-11-17 21:27:25", _reply("what time is the call", answer))
    nxt = _e("2026-11-17 21:28:00", _reply("how about the guidance", answer))
    O.mark([a, b, nxt])
    assert a["reasked"] and a["did_not_land"] and not a["corrected"]
    assert not b["reasked"] and not b["did_not_land"]


def test_one_symbol_from_a_question_that_named_several():
    assert O.restates("and MSFT", "aapl, msft and nvda earnings expectations")
    # one shared word is no re-ask on its own
    assert not O.restates("no shot", "is there a shot it holds 400")
    assert not O.restates("how about the cash flow", "is amazon cash flow negative")


def test_a_canned_line_is_no_answer():
    assert O.gave_up("→ Thought myself in circles and ran out of room. Try asking it more directly.")
    assert O.gave_up("→ No response came back (reason: STOP). Try again or rephrase.\n\nData: x")
    # the cleaner turns the dash into a comma
    assert O.gave_up("→ don't have that one, pull it from your broker or data vendor.")
    assert not O.gave_up("→ **AAPL** at $255.46")
    e = _e("2026-11-11 21:27:43", "average return on Thursdays",
           "→ Thought myself in circles and ran out of room. Try asking it more directly.")
    O.mark([e])
    assert e["gave_up"] and e["did_not_land"]


def test_measure_only_corrections_and_the_banter_that_looks_like_one():
    for t in ("You have to work each one out separately", "give me a number",
              "So again, who makes the playoffs"):
        assert O.is_correction(t), t
    for t in ("I'm asking for a friend", "No shit", "I'm not a fucking pussy",
              "do you have to hold through the print", "give me the price of NVDA and AMD"):
        assert not O.is_correction(t), t
    # the live route's pattern is not widened by the measure
    from discord_bot import follow_up
    assert not follow_up.is_correction("give me a number")
