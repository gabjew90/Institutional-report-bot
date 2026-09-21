"""Repetition is what the ROOM hears, not what one asker hears.

2026-09-18: Ry_spaceman asked at 22:31, BK at 22:32 and again at 22:33,
BK once more at 22:37. Four answers, the same cached line about the
same member ("industrial-grade operation on unbridled slurs and
Facebook ragebait"). The recycle guard saw none of it: it looked up
prior answers per asker, and every one of those was that asker's first
ask. The ranking shape also carries no clapback shape, which the guard
required."""
import inspect

from discord_bot import bot as B

A1 = ("Grand Nagus Yeezy takes the crown by a mile. Guy's basically running an "
      "industrial-grade operation on unbridled slurs and Facebook ragebait while "
      "live-action roleplaying a feudal lord.")
A2 = ("Grand Nagus Yeezy still owns the belt by a wide margin. Guy's running an "
      "industrial-grade operation on unbridled slurs and Facebook ragebait while "
      "live-action roleplaying a feudal lord.")
FRESH = ("Sam wins it on volume alone, mostly because he cannot get through a "
         "sentence about yachts without narrating the brochure.")


def test_the_repeated_9_18_ranking_is_detected():
    shared = B._recycled_roast_hooks(A2, [A1])
    assert len(shared) >= B._RECYCLE_HOOK_MIN, shared


def test_a_genuinely_new_answer_is_not_flagged():
    shared = B._recycled_roast_hooks(FRESH, [A1])
    assert len(shared) < B._RECYCLE_HOOK_MIN, shared


def test_the_channel_history_feeds_the_guard():
    src = inspect.getsource(B._ask_02_classify_and_prefetch) if hasattr(
        B, "_ask_02_classify_and_prefetch") else ""
    if not src:
        import pathlib
        src = pathlib.Path("discord_bot/bot.py").read_text(encoding="utf-8")
    assert "get_recent_bot_answers_in_channel" in src
    assert "_prior_bot_answer_texts.append" in src


def test_the_verbatim_span_guard_catches_what_the_shape_gate_hides():
    """The repeated answers were rankings, which carry no clapback
    shape, so the hook guard could never see them. Removing that gate
    is not the fix: it is the gate that stops this guard rewriting a
    factual answer into a jab (the Boeing incident, pinned by
    smoke_rewrite_guards_keep_question). A verbatim-span check is safe
    without it because its rewrite only asks for different words."""
    assert not B._is_clapback_shaped(A1)
    assert B._repeated_span(A2, [A1])
    src = inspect.getsource(B._ask_06_roast_subject_guards)
    i = src.index("_repeated_span(answer")
    window = src[max(0, i - 500):i]
    assert "_is_clapback_shaped" not in window
    # the hook guard keeps its gate
    j = src.index("_recycled_roast_hooks(answer")
    assert "_is_clapback_shaped(answer)" in src[max(0, j - 400):j]


def test_the_span_guard_ignores_short_and_novel_answers():
    assert B._repeated_span(FRESH, [A1]) == ""
    assert B._repeated_span("too short to count", [A1]) == ""
    assert B._repeated_span(A2, []) == ""


def test_the_repeated_span_is_the_cached_profile_line():
    # spans are tokenized ("industrial-grade" == "industrial grade") and
    # the window is 12 words, so it starts mid-phrase; what matters is
    # that it carries the cached profile's distinctive material
    span = B._repeated_span(A2, [A1])
    assert "operation on unbridled slurs and facebook ragebait" in span
    assert B._span_is_distinctive(span)


def test_the_channel_lookup_is_short_windowed():
    import db
    import inspect as _i
    src = _i.getsource(db.get_recent_bot_answers_in_channel)
    assert "max_age_days: int = 2" in src
    assert "channel_id = ?" in src


def test_the_bar_is_twelve_words_and_a_span_must_carry_material():
    """12, not 8 (2026-09-19 review): eight words of connective prose
    recurs between answers about different people and a reword there
    costs a model call to fix nothing. A shared run that is purely
    function words is not material."""
    assert B._REPEATED_SPAN_MIN_WORDS == 12
    eight = "is right there on the podium competing for"
    assert B._repeated_span(f"He {eight} silver.", [f"She {eight} bronze."]) == ""
    assert not B._span_is_distinctive("and of to it is a the was in for on with")
    # a long run of real words is a repeat, and should still trip
    assert B._span_is_distinctive("operation on unbridled slurs and facebook ragebait")
