"""The analyst log: analyst_trades, bot book posts, win/loss summaries.

Moved verbatim from db.py on 2026-09-01. Every reference to a db.py
function goes through `_db.<name>` so the facade stays the single
patch point and the thread-local connection model lives in db.py.
"""
from datetime import datetime, date, timedelta
import json
import logging

import db as _db  # noqa: E402

log = logging.getLogger("db")


def analyst_trade_exists(discord_message_id: int, discord_attachment_id: int) -> bool:
    """Dedup check before OCR'ing an image we've already processed."""
    row = _db.get_connection().execute(
        "SELECT 1 FROM analyst_trades WHERE discord_message_id = ? "
        "AND discord_attachment_id = ?",
        (int(discord_message_id), int(discord_attachment_id)),
    ).fetchone()
    return row is not None


def _find_inheritable_entry(
    conn,
    *,
    ticker: str,
    contract_type: str | None,
    posted_at: str,
    tracking_mode: str,
    caller: str | None,
    author_id: int | None,
) -> dict | None:
    """The scope's most recent open/add on `ticker` (45d back from
    `posted_at`) whose position has NO close event yet — the entry an
    orphan (strikeless) close should inherit contract fields from.

    Scope rules mirror the stage-1 fill: caller closes match only the
    same caller's opens; member closes only the same author_id's. The
    NOT EXISTS guard prevents one entry's fields being handed to two
    different exits (the second orphan close on a ticker whose position
    already closed stays orphan and gets the close_without_open tag)."""
    scope_o = " AND o.tracking_mode = ?"
    scope_c = " AND c.tracking_mode = ?"
    sp: tuple = (tracking_mode,)
    if tracking_mode == "caller" and caller:
        scope_o += " AND LOWER(COALESCE(o.caller, '')) = ?"
        scope_c += " AND LOWER(COALESCE(c.caller, '')) = ?"
        sp = sp + ((caller or "").strip().lower(),)
    elif tracking_mode == "member" and author_id is not None:
        scope_o += " AND o.author_id = ?"
        scope_c += " AND c.author_id = ?"
        sp = sp + (int(author_id),)
    row = conn.execute(
        f"""SELECT o.contract_type, o.strike, o.expiry
           FROM analyst_trades o
           WHERE o.is_trade = 1
             AND o.ticker = ?
             AND o.action IN ('open', 'add')
             AND (? IS NULL OR COALESCE(o.contract_type, '') = COALESCE(?, ''))
             AND o.posted_at < ?
             AND o.posted_at > datetime(?, '-45 days')
             {scope_o}
             AND NOT EXISTS (
                 SELECT 1 FROM analyst_trades c
                 WHERE c.is_trade = 1
                   AND c.action = 'close'
                   AND c.ticker = o.ticker
                   AND COALESCE(c.contract_type, '') = COALESCE(o.contract_type, '')
                   AND COALESCE(c.strike, -1) = COALESCE(o.strike, -1)
                   AND COALESCE(c.expiry, '') = COALESCE(o.expiry, '')
                   AND c.posted_at > o.posted_at
                   {scope_c}
             )
           ORDER BY o.posted_at DESC
           LIMIT 1""",
        (ticker, contract_type, contract_type, posted_at, posted_at)
        + sp + sp,
    ).fetchone()
    return dict(row) if row else None


def backfill_orphan_exit_links(days: int = 120) -> int:
    """One-shot repair for HISTORICAL orphan closes: close rows with a
    ticker but no strike (P&L cards show ticker + gain% only) get the
    same contract-field inheritance record_analyst_trade now applies at
    write time. Clears a stale close_without_open tag when the
    inherited fields complete the position match. Returns rows updated.
    Idempotent: a filled row has a strike and is never reprocessed."""
    conn = _db.get_connection()
    orphans = conn.execute(
        """SELECT id, ticker, contract_type, posted_at, tracking_mode,
                  caller, author_id
           FROM analyst_trades
           WHERE is_trade = 1 AND action = 'close'
             AND ticker IS NOT NULL AND strike IS NULL
             AND posted_at > datetime('now', ?)""",
        (f"-{int(days)} days",),
    ).fetchall()
    fixed = 0
    for o in orphans:
        tm = (o["tracking_mode"] or "caller").strip().lower()
        cand = _db._find_inheritable_entry(
            conn,
            ticker=o["ticker"],
            contract_type=o["contract_type"],
            posted_at=o["posted_at"],
            tracking_mode=tm if tm in ("caller", "member") else "caller",
            caller=o["caller"],
            author_id=o["author_id"],
        )
        if not cand:
            continue
        conn.execute(
            """UPDATE analyst_trades
               SET contract_type = COALESCE(contract_type, ?),
                   strike = ?,
                   expiry = COALESCE(expiry, ?),
                   inferred_status = CASE
                       WHEN inferred_status = 'close_without_open'
                       THEN NULL ELSE inferred_status END
               WHERE id = ?""",
            (cand["contract_type"], cand["strike"], cand["expiry"],
             o["id"]),
        )
        fixed += 1
    conn.commit()
    return fixed


