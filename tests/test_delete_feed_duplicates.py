"""The feed cleanup keeps one copy and only touches long repeats by the bot."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from delete_feed_duplicates import repeats


def _post(mid, title, author="bot"):
    return {"id": str(mid), "author": {"id": author}, "embeds": [{"title": title}], "timestamp": "2026-11-02T16:37:00"}


def test_keeps_the_earliest_copy_and_returns_the_rest():
    msgs = [_post(i, "GS TMT") for i in (5, 3, 9, 1, 7, 2)]
    out = repeats(msgs, "bot")
    assert [m["id"] for m in out["GS TMT"]] == ["2", "3", "5", "7", "9"]


def test_a_short_repeat_can_be_two_files_and_is_left_alone():
    assert repeats([_post(i, "Daily Tech Index") for i in range(4)], "bot") == {}


def test_the_catch_up_summary_is_left_alone():
    assert repeats([_post(i, "Ingestion catch-up") for i in range(9)], "bot") == {}


def test_someone_elses_posts_and_posts_with_no_title_are_left_alone():
    msgs = [_post(i, "GS TMT", author="member") for i in range(9)]
    msgs += [{"id": str(i), "author": {"id": "bot"}, "embeds": []} for i in range(10, 19)]
    assert repeats(msgs, "bot") == {}
