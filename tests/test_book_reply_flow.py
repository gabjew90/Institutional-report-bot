"""End-to-end book correction: published book -> caller says out -> close.

The parser is tested separately. This covers the wiring around it: the
book store, caller scoping, the time window, and that a `close` row
actually lands with the right contract fields.
"""
import asyncio
import sys

import db
from analyst_log import book_reply


# ------------------------------------------------------------- fakes

class _Author:
    def __init__(self, name, uid=999):
        self.name = name
        self.id = uid


class _Channel:
    def __init__(self, cid=555):
        self.id = cid
        self.name = "stonks-yapping"


class _Ref:
    def __init__(self, message_id):
        self.message_id = message_id


class _Msg:
    def __init__(self, content, author_name="bankerkyle", mid=1,
                 channel_id=555, ref_id=None):
        self.content = content
        self.author = _Author(author_name)
        self.channel = _Channel(channel_id)
        self.id = mid
        self.reference = _Ref(ref_id) if ref_id else None
        self.created_at = None


class _Settings:
    """Stands in for config.settings' caller registry."""
    @staticmethod
    def resolve_analyst_callers():
        return [
            {"name": "bankerkyle", "display": "BK",
             "username": "bankerkyle", "enabled": True},
            {"name": "abe", "display": "Abe",
             "username": "abullish_xyz", "enabled": True},
            {"name": "dormant", "display": "Dormant",
             "username": "dormantguy", "enabled": False},
        ]


_POSITIONS = [
    {"ticker": "MU", "contract_type": "call", "strike": 980.0,
     "expiry": "2026-08-28"},
    {"ticker": "AVGO", "contract_type": "call", "strike": 450.0,
     "expiry": "2026-09-04"},
    {"ticker": "HOOD", "contract_type": "put", "strike": 105.0,
     "expiry": "2026-08-28"},
]


def _run(msg, positions=None, book_caller="bankerkyle",
         book_tickers=("MU", "AVGO", "HOOD"), book_mid=None,
         book_channel=555):
    """Publish a book, deliver `msg`, return the recorded close rows."""
    import config
    recorded = []

    if book_mid is not None:
        db.record_bot_book_post(
            discord_message_id=book_mid, channel_id=book_channel,
            caller=book_caller, tickers=list(book_tickers))

    orig_settings = config.settings
    orig_positions = db.get_current_analyst_positions
    orig_record = db.record_analyst_trade
    try:
        config.settings = _Settings()
        db.get_current_analyst_positions = (
            lambda **kw: list(positions if positions is not None
                              else _POSITIONS))
        db.record_analyst_trade = lambda **kw: recorded.append(kw)
        n = asyncio.run(book_reply.handle_book_correction(None, msg))
    finally:
        config.settings = orig_settings
        db.get_current_analyst_positions = orig_positions
        db.record_analyst_trade = orig_record
    assert n == len(recorded)
    return recorded


# ------------------------------------------------------- the incident

def test_standalone_correction_after_book_records_close():
    """BK's real case: no reply reference, no ping, seconds after the
    book. A reply-only trigger would miss this entirely."""
    rows = _run(_Msg("No AVGO", mid=101), book_mid=100)
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "AVGO"
    assert r["action"] == "close"
    assert r["strike"] == 450.0
    assert r["expiry"] == "2026-09-04"
    assert r["contract_type"] == "call"
    assert r["caller"] == "bankerkyle"


def test_close_carries_no_invented_price():
    """He said he was out, not at what. gain_pct/price stay NULL so the
    W/L tally books it as closed_unscored rather than a fake number."""
    r = _run(_Msg("No MU anymore", mid=102), book_mid=100)[0]
    assert r["gain_pct"] is None
    assert r["price"] is None


def test_direct_reply_to_the_book_also_works():
    rows = _run(_Msg("out of AVGO", mid=103, ref_id=100), book_mid=100)
    assert [r["ticker"] for r in rows] == ["AVGO"]


# ------------------------------------------------------------ scoping

def test_no_book_no_correction():
    """Without a published book there is no antecedent, so "No AVGO" is
    just a sentence.

    Uses its own channel id: the other tests leave fresh books in 555,
    and reusing it here would let one of those satisfy the lookup and
    make this pass for the wrong reason.
    """
    assert _run(_Msg("No AVGO", mid=104, channel_id=8801),
                book_mid=None) == []


