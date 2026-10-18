"""The /ask dash rewrite (bot._clean_voice_violations), 2026-10-18 audit A28.

"Net sales guided at €11.0B–€12.0B" shipped as "€11.0B, €12.0B", and
"by 2027—rivaling" shipped with its em-dash.
"""
from discord_bot.bot import _clean_voice_violations as clean


def test_a_currency_range_reads_to():
    out, _ = clean("Net sales guided at **€11.0B–€12.0B** for the quarter.")
    assert "€11.0B to €12.0B" in out


def test_a_dash_glued_to_a_year_before_a_word_is_a_comma():
    out, _ = clean("1 to 2 gigawatts of capacity by 2027—rivaling small-city loads.")
    assert "—" not in out and "2027, rivaling" in out


def test_plain_ranges_still_read_to():
    out, _ = clean("Shares traded 10 – 12 all day.")
    assert "10 to 12" in out