def record_analyst_trade(
    *,
    discord_message_id: int,
    discord_attachment_id: int,
    author: str,
    posted_at: str,
    image_url: str | None,
    caption: str | None,
    is_trade: bool,
    gemini_json: dict | None,
    ticker: str | None = None,
    contract_type: str | None = None,
    strike: float | None = None,
    expiry: str | None = None,
    action: str | None = None,
    gain_pct: float | None = None,
    price: float | None = None,
    caller: str | None = None,
    author_id: int | None = None,
    tracking_mode: str = "caller",
    replace: bool = False,
) -> None:
    """Insert an analyst-trade row.

    Default: INSERT OR IGNORE on the (message_id, attachment_id) unique
    constraint — used by the live watcher and standard backfill, so
    re-processing the same image is a no-op.

    `replace=True`: INSERT OR REPLACE — used by the --force backfill
    when we want to update existing rows with re-extracted data (e.g.
    after changing the OCR prompt). The replace path drops the existing
    row and re-inserts, but is_trade rows that existed before stay
    in place if their message/attachment IDs match (UNIQUE constraint).
    """
    import json as _json
    conn = _db.get_connection()

    # Expiry-fill from prior open: when a close screenshot doesn't show
    # the expiry (Robinhood close-confirmation cards sometimes show only
    # ticker + gain%, with Gemini noting "expiry not visible"), look for
    # the most-recent open/add with same (ticker, contract_type, strike)
    # in the last 14 days and copy its expiry into this close row.
    # Without this, the 4-tuple match against the open fails — the close
    # looks orphan and the open stays "live" forever even though Abe
    # explicitly closed it.
    # Tracking scope for expiry-fill / close-without-open lookups: a caller
    # close should only match against the same caller's opens; a member
    # close should only match against the same author_id's opens. This
    # prevents BK's close from accidentally inheriting another user's
    # expiry, and prevents members from polluting caller match lookups.
    norm_tm = (tracking_mode or "caller").strip().lower()
    if norm_tm not in ("caller", "member"):
        norm_tm = "caller"
    scope_clause = " AND tracking_mode = ?"
    scope_params: tuple = (norm_tm,)
    if norm_tm == "caller" and caller:
        scope_clause += " AND LOWER(COALESCE(caller, '')) = ?"
        scope_params = scope_params + ((caller or "").strip().lower(),)
    elif norm_tm == "member" and author_id is not None:
        scope_clause += " AND author_id = ?"
        scope_params = scope_params + (int(author_id),)

    if is_trade and action == "close" and expiry is None and ticker:
        inferred_expiry = conn.execute(
            f"""SELECT expiry FROM analyst_trades
               WHERE is_trade = 1
                 AND ticker = ?
                 AND COALESCE(contract_type, '') = COALESCE(?, '')
                 AND COALESCE(strike, -1) = COALESCE(?, -1)
                 AND expiry IS NOT NULL
                 AND action IN ('open', 'add')
                 AND posted_at < ?
                 AND posted_at > datetime(?, '-14 days')
                 {scope_clause}
               ORDER BY posted_at DESC
               LIMIT 1""",
            (ticker, contract_type, strike, posted_at, posted_at) + scope_params,
        ).fetchone()
        if inferred_expiry:
            expiry = inferred_expiry[0]

    # Stage-2 inheritance (2026-07-11 review). P&L close cards commonly
    # show ONLY ticker + gain% — no strike, no contract type, no expiry.
    # The stage-1 fill above requires strike + contract_type to match,
    # so a strikeless close stayed orphan: it partitioned separately in
    # the position rollup, the open stayed "live" forever ("no exit
    # posted"), and outcome coverage starved (40 of 447 member rows had
    # outcomes). When the close is missing its STRIKE, inherit every
    # missing contract field from the scope's most recent open/add on
    # the same ticker (45d) whose position has no close yet. Extracted
    # values are never overwritten; a close that names a contract_type
    # only matches entries of that type.
    if is_trade and action == "close" and ticker and strike is None:
        _cand = _db._find_inheritable_entry(
            conn,
            ticker=ticker,
            contract_type=contract_type,
            posted_at=posted_at,
            tracking_mode=norm_tm,
            caller=caller,
            author_id=author_id,
        )
        if _cand:
            contract_type = contract_type or _cand["contract_type"]
            strike = _cand["strike"]
            expiry = expiry or _cand["expiry"]

    # Close-without-open detection: when logging a `close` row with no
    # prior `open` or `add` for the same contract in the last 30 days,
    # tag the row so the bot knows the entry isn't in the log. Catches
    # OCR-missed opens, opens older than the context window, and trades
    # that pre-date the watcher's deployment. Bot's voice rule uses the
    # tag to phrase carefully ("he flagged the exit — entry isn't in the
    # log") instead of fabricating an entry. Runs AFTER expiry-fill so
    # an inferred expiry can rescue the 4-tuple match.
    inferred_status: str | None = None
    if is_trade and action == "close" and ticker and expiry:
        prior_open = conn.execute(
            f"""SELECT COUNT(*) FROM analyst_trades
               WHERE is_trade = 1
                 AND ticker = ?
                 AND COALESCE(contract_type, '') = COALESCE(?, '')
                 AND COALESCE(strike, -1) = COALESCE(?, -1)
                 AND expiry = ?
                 AND action IN ('open', 'add')
                 AND posted_at > datetime(?, '-30 days')
                 {scope_clause}""",
            (ticker, contract_type, strike, expiry, posted_at) + scope_params,
        ).fetchone()
        if prior_open[0] == 0:
            inferred_status = "close_without_open"

    # Normalize tracking_mode — only 'caller' / 'member' accepted, default
    # to 'caller' on anything else for backwards-compat with the legacy
    # single-caller call sites.
    tm = (tracking_mode or "").strip().lower()
    if tm not in ("caller", "member"):
        tm = "caller"
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    conn.execute(
        f"""{verb} INTO analyst_trades
           (discord_message_id, discord_attachment_id, author, author_id,
            posted_at, image_url, caption, is_trade, ticker, contract_type,
            strike, expiry, action, gain_pct, price, caller, gemini_json,
            inferred_status, tracking_mode, extraction_source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?)""",
        (
            int(discord_message_id),
            int(discord_attachment_id),
            author,
            int(author_id) if author_id is not None else None,
            posted_at,
            image_url,
            caption,
            1 if is_trade else 0,
            ticker,
            contract_type,
            strike,
            expiry,
            action,
            gain_pct,
            price,
            (caller or "").strip().lower() or None,
            _json.dumps(gemini_json) if gemini_json is not None else None,
            inferred_status,
            tm,
            # Tag the modality here rather than leaving NULL for a boot
            # migration to guess at — that guess is what mislabelled 31K
            # rows as screenshots (2026-07-30). Same rule the migration
            # uses: a url or an attachment id means image evidence.
            "image" if (image_url or discord_attachment_id) else "text",
        ),
    )
    conn.commit()