def test_other_callers_book_is_not_corrected():
    """Abe cannot close Kyle's positions."""
    rows = _run(_Msg("No AVGO", author_name="abullish_xyz", mid=105),
                book_caller="bankerkyle", book_mid=110)
    assert rows == []


def test_non_caller_cannot_correct():
    """A random member saying "No AVGO" changes nothing."""
    rows = _run(_Msg("No AVGO", author_name="someguy", mid=106),
                book_mid=111)
    assert rows == []


def test_disabled_caller_cannot_correct():
    rows = _run(_Msg("No AVGO", author_name="dormantguy", mid=107),
                book_caller="dormant", book_mid=112)
    assert rows == []


def test_different_channel_is_not_in_scope():
    """The book was published in another room; a bare line here is not
    anchored to it."""
    rows = _run(_Msg("No AVGO", mid=108, channel_id=777),
                book_mid=113, book_channel=555)
    assert rows == []


def test_ticker_not_currently_open_records_nothing():
    """The book can be seconds stale. Only live positions are closed."""
    rows = _run(_Msg("No AVGO", mid=109),
                positions=[p for p in _POSITIONS if p["ticker"] != "AVGO"],
                book_mid=114)
    assert rows == []


def test_only_the_named_position_closes():
    """Two other positions are open and must be left alone."""
    rows = _run(_Msg("No AVGO", mid=115), book_mid=116)
    assert [r["ticker"] for r in rows] == ["AVGO"]


def test_non_exit_message_records_nothing():
    rows = _run(_Msg("^ this plus the SMCI 39c and I added 1 more SPX",
                     mid=117), book_mid=118)
    assert rows == []


def test_multiple_positions_get_distinct_attachment_ids():
    """One message closing two positions must not collide on the
    UNIQUE(message_id, attachment_id) constraint — that would silently
    drop the second close."""
    rows = _run(_Msg("out of MU and out of AVGO", mid=119), book_mid=120)
    assert len(rows) == 2
    ids = [r["discord_attachment_id"] for r in rows]
    assert len(set(ids)) == 2
    # and clear of the caption-only row's synthetic id 0
    assert all(i != 0 for i in ids)


# -------------------------------------------------------- the store

def test_book_store_roundtrip_and_window():
    db.record_bot_book_post(discord_message_id=201, channel_id=901,
                            caller="bankerkyle", tickers=["mu", "avgo"])
    got = db.get_bot_book_post(201)
    assert got["caller"] == "bankerkyle"
    assert got["tickers"] == ["AVGO", "MU"]      # normalized + sorted

    recent = db.find_recent_book_posts(channel_id=901, within_minutes=10)
    assert any(b["message_id"] == 201 for b in recent)

    # An old book falls out of the window.
    assert not db.find_recent_book_posts(channel_id=901,
                                         within_minutes=-5)


def test_stored_timestamp_uses_sqlite_format():
    """posted_at must come from datetime('now'), NOT Python isoformat().

    find_recent_book_posts compares against datetime('now', ...).
    SQLite writes 'YYYY-MM-DD HH:MM:SS' with a space; Python writes a
    'T'. TEXT comparison sorts 'T' after ' ', so a T-format row silently
    passes every window check no matter how old it is — the same defect
    documented for the pulse cutoffs. Assert the format directly rather
    than inferring it from a window that happens to agree.
    """
    db.record_bot_book_post(discord_message_id=204, channel_id=905,
                            caller="abe", tickers=["RKLB"])
    ts = db.get_connection().execute(
        "SELECT posted_at FROM bot_book_posts WHERE discord_message_id=204"
    ).fetchone()[0]
    assert "T" not in ts, (
        f"posted_at is {ts!r} — T-format breaks the window comparison")
    assert len(ts) == 19, f"unexpected timestamp shape: {ts!r}"


def test_book_store_scoped_by_channel():
    db.record_bot_book_post(discord_message_id=202, channel_id=902,
                            caller="abe", tickers=["RKLB"])
    assert not [b for b in db.find_recent_book_posts(channel_id=903)
                if b["message_id"] == 202]


def test_prune_drops_nothing_fresh():
    db.record_bot_book_post(discord_message_id=203, channel_id=904,
                            caller="abe", tickers=["RKLB"])
    db.prune_bot_book_posts(keep_days=3)
    assert db.get_bot_book_post(203) is not None


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
