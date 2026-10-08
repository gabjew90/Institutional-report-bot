"""Which members a question names (db.find_users_mentioned_in_text,
2026-10-08 audit): "report" loaded reportfirst1112 and a slur loaded a
member whose display name starts with it."""
import db


def _profile(uid, username, display):
    db.upsert_user_profile(user_id=uid, username=username, display_name=display,
                           profile_text="x", message_count_at_update=1,
                           last_seen_message_at=None)


def test_prefix_matching_needs_a_letters_only_name():
    _profile(910001, "reportfirst1112", "Texas Rosa")
    _profile(910002, "zach_m_77", "Zachary M")
    found = db.find_users_mentioned_in_text("what time apld report earnings")
    assert 910001 not in found
    assert 910002 in db.find_users_mentioned_in_text("what did zach say about nvda")


def test_a_slur_never_prefix_matches_a_member():
    from scripts.slur_patterns import count_racial_slurs
    _profile(910003, "dovahjo_t", "Niggahjo")
    word = "nigga"
    assert count_racial_slurs(word) > 0
    assert 910003 not in db.find_users_mentioned_in_text(f"LMAO I wish a {word} would")
