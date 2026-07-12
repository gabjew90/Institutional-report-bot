"""Smoke: exit-linking stage 2 (2026-07-11 ledger review).

Position identity is (ticker, contract_type, strike, expiry), but P&L
close cards commonly show only ticker + gain% — the extracted close row
lands with NULL strike/expiry, partitions separately, the open stays
"live" forever ('no exit posted'), and outcome coverage starves (40 of
447 member rows had outcomes). Stage-2 inheritance: a strikeless close
inherits every missing contract field from the scope's most recent
UNCLOSED open/add on the same ticker; backfill_orphan_exit_links
repairs history at boot.

Behavioral test against a temp DB (env DB_PATH set before importing
db, so the singleton connection binds to the fixture)."""

import os
import sys
import tempfile

_TMP = os.path.join(tempfile.mkdtemp(prefix="smoke_exit_"), "t.db")
os.environ["DB_PATH"] = _TMP
os.environ.setdefault("PDF_DOWNLOAD_DIR", os.path.dirname(_TMP))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402  (binds to the temp DB)


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_MSG = [1000]


def _trade(author_id, ticker, action, *, strike=None, expiry=None,
           ct=None, gain=None, posted_at):
    _MSG[0] += 1
    db.record_analyst_trade(
        discord_message_id=_MSG[0],
        discord_attachment_id=1,
        author=f"user{author_id}",
        author_id=author_id,
        posted_at=posted_at,
        image_url=None,
        caption=None,
        is_trade=True,
        gemini_json=None,
        ticker=ticker,
        contract_type=ct,
        strike=strike,
        expiry=expiry,
        action=action,
        gain_pct=gain,
        tracking_mode="member",
    )
    row = db.get_connection().execute(
        "SELECT * FROM analyst_trades WHERE discord_message_id = ?",
        (_MSG[0],),
    ).fetchone()
    return dict(row)


def test_strikeless_close_inherits():
    _trade(1, "TSLA", "open", strike=430.0, expiry="2026-07-18",
           ct="call", posted_at="2026-07-08 14:00:00")
    close = _trade(1, "TSLA", "close", gain=55.0,
                   posted_at="2026-07-10 15:00:00")
    assert close["strike"] == 430.0, close
    assert close["expiry"] == "2026-07-18", close
    assert close["contract_type"] == "call", close
    assert close["inferred_status"] is None, \
        f"matched close must not be tagged close_without_open: {close}"
    _ok("strikeless close inherits strike/expiry/type from the open")


def test_scope_isolation_and_no_overwrite():
    # author 2's orphan close must NOT inherit author 1's open
    orphan = _trade(2, "TSLA", "close", gain=30.0,
                    posted_at="2026-07-10 16:00:00")
    assert orphan["strike"] is None, \
        f"cross-author inheritance must not happen: {orphan}"
    # a close WITH its own strike keeps it (never overwritten)
    _trade(3, "NVDA", "open", strike=145.0, expiry="2026-08-15",
           ct="call", posted_at="2026-07-09 10:00:00")
    keep = _trade(3, "NVDA", "close", strike=150.0, gain=20.0,
                  posted_at="2026-07-10 17:00:00")
    assert keep["strike"] == 150.0, keep
    _ok("scope isolation holds; extracted strike never overwritten")


def test_closed_entry_not_reused():
    # author 1's TSLA position closed in test 1 — a SECOND orphan close
    # can't inherit from the same entry.
    again = _trade(1, "TSLA", "close", gain=10.0,
                   posted_at="2026-07-11 15:00:00")
    assert again["strike"] is None, \
        f"an already-closed entry must not be reused: {again}"
    _ok("one entry links one exit; second orphan close stays orphan")


def test_position_rollup_sees_the_close():
    # author 4 opens; strikeless close arrives; the rollup must no
    # longer list the position as live.
    _trade(4, "GEO", "open", strike=31.0, expiry="2099-12-17",
           ct="call", posted_at="2026-07-08 14:00:00")
    live_before = db.get_current_analyst_positions(tracking_mode="member")
    assert any(p["ticker"] == "GEO" for p in live_before), live_before
    _trade(4, "GEO", "close", gain=42.0, posted_at="2026-07-10 18:00:00")
    live_after = db.get_current_analyst_positions(tracking_mode="member")
    assert not any(p["ticker"] == "GEO" for p in live_after), \
        f"linked close must end the position: {live_after}"
    _ok("position rollup: linked close ends the open position")


def test_backfill_repairs_history():
    # Orphan close exists BEFORE its entry was extracted (backfill /
    # out-of-order ingestion). The write-time pass can't fix it; the
    # boot backfill does.
    orphan = _trade(5, "AMD", "close", gain=66.0,
                    posted_at="2026-07-10 12:00:00")
    assert orphan["strike"] is None
    _trade(5, "AMD", "open", strike=180.0, expiry="2026-07-25",
           ct="call", posted_at="2026-07-09 09:00:00")
    n = db.backfill_orphan_exit_links()
    assert n >= 1, f"backfill should link at least the AMD orphan: {n}"
    row = db.get_connection().execute(
        "SELECT strike, expiry FROM analyst_trades "
        "WHERE discord_message_id = ?", (orphan["discord_message_id"],),
    ).fetchone()
    assert row["strike"] == 180.0 and row["expiry"] == "2026-07-25", dict(row)
    # idempotent: second run links nothing new for AMD
    n2 = db.backfill_orphan_exit_links()
    assert n2 == 0 or n2 < n, f"backfill must be idempotent: {n2}"
    _ok("boot backfill links historical orphans; idempotent")


if __name__ == "__main__":
    print("=== exit-linking smoke (temp DB) ===")
    test_strikeless_close_inherits()
    test_scope_isolation_and_no_overwrite()
    test_closed_entry_not_reused()
    test_position_rollup_sees_the_close()
    test_backfill_repairs_history()
    print("\nALL EXIT-LINKING SMOKE TESTS PASS")
