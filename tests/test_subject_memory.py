"""What the room was already told about a subject (2026-10-08 audit: the
bot listed Anduril exposure without KRKNF, which it named the day before)."""
import db
from discord_bot import ask_router as R


def test_earlier_answers_about_the_subject_are_found():
    db.record_ask_bot_answer(5551, 777, "who partners with Anduril on boats",
                             "Kraken Robotics ($KRKNF) supplies batteries to Anduril.")
    db.record_ask_bot_answer(5552, 778, "whats nvda at", "NVDA is at 180")
    terms = R.subject_terms("what are public companies with exposure to Anduril?")
    assert terms == ["Anduril"]
    rows = db.bot_answers_mentioning(terms)
    assert rows and "KRKNF" in rows[0]["answer"]
    assert db.bot_answers_mentioning([]) == []
