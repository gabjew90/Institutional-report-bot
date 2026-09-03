"""Pulses and the calendar: daily_reports, pulse_leans, calendar posts, market-cap and logo caches, reminders.

Moved verbatim from db.py on 2026-09-01. Every reference to a db.py
function goes through `_db.<name>` so the facade stays the single
patch point and the thread-local connection model lives in db.py.
"""
from datetime import datetime, date, timedelta
import logging
import re
import sqlite3

import db as _db  # noqa: E402

log = logging.getLogger("db")


def insert_daily_report(
    report_date: str,
    report_type: str,
    report_json: str,
    report_markdown: str,
    pdf_count: int,
    input_tokens: int,
    output_tokens: int,
) -> int:
    conn = _db.get_connection()
    # Explicit ISO 'T'-format created_at so it matches dropbox_modified_at
    # format for lexicographic comparison in later queries.
    created_at = datetime.utcnow().isoformat()
    cur = conn.execute(
        """INSERT INTO daily_reports
           (report_date, report_type, report_json, report_markdown, pdf_count,
            input_tokens_used, output_tokens_used, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (report_date, report_type, report_json, report_markdown, pdf_count,
         input_tokens, output_tokens, created_at),
    )
    conn.commit()
    return cur.lastrowid


def find_sent_report_by_pending_file(pending_file: str) -> int | None:
    """Return the id of a daily_reports row already SENT to Discord for
    this bridge pending filename, or None.

    Idempotency key for the bridge success path: the payload carries
    `"pending_file": "<name>"`, and a row with discord_sent_at set means
    Discord already received this pulse — a surviving pending file is a
    failed GitHub cleanup, not an undelivered pulse (2026-08-04 review).
    Unsent rows deliberately do not match, so a failed post still
    retries.
    """
    conn = _db.get_connection()
    row = conn.execute(
        """SELECT id FROM daily_reports
           WHERE discord_sent_at IS NOT NULL
             AND report_json LIKE ?
           ORDER BY id DESC LIMIT 1""",
        (f'%"pending_file": "{pending_file}"%',),
    ).fetchone()
    return row[0] if row else None


def mark_report_sent(report_id: int) -> None:
    conn = _db.get_connection()
    conn.execute(
        "UPDATE daily_reports SET discord_sent_at = datetime('now') WHERE id = ?",
        (report_id,),
    )
    conn.commit()


def daily_pulse_delivered_on(report_date: str) -> bool:
    """Did a SCHEDULED pulse actually land on `report_date` (local date)?

    The bridge writes a daily_reports row with report_type='daily' when it
    posts the routine's pulse to Discord, so a missing row means no pulse
    reached anyone. Manual /pulse writes report_type='manual' and does not
    count — it does not satisfy the scheduled cadence.

    Used by the missing-pulse watchdog. 2026-08-11: the routine's model ran
    out of credits mid-run, wrote one progress stamp and stopped. No pulse,
    no error, no marker. The holiday path self-documents because the agent
    is alive to commit a skip marker; an agent that dies cannot report its
    own death, so the check has to live outside it.
    """
    row = _db.get_connection().execute(
        """SELECT 1 FROM daily_reports
           WHERE report_date = ? AND report_type = 'daily' LIMIT 1""",
        (report_date,),
    ).fetchone()
    return row is not None


def get_last_report_time() -> str | None:
    """Get the created_at timestamp of the most recent daily report, any date."""
    row = _db.get_connection().execute(
        """SELECT created_at FROM daily_reports
           WHERE report_type = 'daily'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    return row["created_at"] if row else None


def get_last_daily_pulse() -> dict | None:
    """Get the full last scheduled pulse row (for cross-day context)."""
    row = _db.get_connection().execute(
        """SELECT report_date, created_at, report_markdown, pdf_count
           FROM daily_reports
           WHERE report_type = 'daily'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def get_recent_daily_pulse_titles(limit: int = 5) -> list[str]:
    """Return the title (first-line H1) of the last `limit` scheduled
    pulses, newest first. Used by the synthesizer to inject a "DO NOT
    REPEAT" title-novelty block — without this, the model tends to
    recycle catchphrases like 'AI cracks' across multiple consecutive
    days (observed 06-04 / 06-05 / 06-08 all used 'AI cracks' as the
    second clause of the title).

    Empty list when no pulses found or extraction fails.
    """
    import re as _re
    rows = _db.get_connection().execute(
        """SELECT report_markdown FROM daily_reports
           WHERE report_type = 'daily'
           ORDER BY created_at DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    titles: list[str] = []
    for r in rows:
        md = (r["report_markdown"] or "").lstrip()
        # Strip frontmatter if present
        if md.startswith("---"):
            end = md.find("\n---", 3)
            if end != -1:
                md = md[end + 4:].lstrip()
        m = _re.match(r"#\s+(.+)", md)
        if m:
            titles.append(m.group(1).strip())
    return titles


def save_pulse_state_candidate(state_json: str, dumped_at: str) -> None:
    """Insert a context-dump state snapshot. Keeps only the last 50
    candidates (the dump job fires ~hourly; unstamped rows older than
    the working set are noise)."""
    conn = _db.get_connection()
    conn.execute(
        "INSERT INTO pulse_state (dumped_at, state_json) VALUES (?, ?)",
        (dumped_at, state_json),
    )
    conn.execute(
        """DELETE FROM pulse_state WHERE pulse_date IS NULL AND id NOT IN (
               SELECT id FROM pulse_state WHERE pulse_date IS NULL
               ORDER BY dumped_at DESC LIMIT 50)"""
    )
    conn.commit()


def stamp_pulse_state_for_date(pulse_date: str) -> dict | None:
    """Mark the most recent UNSTAMPED candidate as consumed by the daily
    pulse for `pulse_date`, and return it. Idempotent: if a row is
    already stamped for this date (bridge retry after partial delivery),
    return that row without stamping another."""
    conn = _db.get_connection()
    existing = conn.execute(
        "SELECT id, dumped_at, state_json FROM pulse_state "
        "WHERE pulse_date = ? ORDER BY id DESC LIMIT 1",
        (pulse_date,),
    ).fetchone()
    if existing:
        return dict(existing)
    row = conn.execute(
        "SELECT id, dumped_at, state_json FROM pulse_state "
        "WHERE pulse_date IS NULL ORDER BY dumped_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE pulse_state SET pulse_date = ? WHERE id = ?",
        (pulse_date, row["id"]),
    )
    conn.commit()
    return dict(row)


def get_prev_stamped_pulse_state(before_date: str) -> dict | None:
    """The previous daily pulse's stamped state — the WHAT CHANGED
    diff baseline."""
    row = _db.get_connection().execute(
        "SELECT id, dumped_at, pulse_date, state_json FROM pulse_state "
        "WHERE pulse_date IS NOT NULL AND pulse_date < ? "
        "ORDER BY pulse_date DESC, id DESC LIMIT 1",
        (before_date,),
    ).fetchone()
    return dict(row) if row else None


def upsert_pulse_leans(today: str, leans: list[dict]) -> list[dict]:
    """Record today's extracted leans. A lean matching a LIVE row
    (same instrument+direction) refreshes last_seen_date; otherwise a
    new row is inserted with first_seen = today. Idempotent for bridge
    retries on the same day.

    Stance-flip handling (2026-06-15): when a NEW lean's instrument has
    a LIVE row in the OPPOSITE direction, that opposite row is marked
    'superseded' (leaves the board) so the board can't show a
    contradictory long+short on the same instrument — the 06-15 pulse
    carried $USO d5 LONG and NEW SHORT simultaneously. Returns the list
    of flips performed this call: [{instrument, from, to}] — the bridge
    feeds these to WHAT CHANGED ('Flipped: $USO long -> short') and the
    board (FLIP marker). Empty on a bridge retry (the opposite row is
    already superseded, so no second flip fires)."""
    conn = _db.get_connection()
    flips: list[dict] = []
    for lean in leans:
        inst = (lean.get("instrument") or "").upper().strip()
        direction = (lean.get("direction") or "").lower().strip()
        if not inst or direction not in ("long", "short"):
            continue
        live = conn.execute(
            "SELECT id FROM pulse_leans WHERE instrument = ? AND "
            "direction = ? AND status = 'live' ORDER BY id DESC LIMIT 1",
            (inst, direction),
        ).fetchone()
        if live:
            # Lineage integrity (2026-08-04 review): a re-affirmation
            # whose instrument set CHANGED must say so — the 08-04 board
            # rendered "held since Jul 29: Long $MSFT, $GOOGL" when the
            # Jul 29 lean was MSFT-only and $GOOGL had never been on any
            # board. first_seen is kept (the thesis lineage is real for
            # the primary) but the added/removed tickers are visible.
            new_ctx = lean.get("context") or None
            if new_ctx:
                old_ctx = conn.execute(
                    "SELECT context_snippet FROM pulse_leans WHERE id = ?",
                    (live["id"],),
                ).fetchone()[0] or ""
                _tag_re = re.compile(r"\$[A-Za-z][A-Za-z0-9.]{0,6}")
                old_set = {t.upper() for t in _tag_re.findall(
                    old_ctx.split("· instruments updated", 1)[0])}
                new_set = {t.upper() for t in _tag_re.findall(new_ctx)}
                if old_set and new_set and old_set != new_set:
                    new_ctx = (
                        f"{new_ctx[:120]} · instruments updated today "
                        f"(was: {', '.join(sorted(old_set))})"
                    )
                new_ctx = new_ctx[:200]
            # Preserve the PRIOR last_seen before overwriting it. Without
            # this the board cannot tell a continuous hold from a
            # re-entry: a lean dropped and re-added still carries its
            # original first_seen_date, and last_seen_date is stamped to
            # today, so the gap leaves no trace. 2026-08-12 shipped
            # "held since Aug 7" for $MU (absent Aug 10 and Aug 11) and
            # "held since Aug 10" for $QQQ puts (absent Aug 11, and
            # scored "flat (-0.3%)" on its way off the board the day
            # before). A reader sees an unbroken call; the board actually
            # dropped it and picked it back up.
            conn.execute(
                "UPDATE pulse_leans SET prev_seen_date = last_seen_date, "
                "last_seen_date = ?, "
                "context_snippet = COALESCE(?, context_snippet) WHERE id = ?",
                (today, new_ctx, live["id"]),
            )
        else:
            opposite = "short" if direction == "long" else "long"
            live_opp = conn.execute(
                "SELECT id FROM pulse_leans WHERE instrument = ? AND "
                "direction = ? AND status = 'live' ORDER BY id DESC LIMIT 1",
                (inst, opposite),
            ).fetchone()
            if live_opp:
                conn.execute(
                    "UPDATE pulse_leans SET status = 'superseded' WHERE id = ?",
                    (live_opp["id"],),
                )
                flips.append({"instrument": inst, "from": opposite, "to": direction})
            conn.execute(
                "INSERT OR IGNORE INTO pulse_leans "
                "(instrument, direction, first_seen_date, last_seen_date, "
                " context_snippet, status) VALUES (?, ?, ?, ?, ?, 'live')",
                (inst, direction, today, today,
                 (lean.get("context") or "")[:160]),
            )
    conn.commit()
    return flips


def get_prev_scheduled_pulse_date(today: str) -> str | None:
    """Date of the most recent SCHEDULED pulse before `today` — the
    previous TRADE BOARD's date. Powers the board's DROPPED lines: a
    lean last seen exactly on this date and absent from today's board
    was abandoned today (2026-07-10: four of five leans vanished
    silently). Manual pulses don't count — they don't move the board."""
    try:
        row = _db.get_connection().execute(
            "SELECT MAX(report_date) FROM daily_reports "
            "WHERE report_type = 'daily' AND report_date < ?",
            (today,),
        ).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def count_recent_reversals(
    instrument: str, today: str, window_days: int = 10
) -> int:
    """Direction changes for one instrument's lean history inside a
    rolling window — the board's churn signal.

    2026-08-04 review: the SMH/SOXX complex flipped direction five times
    in seven sessions and each FLIP rendered as if it were the first.
    Walks the instrument's rows (any status — superseded rows ARE the
    flip history) in first_seen order and counts adjacent direction
    changes."""
    conn = _db.get_connection()
    rows = conn.execute(
        "SELECT direction FROM pulse_leans "
        "WHERE instrument = ? AND first_seen_date >= date(?, ?) "
        "ORDER BY first_seen_date, id",
        (instrument.upper().strip(), today, f"-{int(window_days)} day"),
    ).fetchall()
    dirs = [r[0] for r in rows if r[0]]
    return sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)


def get_board_leans(today: str, max_age_days: int = 5) -> list[dict]:
    """Live leans for the TRADE BOARD, newest-first. Ages out leans not
    re-affirmed within max_age_days as a side effect (keeps the board
    honest without a separate cron)."""
    conn = _db.get_connection()
    conn.execute(
        "UPDATE pulse_leans SET status = 'aged_out' "
        "WHERE status = 'live' AND last_seen_date < date(?, ?)",
        (today, f"-{int(max_age_days)} day"),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT instrument, direction, first_seen_date, last_seen_date, "
        "prev_seen_date, context_snippet FROM pulse_leans "
        "WHERE status = 'live' "
        "ORDER BY first_seen_date DESC, instrument",
    ).fetchall()
    return [dict(r) for r in rows]


def reminder_already_sent(fire_date: str, event_id: str, lead: int) -> bool:
    """True if this (fire_date, event_id, lead) reminder already posted."""
    row = _db.get_connection().execute(
        "SELECT 1 FROM reminder_sent WHERE fire_date = ? AND event_id = ? "
        "AND lead = ?",
        (fire_date, event_id, int(lead)),
    ).fetchone()
    return row is not None


def mark_reminder_sent(fire_date: str, event_id: str, lead: int) -> None:
    """Record that a reminder posted, so a redeploy can't double-post.
    Only called AFTER a successful Discord send."""
    from datetime import datetime
    conn = _db.get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO reminder_sent (fire_date, event_id, lead, "
        "sent_at) VALUES (?, ?, ?, ?)",
        (fire_date, event_id, int(lead), datetime.utcnow().isoformat()),
    )
    conn.commit()


def get_symbol_logos(symbols: list[str], max_age_days: int = 30,
                     miss_max_age_days: int = 7) -> dict:
    """symbol -> PNG bytes (b'' = known to have no logo), fresh rows only.

    Same two-TTL split as get_market_caps, for the same reason: a
    successful logo is stable for a month, but a MISS must expire sooner
    or one bad fetch hides a logo until the long TTL lapses. A miss is
    cheap to recheck; a wrong one is visible on every sheet.
    """
    if not symbols:
        return {}
    out: dict = {}
    for i in range(0, len(symbols), 400):
        chunk = symbols[i:i + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = _db.get_connection().execute(
            f"SELECT symbol, image FROM symbol_logo "
            f"WHERE symbol IN ({placeholders}) AND ("
            f"  (LENGTH(COALESCE(image, X'')) > 0 "
            f"   AND fetched_at >= datetime('now', ?)) "
            f"  OR "
            f"  (LENGTH(COALESCE(image, X'')) = 0 "
            f"   AND fetched_at >= datetime('now', ?)) "
            f")",
            [*chunk, f"-{int(max_age_days)} days",
             f"-{int(miss_max_age_days)} days"],
        ).fetchall()
        for sym, img in rows:
            out[sym] = bytes(img) if img else b""
    return out


def upsert_symbol_logos(rows: list[tuple]) -> int:
    """rows: (symbol, png_bytes). Pass b'' for 'Finnhub has no logo' —
    that is a cached answer, not a failure to record."""
    if not rows:
        return 0
    conn = _db.get_connection()
    conn.executemany(
        "INSERT OR REPLACE INTO symbol_logo (symbol, image, fetched_at) "
        "VALUES (?, ?, datetime('now'))",
        [(s, sqlite3.Binary(b or b"")) for s, b in rows],
    )
    conn.commit()
    return len(rows)


def get_market_caps(symbols: list[str], max_age_days: int = 7,
                    fail_max_age_days: int = 1) -> dict:
    """symbol -> {'cap': float_musd, 'name': str} for fresh cache rows.

    TWO TTLs, deliberately. A successful lookup is good for
    `max_age_days` (7) measured in elapsed time. A FAILED one — stored
    as cap 0 so it still gets cached — expires by CALENDAR DAY: it is
    served only if it was fetched within `fail_max_age_days` (1) date
    boundaries, so the next nightly run always retries it.

    The failure branch compares dates rather than elapsed hours on
    purpose. The refresh job runs on a ~24h cadence, so an elapsed-time
    cutoff of exactly 1 day puts every real retry precisely on the
    boundary, where `>=` keeps the stale failure and the retry silently
    does not happen. A date comparison has no such edge.

    Before this split, a single transient Finnhub failure cached a
    mega-cap at 0 for the full 7 days. The calendar ranks by cap, so
    that name sorted below micro-caps and fell off the sheet for a
    week on one bad request. The failure row still earns a TTL (it
    stops a hot retry loop within the day); it just must not earn the
    success TTL.
    """
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    out: dict = {}
    conn = _db.get_connection()
    for i in range(0, len(symbols), 500):
        chunk = symbols[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT symbol, market_cap_musd, name FROM symbol_market_cap "
            f"WHERE symbol IN ({placeholders}) AND ("
            f"  (COALESCE(market_cap_musd, 0) > 0 "
            f"   AND fetched_at >= datetime('now', ?)) "
            f"  OR "
            f"  (COALESCE(market_cap_musd, 0) <= 0 "
            f"   AND date(fetched_at) > date('now', ?)) "
            f")",
            [*chunk, f"-{int(max_age_days)} days",
             f"-{int(fail_max_age_days)} days"],
        ).fetchall()
        for r in rows:
            out[r["symbol"]] = {
                "cap": r["market_cap_musd"] or 0.0,
                "name": r["name"] or r["symbol"],
            }
    return out


def upsert_market_caps(rows: list[tuple]) -> int:
    """rows: (symbol, market_cap_musd, name). Upsert with a fresh
    fetched_at. A failed profile lookup is stored as (sym, 0, sym) so it
    still gets a TTL instead of re-fetching in a hot loop — but
    get_market_caps expires those after ONE day, not seven, so a
    transient failure cannot bench a real name for a week."""
    if not rows:
        return 0
    conn = _db.get_connection()
    conn.executemany(
        "INSERT OR REPLACE INTO symbol_market_cap "
        "(symbol, market_cap_musd, name, fetched_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        rows,
    )
    conn.commit()
    return len(rows)


def record_calendar_posts(date_iso: str, posts: list[tuple[int, int]],
                          lineup_hash: str, lineup_json: str | None = None) -> None:
    """posts: [(channel_id, message_id), ...] for one sheet. lineup_json
    is the posted rows (symbols, sessions, moves) so a refresh can keep
    a 3 PM price when the morning rebuild cannot price the name."""
    conn = _db.get_connection()
    conn.executemany(
        "INSERT OR REPLACE INTO calendar_posts "
        "(date_iso, channel_id, message_id, lineup_hash, lineup_json) VALUES (?, ?, ?, ?, ?)",
        [(date_iso, int(c), int(m), lineup_hash, lineup_json) for c, m in posts])
    conn.commit()


def get_calendar_posts(date_iso: str) -> list[dict]:
    rows = _db.get_connection().execute(
        "SELECT channel_id, message_id, lineup_hash, refreshed_at, lineup_json "
        "FROM calendar_posts WHERE date_iso = ?", (date_iso,)).fetchall()
    return [dict(r) for r in rows]


def mark_calendar_refreshed(date_iso: str, lineup_hash: str,
                            lineup_json: str | None = None) -> None:
    conn = _db.get_connection()
    conn.execute(
        "UPDATE calendar_posts SET lineup_hash = ?, "
        "lineup_json = COALESCE(?, lineup_json), "
        "refreshed_at = datetime('now') WHERE date_iso = ?",
        (lineup_hash, lineup_json, date_iso))
    conn.commit()


def calendar_already_posted(date_iso: str) -> bool:
    """Idempotency check for the daily calendar graphic — True when a
    calendar_posted pipeline event exists for this date (spec §6: no
    re-post on reboot)."""
    if _db.get_connection().execute(
            "SELECT 1 FROM calendar_posts WHERE date_iso = ? LIMIT 1",
            (date_iso,)).fetchone():
        return True
    row = _db.get_connection().execute(
        "SELECT 1 FROM pipeline_events WHERE event_type = 'calendar_posted' "
        "AND payload LIKE ? LIMIT 1",
        (f'%{date_iso}%',),
    ).fetchone()
    return row is not None


def get_last_daily_pulses(limit: int = 3) -> list[dict]:
    """The last `limit` scheduled pulses, newest first — report_date +
    report_markdown. Used by the consensus ledger (2026-08-21): a
    consensus is often published 2-3 pulses before the print (WMT's
    $0.75/$188.79bn ran 8/18, the print was 8/20), so a single-pulse
    lookback missed it."""
    rows = _db.get_connection().execute(
        """SELECT report_date, created_at, report_markdown
           FROM daily_reports
           WHERE report_type = 'daily'
           ORDER BY created_at DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]