def insert_text_extracted_trade_if_not_dup(
    *,
    author_id: int,
    author_username: str,
    discord_message_id: int,
    posted_at: str,
    extracted: dict,
    channel_name: str | None = None,
    dedup_window_minutes: int = 5,
) -> bool:
    """Insert a classifier-extracted analyst_trades row with two-tier
    dedup.

      Tier 1 (strict): if any analyst_trades row already exists with
        the SAME discord_message_id, skip. One Discord message → at
        most one row, regardless of modality (text vs image vs mixed).
        This catches the live case where ocr_attachments_inline already
        wrote an image row for a message with screenshot + caption AND
        the new text+vision classifier tries to write a second row for
        the same message.

      Tier 2 (fuzzy): if a row exists with extraction_source='image'
        within ±dedup_window_minutes for the same (author_id, ticker,
        expiry, strike, contract_type, action), skip. Handles the
        cross-message case (text post then screenshot of the same
        trade 2 min later in a separate message).

    Returns True if inserted, False if skipped.

    Image extraction always wins on conflict: images are higher-
    fidelity verified screenshots.
    """
    conn = _db.get_connection()
    ticker = (extracted.get("ticker") or "").upper()
    contract_type = (extracted.get("contract_type") or "").lower() or None
    strike = extracted.get("strike")
    expiry = extracted.get("expiry") or None
    action = (extracted.get("action") or "").lower() or None

    # Tier 1: discord_message_id dedup. One Discord message → at most
    # one row regardless of modality.
    existing_by_msg_id = conn.execute(
        "SELECT id, extraction_source FROM analyst_trades "
        "WHERE discord_message_id = ? LIMIT 1",
        (int(discord_message_id),),
    ).fetchone()
    if existing_by_msg_id:
        return False

    # Parse posted_at for Tier 2 window bounds.
    try:
        ts = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        ts = datetime.utcnow()
    window_start = (ts - timedelta(minutes=dedup_window_minutes)).isoformat()
    window_end = (ts + timedelta(minutes=dedup_window_minutes)).isoformat()

    # Tier 2: same-fields against image rows within window.
    existing = conn.execute(
        """SELECT id FROM analyst_trades
            WHERE author_id = ?
              AND extraction_source = 'image'
              AND UPPER(ticker) = ?
              AND COALESCE(LOWER(contract_type), '') = COALESCE(?, '')
              AND COALESCE(strike, -1) = COALESCE(?, -1)
              AND COALESCE(expiry, '') = COALESCE(?, '')
              AND COALESCE(LOWER(action), '') = COALESCE(?, '')
              AND posted_at >= ?
              AND posted_at <= ?
            LIMIT 1""",
        (
            int(author_id), ticker, contract_type, strike, expiry, action,
            window_start, window_end,
        ),
    ).fetchone()
    if existing:
        return False  # cross-message dup of an image row — skip

    # No dup. Insert. discord_attachment_id is -1 for text rows (column
    # is NOT NULL; the sentinel is queryable separately if needed).
    conn.execute(
        """INSERT INTO analyst_trades
              (discord_message_id, discord_attachment_id, author,
               author_id, posted_at, ticker, contract_type, strike,
               expiry, action, gain_pct, price, is_trade,
               tracking_mode, extraction_source, gemini_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
        (
            int(discord_message_id), -1, author_username, int(author_id),
            posted_at, ticker, contract_type, strike, expiry, action,
            extracted.get("gain_pct"), extracted.get("price"),
            "member",  # text rows are always member-mode by definition
            extracted.get("extraction_source") or "text",
            json.dumps(extracted),
        ),
    )
    conn.commit()
    return True


def get_recent_analyst_trades(
    hours: int = 24,
    limit: int = 50,
    caller: str | None = None,
    tracking_mode: str | None = "caller",
) -> list[dict]:
    """Recent trade-tagged rows (is_trade=1) ordered newest first.

    `caller` filters by canonical caller (case-insensitive); None reads
    across all callers.

    `tracking_mode` defaults to 'caller' so the /ask RECENT TRADES
    context block stays clean — only official-caller rows surface there.
    Pass tracking_mode=None to read across both modes (e.g. for the
    member-points scoring future-helper or admin debugging). Pass
    tracking_mode='member' to read only member-posted alerts.
    """
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    # action filter: only the four real trade actions surface in /ask.
    # Excludes legacy 'viewing' / 'unclear' rows (the prompt no longer
    # emits these, but historical rows from before 2026-05-28 still
    # exist in production and would otherwise pollute RECENT TRADES
    # blocks — e.g. "viewing MSFT" from a 🍆 reaction emoji).
    where = [
        "is_trade = 1",
        "posted_at > ?",
        "LOWER(COALESCE(action,'')) IN ('open','add','trim','close')",
    ]
    params: list = [cutoff]
    if caller:
        where.append("LOWER(caller) = ?")
        params.append(caller.strip().lower())
    if tracking_mode is not None:
        where.append("tracking_mode = ?")
        params.append((tracking_mode or "").strip().lower() or "caller")
    rows = _db.get_connection().execute(
        f"""SELECT * FROM analyst_trades
           WHERE {' AND '.join(where)}
           ORDER BY posted_at DESC
           LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def record_bot_book_post(*, discord_message_id: int, channel_id: int,
                         caller: str, tickers: list[str]) -> None:
    """Remember that the bot published `caller`'s book as this message.

    Written right after the send, so a correction arriving seconds later
    has something to attach to.
    """
    import json as _json
    conn = _db.get_connection()
    # posted_at comes from the column DEFAULT datetime('now'), not from
    # Python. find_recent_book_posts compares against datetime('now',
    # ...), and SQLite's format uses a space separator while Python's
    # isoformat() uses T. TEXT comparison sorts T after space, so mixing
    # the two silently breaks every window query -- the same defect
    # documented for the pulse cutoffs.
    conn.execute(
        "INSERT OR REPLACE INTO bot_book_posts "
        "(discord_message_id, channel_id, caller, tickers) "
        "VALUES (?, ?, ?, ?)",
        (int(discord_message_id), int(channel_id),
         (caller or "").strip().lower(),
         _json.dumps(sorted({(t or "").strip().upper()
                             for t in tickers if (t or "").strip()}))),
    )
    conn.commit()


def find_recent_book_posts(*, channel_id: int,
                           within_minutes: int = 10) -> list[dict]:
    """Books published in this channel in the last `within_minutes`.

    Returns newest first: [{message_id, caller, tickers:[...]}]. The
    caller is NOT filtered here -- the handler matches it against the
    correcting author, and reading them all lets a caller correct a book
    someone else asked for, which is the normal case (Abe asked for
    Kyle's book; Kyle corrected it).
    """
    import json as _json
    rows = _db.get_connection().execute(
        "SELECT discord_message_id, caller, tickers FROM bot_book_posts "
        "WHERE channel_id = ? "
        "AND posted_at >= datetime('now', ?) "
        "ORDER BY posted_at DESC LIMIT 20",
        (int(channel_id), f"-{int(within_minutes)} minutes"),
    ).fetchall()
    out = []
    for r in rows:
        try:
            tickers = _json.loads(r[2]) or []
        except Exception:
            tickers = []
        out.append({"message_id": r[0], "caller": r[1], "tickers": tickers})
    return out


def get_bot_book_post(discord_message_id: int) -> dict | None:
    """One published book by message id, for the direct-reply path."""
    import json as _json
    r = _db.get_connection().execute(
        "SELECT discord_message_id, caller, tickers FROM bot_book_posts "
        "WHERE discord_message_id = ?", (int(discord_message_id),),
    ).fetchone()
    if not r:
        return None
    try:
        tickers = _json.loads(r[2]) or []
    except Exception:
        tickers = []
    return {"message_id": r[0], "caller": r[1], "tickers": tickers}


def prune_bot_book_posts(keep_days: int = 3) -> int:
    """Drop books older than `keep_days`. A correction lands in minutes,
    so this table has no reason to grow."""
    conn = _db.get_connection()
    cur = conn.execute(
        "DELETE FROM bot_book_posts WHERE posted_at < datetime('now', ?)",
        (f"-{int(keep_days)} days",))
    conn.commit()
    return cur.rowcount or 0


def get_current_analyst_positions(
    caller: str | None = None,
    tracking_mode: str | None = "caller",
) -> list[dict]:
    """Currently-open positions, computed as a state machine over the event
    chain: the LATEST action per (ticker, contract_type, strike, expiry)
    determines whether the position is still alive.

    - latest action in {open, add, trim} → position is live
    - latest action == 'close' → position is fully exited

    This replaces an earlier SUM-CASE +/-1 arithmetic model that incorrectly
    treated `trim` the same as `close` (so `open -> trim` netted to 0 and
    dropped the position from "currently open" even though Abe was still
    holding the remaining half). A trim reduces size but does NOT end the
    position; only an explicit `close` action does.

    `last_gain_pct` reflects the LATEST event's gain pill (not MAX across
    all events) so the bot reports the current state rather than a stale
    peak. Expired positions are excluded via the `expired_unknown`
    inferred_status set by the daily expire sweep.
    """
    # Caller filter is parametrized into both CTEs so positions stay
    # hard-separated per caller. None = all callers (legacy behavior).
    # tracking_mode defaults to 'caller' so member rows never bleed into
    # the /ask "currently open" block; pass None to read across both.
    caller_clause_ranked = ""
    caller_clause_entries = ""
    ranked_params: tuple = ()
    entries_params: tuple = ()
    if caller:
        caller_clause_ranked += " AND LOWER(caller) = ?"
        caller_clause_entries += " AND LOWER(caller) = ?"
        ranked_params = ranked_params + (caller.strip().lower(),)
        entries_params = entries_params + (caller.strip().lower(),)
    if tracking_mode is not None:
        tm = (tracking_mode or "").strip().lower() or "caller"
        caller_clause_ranked += " AND tracking_mode = ?"
        caller_clause_entries += " AND tracking_mode = ?"
        ranked_params = ranked_params + (tm,)
        entries_params = entries_params + (tm,)
    params = ranked_params + entries_params
    rows = _db.get_connection().execute(
        f"""WITH ranked AS (
            SELECT ticker, contract_type, strike, expiry,
                   action AS latest_action,
                   posted_at AS last_activity,
                   gain_pct AS last_gain_pct,
                   price AS last_price,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker, contract_type, strike, expiry
                       ORDER BY posted_at DESC
                   ) AS rn
            FROM analyst_trades
            WHERE is_trade = 1
              AND action IN ('open', 'add', 'close', 'trim')
              AND (inferred_status IS NULL OR inferred_status != 'expired_unknown')
              AND ticker IS NOT NULL
              AND expiry IS NOT NULL
              -- After 21:00 UTC (~5pm ET, post-cash-close), drop today's
              -- expiries from "currently open" — they're settled. The
              -- daily expire-sweep cron will mark them expired_unknown
              -- at 04:00 ET, but for the next ~11h after close they'd
              -- otherwise still show in the bot's "currently open" list.
              AND (
                  date(expiry) > date('now')
                  OR (date(expiry) = date('now')
                      AND CAST(strftime('%H', 'now') AS INTEGER) < 21)
              )
              {caller_clause_ranked}
        ),
        entries AS (
            -- The original open's price = entry price for the position.
            -- Picks the earliest open/add per contract so trims don't
            -- overwrite the entry valuation.
            SELECT ticker, contract_type, strike, expiry,
                   price AS entry_price,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker, contract_type, strike, expiry
                       ORDER BY posted_at ASC
                   ) AS rn
            FROM analyst_trades
            WHERE is_trade = 1
              AND action IN ('open', 'add')
              AND price IS NOT NULL
              AND ticker IS NOT NULL
              AND expiry IS NOT NULL
              {caller_clause_entries}
        )
        SELECT r.ticker, r.contract_type, r.strike, r.expiry,
               r.latest_action, r.last_activity, r.last_gain_pct,
               r.last_price, e.entry_price
        FROM ranked r
        LEFT JOIN entries e
          ON e.ticker = r.ticker
         AND COALESCE(e.contract_type,'') = COALESCE(r.contract_type,'')
         AND COALESCE(e.strike,-1) = COALESCE(r.strike,-1)
         AND e.expiry = r.expiry
         AND e.rn = 1
        WHERE r.rn = 1
          AND r.latest_action IN ('open', 'add', 'trim')
        ORDER BY date(r.expiry) ASC, r.last_activity DESC""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def compute_caller_win_loss_summary(
    days: int = 30,
    caller: str | None = None,
    tracking_mode: str | None = "caller",
) -> dict:
    """Compute a caller's W/L tally over the last N days under the rule:
    expirations-without-close count as losses.

    - Win  = action='close' AND gain_pct > 0
    - Loss = action='close' AND gain_pct < 0
             OR open/add row with inferred_status='expired_unknown'
             (he opened but never posted a close — silent expiry, treat as L)
    - Flat = action='close' AND gain_pct == 0 (rare)

    When `caller` is set, restricts to that caller's rows only — used by
    the per-caller /ask context blocks for hard separation. None = all
    callers (legacy behavior for single-caller deployments).

    Returns a dict with counts, win rate, avg win, avg loss + the win
    trades and silent-expiry trades.
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn = _db.get_connection()
    caller_clause = ""
    extra_params: tuple = ()
    if caller:
        caller_clause += " AND LOWER(caller) = ?"
        extra_params = extra_params + (caller.strip().lower(),)
    if tracking_mode is not None:
        caller_clause += " AND tracking_mode = ?"
        extra_params = extra_params + (
            (tracking_mode or "").strip().lower() or "caller",
        )

    closed_rows = conn.execute(
        f"""SELECT gain_pct FROM analyst_trades
           WHERE is_trade = 1
             AND action = 'close'
             AND posted_at > ?
             {caller_clause}""",
        (cutoff, *extra_params),
    ).fetchall()
    win_gains = [r["gain_pct"] for r in closed_rows if (r["gain_pct"] or 0) > 0]
    doc_loss_gains = [r["gain_pct"] for r in closed_rows if (r["gain_pct"] or 0) < 0]
    flat_count = sum(
        1 for r in closed_rows if (r["gain_pct"] is None or r["gain_pct"] == 0)
    )

    silent_loss_rows = conn.execute(
        f"""SELECT ticker, contract_type, strike, expiry, posted_at
           FROM analyst_trades
           WHERE is_trade = 1
             AND action IN ('open', 'add')
             AND inferred_status = 'expired_unknown'
             AND posted_at > ?
             {caller_clause}
           ORDER BY expiry DESC""",
        (cutoff, *extra_params),
    ).fetchall()
    silent_loss_count = len(silent_loss_rows)

    # Specific winning closes — so the bot doesn't have to recompute from
    # the trades list when asked for a breakdown.
    win_rows = conn.execute(
        f"""SELECT ticker, contract_type, strike, expiry, gain_pct, posted_at
           FROM analyst_trades
           WHERE is_trade = 1
             AND action = 'close'
             AND gain_pct > 0
             AND posted_at > ?
             {caller_clause}
           ORDER BY posted_at DESC""",
        (cutoff, *extra_params),
    ).fetchall()

    total_wins = len(win_gains)
    total_losses = len(doc_loss_gains) + silent_loss_count
    decided = total_wins + total_losses
    win_rate = (total_wins / decided * 100) if decided > 0 else 0.0

    return {
        "days": days,
        "wins": total_wins,
        "losses_documented": len(doc_loss_gains),
        "losses_silent_expiry": silent_loss_count,
        "total_losses": total_losses,
        "flat": flat_count,
        "decided": decided,
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(sum(win_gains) / total_wins, 1) if total_wins else None,
        "avg_loss_pct": (
            round(sum(doc_loss_gains) / len(doc_loss_gains), 1)
            if doc_loss_gains else None
        ),
        "win_trades": [dict(r) for r in win_rows],
        "silent_expiry_trades": [dict(r) for r in silent_loss_rows],
    }


def purge_old_expired_analyst_trades(days_after_expiry: int = 14) -> list[dict]:
    """Hard-delete trade rows whose expiry was more than `days_after_expiry`
    days ago AND have been marked expired_unknown. Called by the weekly
    cron. Returns the rows that were deleted so the cron can announce them.

    Two-stage cleanup keeps the DB bounded:
    1. Daily auto-expire marks past-expiry rows as 'expired_unknown'.
    2. This weekly purge deletes rows that have been expired AND past their
       retention window.

    Set days_after_expiry=0 to disable (returns immediately).
    """
    if days_after_expiry <= 0:
        return []
    conn = _db.get_connection()
    targets = conn.execute(
        f"""SELECT id, ticker, contract_type, strike, expiry, action,
                   gain_pct, posted_at
            FROM analyst_trades
            WHERE expiry IS NOT NULL
              AND inferred_status = 'expired_unknown'
              AND date(expiry, '+{int(days_after_expiry)} days') < date('now')"""
    ).fetchall()
    if not targets:
        return []
    ids = [r["id"] for r in targets]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM analyst_trades WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()
    return [dict(r) for r in targets]


def find_matching_open_expiry(
    *,
    caller: str | None,
    ticker: str,
    contract_type: str | None,
    strike: float | None,
    author_id: int | None = None,
    tracking_mode: str | None = "caller",
) -> str | None:
    """Find the expiry of a currently-open position matching this
    caller + ticker + contract_type + strike. Used by the watcher to
    resolve close/trim captions where the expiry wasn't stated —
    matches against the caller's actually-outstanding positions.

    Logic:
      - Computes the latest action per (ticker, contract_type, strike,
        expiry) for the caller
      - Filters to expiries whose latest action is open/add/trim (alive)
      - Excludes positions marked expired_unknown
      - Returns the MOST-RECENTLY-TOUCHED live expiry (changed
        2026-06-10 from soonest-expiry). When a caller runs two live
        positions in the same contract (e.g., TSLA 300c 5/29 AND 6/5),
        an unlabeled "closed it" caption almost always refers to the
        position they most recently opened/added/trimmed — recency of
        activity is the discussion thread, not calendar proximity to
        expiry. Soonest-expiry mismatched closes to the older leg,
        skewing W/L and leaving phantom opens.

    Returns None when no live position matches (caller closed it
    earlier, or opened it off-channel before the bot saw them).
    """
    if not ticker:
        return None
    params: list = [ticker.upper()]
    sql = """WITH ranked AS (
        SELECT expiry, action, posted_at,
               ROW_NUMBER() OVER (
                   PARTITION BY ticker, contract_type, strike, expiry
                   ORDER BY posted_at DESC
               ) AS rn
        FROM analyst_trades
        WHERE is_trade = 1
          AND UPPER(ticker) = ?
          AND action IN ('open', 'add', 'close', 'trim')
          AND expiry IS NOT NULL
          AND (inferred_status IS NULL OR inferred_status != 'expired_unknown')
    """
    if contract_type:
        sql += " AND LOWER(COALESCE(contract_type,'')) = ?"
        params.append(contract_type.strip().lower())
    if strike is not None:
        try:
            sql += " AND COALESCE(strike,-1) = ?"
            params.append(float(strike))
        except (TypeError, ValueError):
            return None
    if caller:
        sql += " AND LOWER(COALESCE(caller,'')) = ?"
        params.append(caller.strip().lower())
    if author_id is not None:
        sql += " AND author_id = ?"
        params.append(int(author_id))
    if tracking_mode is not None:
        sql += " AND tracking_mode = ?"
        params.append((tracking_mode or "").strip().lower() or "caller")
    sql += """)
        SELECT expiry FROM ranked
        WHERE rn = 1
          AND action IN ('open', 'add', 'trim')
        ORDER BY posted_at DESC
        LIMIT 1
    """
    try:
        row = _db.get_connection().execute(sql, tuple(params)).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def get_analyst_trade_by_message_id(message_id: int) -> dict | None:
    """Return the most-recent analyst_trades row for a discord message_id,
    or None if no row exists. Used by the watcher to look up a reply
    parent's extracted contract spec (ticker / strike / expiry) so that
    sparse follow-ups like 'closed' or 'sold @0.41' can be resolved
    against the parent's actual contract rather than defaulting to 0DTE.

    Returns the row as a plain dict with keys: id, caller, action,
    ticker, strike, contract_type, expiry, gain_pct, price, caption,
    is_trade.
    """
    row = _db.get_connection().execute(
        """SELECT id, caller, action, ticker, strike, contract_type,
                  expiry, gain_pct, price, caption, is_trade
           FROM analyst_trades
           WHERE discord_message_id = ?
           ORDER BY id DESC
           LIMIT 1""",
        (int(message_id),),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def get_latest_analyst_trade_posted_at(
    caller: str | None = None,
    tracking_mode: str | None = "caller",
) -> str | None:
    """Return the most-recent `posted_at` ISO timestamp for analyst_trades,
    optionally scoped to a single caller. Used by the on_ready /
    on_resumed catch-up loop to know where to resume scanning from
    when the bot reconnects after a gateway flap.

    tracking_mode defaults to 'caller' so the catchup loop only watches
    its own watermark; member-mode rows have their own catchup driven
    by chat_ingestion's last-seen pointer. Pass None to look across both.

    Returns None when there are no trades on file matching the filters.
    """
    where = ["1=1"]
    params: list = []
    if caller:
        where.append("LOWER(COALESCE(caller, '')) = ?")
        params.append(caller.strip().lower())
    if tracking_mode is not None:
        where.append("tracking_mode = ?")
        params.append((tracking_mode or "").strip().lower() or "caller")
    row = _db.get_connection().execute(
        f"""SELECT MAX(posted_at) AS latest
           FROM analyst_trades
           WHERE {' AND '.join(where)}""",
        tuple(params),
    ).fetchone()
    if row is None:
        return None
    val = row[0] if isinstance(row, tuple) else row["latest"]
    return val or None


def known_trade_caller_names() -> list[str]:
    """Names of every member with rows in the trade ledger — the
    protection set for the /ask outcome guard's third-person check
    (2026-07-09: 'Abe's plays are a speedrun to homelessness' shipped
    unsourced while the ledger showed him overwhelmingly green).

    2026-07-11: was DISTINCT caller only — which, combined with the
    caller-backfill bug, covered three names while 39 members had
    ledger rows. Now unions caller names with member-mode authors'
    usernames AND display names (resolved via chat_messages).

    2026-08-10 — this was NOT a cheap query, whatever the previous
    docstring claimed. Written as `analyst_trades JOIN chat_messages ON
    author_id`, SQLite inverted the join: `SCAN cm USING INDEX
    idx_chat_messages_username_ts` then a probe into analyst_trades per
    row. With 189,855 chat_messages and 49,320 analyst_trades that is a
    full scan of the message table plus a temp B-tree for DISTINCT, and
    it measured **172.9s in production**. The /ask caller caches for
    600s, so it ran on a cache miss — synchronously, on the Discord
    event loop, blocking the gateway heartbeat for ~3 minutes and
    stalling every interaction queued behind it. That is the "responses
    take 60+ seconds now" report.

    Driving from the small side instead (author_ids in the ledger, then
    an indexed probe into chat_messages) uses
    idx_chat_messages_author_ts and measures 0.146s — same 114 rows,
    1183x faster. Keep the subquery form; do not "simplify" it back
    into a JOIN."""
    try:
        conn = _db.get_connection()
        names: set[str] = set()
        for r in conn.execute(
            "SELECT DISTINCT caller FROM analyst_trades "
            "WHERE caller IS NOT NULL AND TRIM(caller) != ''"
        ).fetchall():
            names.add((r[0] or "").strip().lower())
        for r in conn.execute(
            "SELECT DISTINCT author_username, author_display "
            "FROM chat_messages WHERE author_id IN ("
            "  SELECT author_id FROM analyst_trades "
            "  WHERE author_id IS NOT NULL)"
        ).fetchall():
            for v in (r[0], r[1]):
                v = (v or "").strip().lower().lstrip(".")
                if len(v) >= 2:
                    names.add(v)
        return sorted(names)
    except Exception:
        return []


def compute_member_points(author_id: int, days: int = 21) -> dict:
    """Rolling points ledger over the last N days (default 21) for one user.

    Reads BOTH caller-mode rows (official-caller-channel posts) AND
    member-mode rows (shared-alert posts by non-callers) for this user.
    Both count toward the trader's points; the only distinction in the
    ledger is the source label.

    Policy (2026-06-02 wins-only, 2026-07-01 recency-banded): ONLY
    documented WINS score, for everyone — members and official callers
    alike. Losses, ghosts, pending entries, and losing screenshots all
    contribute 0 points (they still show in the bucket counts so the
    qualitative read stays honest).

    Win value is banded by the age of the DOCUMENTING event (the
    winning close/trim or the winning screenshot):

        +2 — win documented within the last 7 days
        +1 — win documented 8..N days ago

    This replaces the old hard cliff (win worth full value on day 13,
    worth nothing on day 15). Recency still dominates — this week's
    tape is worth double — but a documented edge no longer evaporates
    because someone took a vacation. Rows older than the window STAY
    in the DB (never deleted by scoring); they just stop scoring.

    A win event is either:
      - "entry win": an entry (open/add) in the window whose position
        shows a winning close — action='close' with gain_pct > 0, or a
        trim with gain_pct > 0 (a documented profit realization), or
      - "screenshot win": a close-only row with gain_pct > 0 (or
        gain_pct=None posted in gain-loss-porn — order tickets carry
        no gain pill and that channel is structurally a wins channel).

    Ghosting is decoupled from the window: an entry with no close
    ghosts when past its expiry OR open ≥14 days (fixed), regardless
    of the ledger window length. Ghosts score 0 either way; the split
    only matters for the qualitative pending-vs-ghost read.

    Grouping is by (UPPER(ticker), contract_type, strike, expiry). An
    "entry" is action IN ('open', 'add'). Winning trims group with
    closes (see above); trims without gain_pct stay neutral.

    Returns:
        {
            "points": int,                # wins_recent*2 + wins_older*1
            "window_days": int,
            "wins_recent": int,           # wins documented ≤7d ago (+2 each)
            "wins_older": int,            # wins documented 8..Nd ago (+1 each)
            "entries_won": int,           # entry + winning close/trim (0..N d)
            "entries_lost": int,          # entry + losing close (0 pts)
            "entries_ghosted": int,       # entry, no close, expired/≥14d (0 pts)
            "screenshot_wins": int,       # close-only, gain > 0
            "screenshot_losses": int,     # close-only, gain ≤ 0 (0 pts)
            "breakdown": list[dict],
        }
    """
    if not author_id:
        return {
            "points": 0,
            "window_days": days,
            "is_official_caller": False,
            "wins_recent": 0,
            "wins_older": 0,
            "entries_won": 0,
            "entries_lost": 0,
            "entries_ghosted": 0,
            "entries_pending": 0,
            "screenshot_wins": 0,
            "screenshot_losses": 0,
            "breakdown": [],
        }
    now = datetime.utcnow()
    today_iso = now.date().isoformat()
    # SELECT window is slightly larger than the scoring window so the
    # exact-N-day ghost rule has room to fire. Entries posted at exactly
    # N days ago need to be visible in the SELECT so the aged-out branch
    # can ghost them; otherwise they'd fall out of SELECT first and
    # never get scored. Anything ≥(N+1) days old still falls outside.
    cutoff = (now - timedelta(days=days + 1)).isoformat()
    # Ghosting is a judgment about the POSITION (entry with no close,
    # past expiry or stale), not about the ledger window — fixed at 14d
    # so widening the scoring window doesn't loosen the ghost read.
    ghost_age_cutoff = (now - timedelta(days=_db._GHOST_AGE_DAYS)).isoformat()
    # Recency band: wins documented within the last 7d score 2 pts,
    # older wins (8..N d) score 1 pt.
    recent_win_cutoff = (now - timedelta(days=_db._RECENT_WIN_DAYS)).isoformat()
    # Join chat_messages to recover the source channel for each row.
    # The channel name is the disambiguator for close-only screenshots
    # where the order ticket doesn't carry a gain pill: gain-loss-porn
    # is structurally a wins channel (members post P&L flexes there),
    # so a close-only with gain_pct=None in that channel is presumed
    # a win, not a loss. Without the join we have to assume loss for
    # every gain-less close — which mis-scored DeeP FRieD as 11 losses
    # on what were actually his win-of-the-month series posts.
    rows = _db.get_connection().execute(
        """SELECT at.posted_at, at.action, at.ticker, at.contract_type,
                  at.strike, at.expiry, at.gain_pct, at.tracking_mode,
                  cm.channel_name
             FROM analyst_trades AS at
        LEFT JOIN chat_messages AS cm
               ON cm.discord_message_id = at.discord_message_id
            WHERE at.author_id = ?
              AND at.is_trade = 1
              AND at.posted_at > ?
            ORDER BY at.posted_at ASC""",
        (int(author_id), cutoff),
    ).fetchall()

    # Group by contract key — one entry+close pair = one event
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (
            (r["ticker"] or "").upper(),
            (r["contract_type"] or "").lower(),
            r["strike"],
            r["expiry"] or "",
        )
        groups.setdefault(key, []).append(dict(r))

    entries_won = 0
    entries_lost = 0
    entries_ghosted = 0
    entries_pending = 0  # within window, before expiry, < 14d — not scored yet
    screenshot_wins = 0
    screenshot_losses = 0
    wins_recent = 0   # win documented ≤7d ago → 2 pts
    wins_older = 0    # win documented 8..Nd ago → 1 pt
    breakdown: list[dict] = []

    def _band_win(documented_at: str) -> int:
        """2 pts for a win documented in the last 7d, 1 pt for older."""
        nonlocal wins_recent, wins_older
        if (documented_at or "") > recent_win_cutoff:
            wins_recent += 1
            return 2
        wins_older += 1
        return 1

    for key, events in groups.items():
        opens = [
            e for e in events
            if (e["action"] or "").lower() in ("open", "add")
        ]
        # Trims with a gain_pct are profit-realization events on the
        # position — semantically equivalent to a close for points
        # purposes ("Trimmed 30%, +3k realized" IS a documented
        # realization). Group them with closes so an entry+trim+win
        # counts the same as an entry+close+win, and a standalone
        # winning trim screenshot scores like a winning close
        # screenshot. Trims without a gain_pct (size-management only,
        # no realized P&L visible) stay neutral.
        closes = [
            e for e in events
            if (e["action"] or "").lower() == "close"
            or (
                (e["action"] or "").lower() == "trim"
                and e.get("gain_pct") is not None
            )
        ]

        if opens:
            # User's spec:
            #   entry + winning close → +5
            #   entry + losing close  → +3
            #   entry + no close      → +2 (ghost penalty: 3 - 1)
            winning_close = None
            losing_close = None
            for c in closes:
                g = c.get("gain_pct")
                try:
                    gv = float(g) if g is not None else None
                except (TypeError, ValueError):
                    gv = None
                if gv is not None and gv > 0 and winning_close is None:
                    winning_close = c
                elif gv is not None and gv <= 0 and losing_close is None:
                    losing_close = c
            if winning_close is not None:
                entries_won += 1
                pts = _band_win(winning_close.get("posted_at") or "")
                breakdown.append({
                    "kind": (
                        f"entry → documented win "
                        f"(+{pts}: {'last 7d' if pts == 2 else 'older, half credit'})"
                    ),
                    "points": pts,
                    "ticker": key[0],
                    "gain_pct": winning_close.get("gain_pct"),
                })
            elif losing_close is not None:
                entries_lost += 1
                breakdown.append({
                    "kind": "entry → close loss (wins-only: 0 pts)",
                    "points": 0,
                    "ticker": key[0],
                    "gain_pct": losing_close.get("gain_pct"),
                })
            else:
                # No close in window. Ghost if EITHER:
                #   (a) position past its expiration date
                #   (b) entry posted ≥14d ago (window-edge ghost)
                # Else: pending — held in suspense, 0 points until
                # either condition trips or the position closes.
                earliest_open_at = sorted(
                    opens, key=lambda x: x["posted_at"]
                )[0]["posted_at"]
                expiry_str = key[3]  # 'YYYY-MM-DD' or ''
                past_expiry = bool(
                    expiry_str and expiry_str < today_iso
                )
                aged_out = earliest_open_at <= ghost_age_cutoff
                if past_expiry or aged_out:
                    entries_ghosted += 1
                    reason = []
                    if past_expiry:
                        reason.append(f"past expiry {expiry_str}")
                    if aged_out:
                        reason.append(f"open ≥{_db._GHOST_AGE_DAYS}d")
                    breakdown.append({
                        "kind": (
                            f"entry posted, no close (ghost: "
                            f"{', '.join(reason)}; wins-only: 0 pts)"
                        ),
                        "points": 0,
                        "ticker": key[0],
                    })
                else:
                    entries_pending += 1
                    breakdown.append({
                        "kind": (
                            "entry posted, no close yet (pending — "
                            f"before expiry, open <{_db._GHOST_AGE_DAYS}d)"
                        ),
                        "points": 0,
                        "ticker": key[0],
                    })
        elif closes and not opens:
            # Close-only / standalone P&L screenshot (no entry in window)
            # Split on outcome: winning screenshot = +2, losing = +1.
            # Use the LATEST close in the window for the outcome read.
            latest_close = sorted(closes, key=lambda x: x["posted_at"])[-1]
            g = latest_close.get("gain_pct")
            try:
                gv = float(g) if g is not None else None
            except (TypeError, ValueError):
                gv = None
            # Channel-based disambiguation for gain-less closes. Order
            # tickets (Robinhood "Sell to close — Filled at $X.XX") don't
            # carry a gain pill, so Gemini correctly extracts gain_pct=
            # None. Members posting these in gain-loss-porn are flexing
            # wins (the channel is structurally for that); members posting
            # them elsewhere stay as the conservative loss default.
            channel = (latest_close.get("channel_name") or "")
            channel_lower = channel.lower()
            in_winning_channel = "gain-loss-porn" in channel_lower
            if gv is not None and gv > 0:
                screenshot_wins += 1
                pts = _band_win(latest_close.get("posted_at") or "")
                breakdown.append({
                    "kind": (
                        f"screenshot win (close-only, "
                        f"+{pts}: {'last 7d' if pts == 2 else 'older, half credit'})"
                    ),
                    "points": pts,
                    "ticker": key[0],
                    "gain_pct": gv,
                })
            elif gv is None and in_winning_channel:
                # No gain pill visible, but channel signals win.
                screenshot_wins += 1
                pts = _band_win(latest_close.get("posted_at") or "")
                breakdown.append({
                    "kind": (
                        f"screenshot win — channel signal "
                        f"(gain-loss-porn, no gain pill on ticket, +{pts})"
                    ),
                    "points": pts,
                    "ticker": key[0],
                    "gain_pct": None,
                })
            else:
                screenshot_losses += 1
                breakdown.append({
                    "kind": "screenshot loss (close-only, wins-only: 0 pts)",
                    "points": 0,
                    "ticker": key[0],
                    "gain_pct": gv,
                })
        # Else: only trims / viewings — no points

    # Wins-only (2026-06-02), recency-banded (2026-07-01): the same rule
    # for everyone including official callers. Loss/ghost/pending → 0;
    # a win documented ≤7d ago → 2 pts; a win 8..Nd ago → 1 pt.
    caller_mode = _db.is_official_caller(int(author_id))
    total_points = wins_recent * 2 + wins_older * 1
    return {
        "points": total_points,
        "window_days": days,
        "is_official_caller": caller_mode,
        "wins_recent": wins_recent,
        "wins_older": wins_older,
        "entries_won": entries_won,
        "entries_lost": entries_lost,
        "entries_ghosted": entries_ghosted,
        "entries_pending": entries_pending,
        "screenshot_wins": screenshot_wins,
        "screenshot_losses": screenshot_losses,
        "breakdown": breakdown,
    }


def get_member_trade_events(
    author_id: int, days: int = 14, limit: int = 200,
) -> list[dict]:
    """All is_trade=1 rows for a single member-mode author over the last N
    days, newest first. Used by the trader-points scoring system
    (rolling 14-day window — see compute_member_points for the
    5/3/2/2/1 spec with the 0-point pending bucket).

    Caller-mode rows are excluded — official callers have their own
    surfacing in /ask context blocks (RECENT TRADES, currently open).
    This helper is exclusively for the member-mode points calculator.

    Returns the raw rows; the point scoring logic is intentionally NOT
    baked in here so the rubric can iterate without DB churn.
    """
    if not author_id:
        return []
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    rows = _db.get_connection().execute(
        """SELECT * FROM analyst_trades
           WHERE tracking_mode = 'member'
             AND is_trade = 1
             AND author_id = ?
             AND posted_at > ?
           ORDER BY posted_at DESC
           LIMIT ?""",
        (int(author_id), cutoff, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_expired_analyst_positions() -> list[dict]:
    """Daily cron entrypoint. Mark any trade rows whose expiry has passed
    AND have no inferred_status yet as 'expired_unknown'. Returns the rows
    that were newly marked, so the cron can announce them.
    """
    conn = _db.get_connection()
    # Collect what's about to change so we can return it for the announce
    targets = conn.execute(
        """SELECT id, ticker, contract_type, strike, expiry, action,
                  gain_pct, posted_at
           FROM analyst_trades
           WHERE expiry IS NOT NULL
             AND date(expiry) < date('now')
             AND inferred_status IS NULL
             AND is_trade = 1"""
    ).fetchall()
    if not targets:
        return []
    ids = [r["id"] for r in targets]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE analyst_trades SET inferred_status = 'expired_unknown' "
        f"WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()
    return [dict(r) for r in targets]
