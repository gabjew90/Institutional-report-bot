"""Smoke test for the repetition-glitch strip fallback.

Context (2026-07-23): the 07-22 16:49 terlin calendar answer shipped a
token-loop glitch ("...as the session progresses through-bank updates as
the session progresses through-specific releases...") to Discord. The
detector FLAGGED it, but the one-shot higher-temp retry re-glitched and
the failure path shipped the original untouched, with no ask-log marker.

Covers:
  - _repetition_glitch_sentences finds exactly the glitching sentence(s)
  - clean multi-bullet answers return no glitch sentences
  - stripping the glitch sentences yields a clean, non-flagged remainder
  - an answer that is ALL glitch strips to empty (caller ships original)
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# The real 07-22 16:49 shipped answer (arrow prefix simplified — the
# detector tokenizes alpha runs only, so the glyph choice is irrelevant).
_CLEAN_BULLET_1 = (
    "-> **Today (After Market):** Primary focus is **Alphabet (GOOGL)**, "
    "with **CSX Corporation (CSX)** and **Century Communities (CCS)** "
    "also reporting."
)
_CLEAN_BULLET_2 = (
    "-> **Tomorrow (Before Open):** Notable names include **American "
    "Airlines (AAL)**, **RTX Corporation (RTX)**, **Honeywell (HON)**, "
    "and **Newmont (NEM)**."
)
_GLITCH_BULLET = (
    "-> **Tomorrow (After Market):** The docket is lighter, check your "
    "broker for specific small-cap or regional releases as the session "
    "progresses through-bank updates as the session progresses "
    "through-specific releases as the session progresses through-late-day "
    "prints as the schedule firms up-filings as-needed updates."
)
_FULL_ANSWER = f"{_CLEAN_BULLET_1}\n\n{_CLEAN_BULLET_2}\n\n{_GLITCH_BULLET}"


def test_glitch_sentences_found():
    from discord_bot.bot import _repetition_glitch_sentences
    sents = _repetition_glitch_sentences(_FULL_ANSWER)
    assert sents, "expected the glitch bullet to be identified"
    assert any("session progresses" in s for s in sents), sents
    assert all("GOOGL" not in s for s in sents), (
        f"clean bullet wrongly flagged: {sents}"
    )
    _ok("glitch sentence identified, clean bullets untouched")


def test_clean_answer_no_glitch_sentences():
    from discord_bot.bot import _repetition_glitch_sentences
    clean = f"{_CLEAN_BULLET_1}\n\n{_CLEAN_BULLET_2}"
    assert _repetition_glitch_sentences(clean) == [], (
        "clean answer produced glitch sentences"
    )
    _ok("clean multi-bullet answer -> no glitch sentences")


def test_strip_yields_clean_remainder():
    from discord_bot.bot import (
        _repetition_glitch_sentences,
        _strip_sentences,
        _has_repetition_glitch,
    )
    sents = _repetition_glitch_sentences(_FULL_ANSWER)
    remainder = _strip_sentences(_FULL_ANSWER, sents)
    assert remainder, "strip left nothing"
    assert "GOOGL" in remainder and "Honeywell" in remainder, remainder
    assert "session progresses through-bank" not in remainder, remainder
    assert not _has_repetition_glitch(remainder), (
        "remainder still flagged after strip"
    )
    _ok("strip removes glitch bullet, remainder clean and unflagged")


def test_all_glitch_strips_to_empty():
    from discord_bot.bot import (
        _repetition_glitch_sentences,
        _strip_sentences,
    )
    sents = _repetition_glitch_sentences(_GLITCH_BULLET)
    assert sents, "single glitch bullet not identified"
    remainder = _strip_sentences(_GLITCH_BULLET, sents)
    assert not remainder.strip(), (
        f"expected empty remainder, got: {remainder!r}"
    )
    _ok("all-glitch answer strips to empty (caller keeps original)")


if __name__ == "__main__":
    print("=== repetition strip fallback smoke ===")
    test_glitch_sentences_found()
    test_clean_answer_no_glitch_sentences()
    test_strip_yields_clean_remainder()
    test_all_glitch_strips_to_empty()
    print("\nALL REPETITION STRIP FALLBACK SMOKE TESTS PASS")
